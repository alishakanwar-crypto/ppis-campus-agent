"""
Standalone TrueFace ADMS / Push Protocol listener.

The TimeWatch TrueFace 300 uses a ZKTeco binary push protocol (NOT HTTP).
This listener handles the binary handshake, keeps the connection alive,
and processes real-time attendance events.

Run: python trueface_listener.py
"""
import asyncio
import struct
import logging
import json
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface_listener")

IST = timezone(timedelta(hours=5, minutes=30))

# Load user mappings
USERS_FILE = "trueface_users.json"
try:
    with open(USERS_FILE) as f:
        USERS = json.load(f)
    logger.info(f"Loaded {len(USERS)} TrueFace users from {USERS_FILE}")
except Exception:
    USERS = {}
    logger.warning(f"No {USERS_FILE} found, using empty user map")

_notified_today = {}
_last_dedup_date = ""


def _clear_daily_dedup():
    global _last_dedup_date, _notified_today
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_dedup_date:
        _notified_today.clear()
        _last_dedup_date = today


async def _send_whatsapp(name: str, phone: str, time_str: str):
    """Send WhatsApp notification via cloud backend."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://ppis-whatsapp-bot.fly.dev/api/send-whatsapp",
                json={
                    "to": phone,
                    "template_name": "ppis_teacher_present_text",
                    "language_code": "en",
                    "body_params": [name, time_str],
                },
            )
            logger.info(f"WhatsApp API response: {resp.status_code} {resp.text[:200]}")
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"WhatsApp send error: {e}")
        return False


def _hex_dump(data: bytes, max_bytes: int = 256) -> str:
    """Create a readable hex dump of binary data."""
    lines = []
    for i in range(0, min(len(data), max_bytes), 16):
        chunk = data[i:i+16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"  {i:04x}: {hex_part:<48} {ascii_part}")
    if len(data) > max_bytes:
        lines.append(f"  ... ({len(data) - max_bytes} more bytes)")
    return "\n".join(lines)


def _extract_serial(data: bytes) -> str:
    """Extract the device serial number from binary data."""
    try:
        text = data.decode("ascii", errors="ignore")
        # Look for TW serial number pattern
        import re
        match = re.search(r"(TW\d{13,16})", text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def _extract_attendance_data(data: bytes) -> list:
    """Try to extract attendance records from binary data.
    
    ZKTeco push protocol attendance records may contain:
    - User PIN (numeric)
    - Timestamp
    - Verification mode
    - Status
    
    Records are often embedded as text within binary frames.
    """
    records = []
    try:
        text = data.decode("ascii", errors="ignore")
        # Look for tab-separated attendance records: PIN\tTimestamp\tStatus...
        import re
        # Pattern: number, tab, datetime, tab, digits...
        pattern = r"(\d+)\t(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\t(\d+)\t(\d+)"
        for match in re.finditer(pattern, text):
            records.append({
                "pin": match.group(1),
                "timestamp": match.group(2),
                "status": match.group(3),
                "verify": match.group(4),
            })
        
        # Also try space-separated or other formats
        if not records:
            # Some devices use: USER_ID TIMESTAMP VERIFY_MODE STATUS
            pattern2 = r"(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(\d+)"
            for match in re.finditer(pattern2, text):
                records.append({
                    "pin": match.group(1),
                    "timestamp": match.group(2),
                    "status": "0",
                    "verify": match.group(3),
                })
    except Exception as e:
        logger.error(f"Error parsing attendance data: {e}")
    
    return records


async def _process_attendance(pin: str, timestamp: str):
    """Process a single attendance event."""
    _clear_daily_dedup()
    
    user = USERS.get(pin)
    if not user:
        logger.warning(f"[TRUEFACE] Unknown PIN={pin}")
        return
    
    name = user["name"]
    phone = user.get("phone", "")
    logger.info(f"[TRUEFACE] Attendance: {name} PIN={pin} time={timestamp}")
    
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if _notified_today.get(pin) == today:
        logger.info(f"[TRUEFACE] Already notified {name} today, skipping")
        return
    
    now = datetime.now(IST)
    if now.weekday() in (5, 6):
        logger.info(f"[TRUEFACE] Weekend skip for {name}")
        return
    
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        time_str = dt.strftime("%I:%M %p")
    except (ValueError, TypeError):
        time_str = now.strftime("%I:%M %p")
    
    if phone:
        _notified_today[pin] = today
        logger.info(f"[TRUEFACE] Sending WhatsApp to {phone} for {name} at {time_str}")
        asyncio.create_task(_send_whatsapp(name, phone, time_str))


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a persistent TCP connection from the TrueFace device."""
    addr = writer.get_extra_info("peername")
    logger.info(f"[TRUEFACE] === New connection from {addr} ===")

    serial = ""
    msg_count = 0

    try:
        while True:
            try:
                raw = await asyncio.wait_for(reader.read(65536), timeout=120.0)
            except asyncio.TimeoutError:
                logger.info(f"[TRUEFACE] Timeout waiting for data from {addr}, keeping alive")
                continue
            
            if not raw:
                logger.info(f"[TRUEFACE] Connection closed by {addr}")
                break
            
            msg_count += 1
            logger.info(f"[TRUEFACE] Message #{msg_count} from {addr} ({len(raw)} bytes)")
            logger.info(f"[TRUEFACE] Hex dump:\n{_hex_dump(raw)}")
            
            # Extract serial number if we haven't yet
            if not serial:
                serial = _extract_serial(raw)
                if serial:
                    logger.info(f"[TRUEFACE] Device serial: {serial}")
            
            # Try to extract attendance records
            records = _extract_attendance_data(raw)
            if records:
                logger.info(f"[TRUEFACE] Found {len(records)} attendance record(s)")
                for r in records:
                    await _process_attendance(r["pin"], r["timestamp"])
            
            # Analyze the binary header
            if len(raw) >= 8:
                header = struct.unpack("<HHHH", raw[:8])
                cmd_id, checksum, session_id, reply_id = header
                logger.info(f"[TRUEFACE] Header: cmd=0x{cmd_id:04x} chk=0x{checksum:04x} "
                           f"sess=0x{session_id:04x} reply=0x{reply_id:04x}")
                
                # Build acknowledgment response with same session
                # ZKTeco protocol: reply with CMD_ACK_OK (0x7d05) or echo back
                ack_cmd = 0x7d05  # CMD_ACK_OK
                ack_data = b""
                ack_len = 8 + len(ack_data)
                
                # Try simple echo-back acknowledgment
                reply = struct.pack("<HHHH", ack_cmd, 0, session_id, reply_id)
                writer.write(reply)
                await writer.drain()
                logger.info(f"[TRUEFACE] Sent ACK (cmd=0x{ack_cmd:04x} sess=0x{session_id:04x})")
            else:
                # For short messages, just echo back
                writer.write(raw)
                await writer.drain()
                logger.info(f"[TRUEFACE] Echoed back {len(raw)} bytes")
    
    except ConnectionResetError:
        logger.info(f"[TRUEFACE] Connection reset by {addr}")
    except Exception as e:
        logger.error(f"[TRUEFACE] Error handling {addr}: {e}", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        logger.info(f"[TRUEFACE] Connection from {addr} ended (serial={serial}, msgs={msg_count})")


async def main():
    port = 8898
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port)
    logger.info(f"[TRUEFACE] Standalone listener started on port {port}")
    logger.info(f"[TRUEFACE] Registered users: {json.dumps(USERS, indent=2)}")
    logger.info(f"[TRUEFACE] Waiting for TrueFace device connections...")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
