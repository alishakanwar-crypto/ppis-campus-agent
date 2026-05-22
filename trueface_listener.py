"""
Standalone TrueFace Push Protocol listener.

The TimeWatch TrueFace 300 uses a ZKTeco binary push protocol.
This listener tries multiple response strategies to establish
a proper connection with the device.

Run: python trueface_listener.py
"""
import asyncio
import struct
import logging
import json
import re
import sys
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

# Response strategy (change via command line: python trueface_listener.py 1)
# 0 = no response (silent)
# 1 = echo back same packet
# 2 = 8-byte short ACK
# 3 = 16-byte header ACK
# 4 = same 96 bytes with modified command
STRATEGY = int(sys.argv[1]) if len(sys.argv) > 1 else 0


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
    try:
        text = data.decode("ascii", errors="ignore")
        match = re.search(r"(TW\d{13,16})", text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def _extract_attendance_data(data: bytes) -> list:
    records = []
    try:
        text = data.decode("ascii", errors="ignore")
        for match in re.finditer(r"(\d+)\t(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\t(\d+)\t(\d+)", text):
            records.append({
                "pin": match.group(1), "timestamp": match.group(2),
                "status": match.group(3), "verify": match.group(4),
            })
        if not records:
            for match in re.finditer(r"(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(\d+)", text):
                records.append({
                    "pin": match.group(1), "timestamp": match.group(2),
                    "status": "0", "verify": match.group(3),
                })
    except Exception as e:
        logger.error(f"Error parsing: {e}")
    return records


async def _process_attendance(pin: str, timestamp: str):
    _clear_daily_dedup()
    user = USERS.get(pin)
    if not user:
        logger.warning(f"[TRUEFACE] Unknown PIN={pin}")
        return
    name = user["name"]
    phone = user.get("phone", "")
    logger.info(f"[TRUEFACE] ATTENDANCE: {name} PIN={pin} time={timestamp}")
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if _notified_today.get(pin) == today:
        logger.info(f"[TRUEFACE] Already notified {name} today")
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


def _build_response(raw: bytes, serial: str) -> bytes:
    """Build response based on current strategy."""
    if STRATEGY == 0:
        return b""  # No response
    elif STRATEGY == 1:
        return raw  # Echo back same packet
    elif STRATEGY == 2:
        # 8-byte short ACK
        return struct.pack("<BBBBBBBB", 0xb4, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)
    elif STRATEGY == 3:
        # 16-byte header-only ACK
        resp = bytearray(16)
        resp[0] = 0xb4
        resp[4] = 0x01  # Success flag
        resp[8] = 0x07  # Protocol version
        return bytes(resp)
    elif STRATEGY == 4:
        # Same 96 bytes with command byte changed
        resp = bytearray(raw)
        resp[0] = 0xb5  # ACK command
        return bytes(resp)
    return b""


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    logger.info(f"[TRUEFACE] === Connection from {addr} (strategy={STRATEGY}) ===")

    serial = ""
    msg_count = 0

    try:
        while True:
            try:
                raw = await asyncio.wait_for(reader.read(65536), timeout=60.0)
            except asyncio.TimeoutError:
                logger.info(f"[TRUEFACE] 60s timeout from {addr}")
                break

            if not raw:
                logger.info(f"[TRUEFACE] Connection closed by {addr}")
                break

            msg_count += 1
            logger.info(f"[TRUEFACE] Msg #{msg_count} ({len(raw)} bytes) cmd=0x{raw[0]:02x}")
            logger.info(f"[TRUEFACE] Hex:\n{_hex_dump(raw)}")

            if not serial:
                serial = _extract_serial(raw)
                if serial:
                    logger.info(f"[TRUEFACE] Serial: {serial}")

            # Check for attendance data
            records = _extract_attendance_data(raw)
            if records:
                logger.info(f"[TRUEFACE] FOUND {len(records)} attendance record(s)!")
                for r in records:
                    await _process_attendance(r["pin"], r["timestamp"])

            # Send response based on strategy
            resp = _build_response(raw, serial)
            if resp:
                writer.write(resp)
                await writer.drain()
                logger.info(f"[TRUEFACE] Sent {len(resp)}-byte response")
            else:
                logger.info(f"[TRUEFACE] No response (strategy=0, keeping connection open)")

    except ConnectionResetError:
        logger.info(f"[TRUEFACE] Connection reset by {addr}")
    except Exception as e:
        logger.error(f"[TRUEFACE] Error: {e}", exc_info=True)
    finally:
        try:
            writer.close()
        except Exception:
            pass
        logger.info(f"[TRUEFACE] Ended {addr} serial={serial} msgs={msg_count}")


async def main():
    port = 8898
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port)
    logger.info(f"[TRUEFACE] Listener on port {port}, strategy={STRATEGY}")
    strategies = {
        0: "SILENT (no response)",
        1: "ECHO (same packet back)",
        2: "SHORT ACK (8 bytes)",
        3: "HEADER ACK (16 bytes)",
        4: "MODIFIED (96 bytes, cmd=0xb5)",
    }
    logger.info(f"[TRUEFACE] Response: {strategies.get(STRATEGY, 'unknown')}")
    logger.info(f"[TRUEFACE] Users: {json.dumps(USERS, indent=2)}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
