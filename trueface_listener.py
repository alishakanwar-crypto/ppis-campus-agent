"""
Standalone TrueFace Push Protocol listener.

The TimeWatch TrueFace 300 uses a ZKTeco binary push protocol (NOT HTTP).
This listener handles the binary handshake, keeps the connection alive,
and processes real-time attendance events.

Protocol analysis (from device captures):
  - Registration packet: 96 bytes
    - Offset 0x00: command/type (0xb4)
    - Offset 0x04: data length indicator (0x40 = 64)
    - Offset 0x08: protocol version (0x07)
    - Offset 0x20: serial number (16 bytes, null-padded)
  - After registration, device sends attendance events in same connection

Run: python trueface_listener.py
"""
import asyncio
import struct
import logging
import json
import re
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
        match = re.search(r"(TW\d{13,16})", text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def _extract_attendance_data(data: bytes) -> list:
    """Try to extract attendance records from binary data."""
    records = []
    try:
        text = data.decode("ascii", errors="ignore")
        # Tab-separated: PIN\tTimestamp\tStatus\tVerify
        for match in re.finditer(r"(\d+)\t(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\t(\d+)\t(\d+)", text):
            records.append({
                "pin": match.group(1),
                "timestamp": match.group(2),
                "status": match.group(3),
                "verify": match.group(4),
            })
        # Space-separated fallback
        if not records:
            for match in re.finditer(r"(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(\d+)", text):
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
    logger.info(f"[TRUEFACE] ATTENDANCE DETECTED: {name} PIN={pin} time={timestamp}")

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
        logger.info(f"[TRUEFACE] >>> Sending WhatsApp to {phone} for {name} at {time_str}")
        asyncio.create_task(_send_whatsapp(name, phone, time_str))


def _build_registration_ack(serial: str) -> bytes:
    """Build a registration acknowledgment response.
    
    Try multiple response strategies to find what the device accepts.
    Strategy: mirror the registration packet structure with ACK flag.
    """
    # Build a 96-byte response mirroring the registration format
    resp = bytearray(96)
    # Set command to 0xb5 (ACK for 0xb4 registration)
    resp[0] = 0xb5
    resp[1] = 0x00
    resp[2] = 0x00
    resp[3] = 0x00
    # Data length indicator
    resp[4] = 0x40
    resp[5] = 0x00
    resp[6] = 0x00
    resp[7] = 0x00
    # Protocol version
    resp[8] = 0x07
    # Place serial number at same offset (0x20)
    serial_bytes = serial.encode("ascii")[:16]
    resp[0x20:0x20+len(serial_bytes)] = serial_bytes
    return bytes(resp)


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a persistent TCP connection from the TrueFace device."""
    addr = writer.get_extra_info("peername")
    logger.info(f"[TRUEFACE] === New connection from {addr} ===")

    serial = ""
    msg_count = 0
    registered = False

    try:
        while True:
            try:
                # Read data - use a shorter timeout initially, longer after registration
                timeout = 30.0 if not registered else 300.0
                raw = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            except asyncio.TimeoutError:
                logger.info(f"[TRUEFACE] Timeout from {addr} (registered={registered})")
                # Send a keepalive ping if registered
                if registered:
                    try:
                        writer.write(b"\x00")
                        await writer.drain()
                    except Exception:
                        break
                continue

            if not raw:
                logger.info(f"[TRUEFACE] Connection closed by {addr}")
                break

            msg_count += 1
            logger.info(f"[TRUEFACE] Message #{msg_count} from {addr} ({len(raw)} bytes)")
            logger.info(f"[TRUEFACE] Hex dump:\n{_hex_dump(raw)}")

            # Extract serial number
            if not serial:
                serial = _extract_serial(raw)
                if serial:
                    logger.info(f"[TRUEFACE] Device serial: {serial}")

            # Check if this is a registration packet (96 bytes, starts with 0xb4)
            if len(raw) == 96 and raw[0] == 0xb4 and not registered:
                logger.info(f"[TRUEFACE] Registration packet from {serial}")
                
                # Strategy 1: Send ACK response
                ack = _build_registration_ack(serial)
                writer.write(ack)
                await writer.drain()
                registered = True
                logger.info(f"[TRUEFACE] Sent registration ACK, connection established")
                continue

            # Check for attendance data in any subsequent message
            records = _extract_attendance_data(raw)
            if records:
                logger.info(f"[TRUEFACE] Found {len(records)} attendance record(s)!")
                for r in records:
                    await _process_attendance(r["pin"], r["timestamp"])
            else:
                # Log the command byte for analysis
                if len(raw) >= 4:
                    cmd = raw[0]
                    logger.info(f"[TRUEFACE] Unknown message type: cmd=0x{cmd:02x} "
                               f"len={len(raw)} registered={registered}")
                    
                    # For any message after registration, try sending a simple ACK
                    if registered:
                        # Echo the command byte with 0x01 flag as ACK
                        simple_ack = bytearray(8)
                        simple_ack[0] = cmd + 1  # ACK = cmd + 1
                        writer.write(bytes(simple_ack))
                        await writer.drain()
                        logger.info(f"[TRUEFACE] Sent simple ACK (0x{cmd+1:02x})")

    except ConnectionResetError:
        logger.info(f"[TRUEFACE] Connection reset by {addr}")
    except Exception as e:
        logger.error(f"[TRUEFACE] Error: {e}", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        logger.info(f"[TRUEFACE] Connection from {addr} ended "
                    f"(serial={serial}, msgs={msg_count}, registered={registered})")


async def main():
    port = 8898
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port)
    logger.info(f"[TRUEFACE] Standalone listener started on port {port}")
    logger.info(f"[TRUEFACE] Users: {json.dumps(USERS, indent=2)}")
    logger.info(f"[TRUEFACE] Waiting for device connections...")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
