"""
Standalone TrueFace ADMS listener.

Listens on port 8898 for raw TCP connections from the TrueFace device.
Parses the ZKTeco ADMS protocol and forwards attendance events to the
campus agent's /api/trueface endpoints.

Run: python trueface_listener.py
"""
import asyncio
import logging
import urllib.parse
import json
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface_listener")

IST = timezone(timedelta(hours=5, minutes=30))
AGENT_URL = "http://127.0.0.1:8897"

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


def _parse_attlog(body: str):
    records = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            records.append({
                "pin": parts[0].strip(),
                "timestamp": parts[1].strip() if len(parts) > 1 else "",
                "status": parts[2].strip() if len(parts) > 2 else "0",
                "verify": parts[3].strip() if len(parts) > 3 else "0",
            })
    return records


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a raw TCP connection from the TrueFace device."""
    addr = writer.get_extra_info("peername")
    logger.info(f"[TRUEFACE] Connection from {addr}")

    try:
        # Read the full HTTP request
        raw = await asyncio.wait_for(reader.read(65536), timeout=10.0)
        raw_text = raw.decode("utf-8", errors="replace")
        logger.info(f"[TRUEFACE] Raw data ({len(raw)} bytes):\n{raw_text[:500]}")

        # Parse HTTP request line
        lines = raw_text.split("\r\n")
        if not lines:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        request_line = lines[0]
        logger.info(f"[TRUEFACE] Request: {request_line}")

        # Find body (after blank line)
        body = ""
        blank_idx = raw_text.find("\r\n\r\n")
        if blank_idx >= 0:
            body = raw_text[blank_idx + 4:]

        # Parse method and path
        parts = request_line.split(" ")
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"

        # Parse query string
        parsed = urllib.parse.urlparse(path)
        params = urllib.parse.parse_qs(parsed.query)
        sn = params.get("SN", [params.get("sn", ["unknown"])[0]])[0]
        table = params.get("table", [""])[0].upper()

        logger.info(f"[TRUEFACE] {method} {parsed.path} SN={sn} table={table}")

        if "cdata" in parsed.path.lower():
            if method == "GET":
                # Handshake response
                response = (
                    f"GET OPTION FROM: {sn}\r\n"
                    f"ATTLOGStamp=0\r\n"
                    f"OPERLOGStamp=0\r\n"
                    f"ATTPHOTOStamp=0\r\n"
                    f"ErrorDelay=30\r\n"
                    f"Delay=5\r\n"
                    f"TransTimes=00:00;14:05\r\n"
                    f"TransInterval=1\r\n"
                    f"TransFlag=TransData AttLog\tOpLog\r\n"
                    f"TimeZone=5\r\n"
                    f"Realtime=1\r\n"
                    f"Encrypt=0\r\n"
                )
                http_resp = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: text/plain\r\n"
                    f"Content-Length: {len(response)}\r\n"
                    f"\r\n"
                    f"{response}"
                )
                writer.write(http_resp.encode())
                logger.info(f"[TRUEFACE] Sent handshake for SN={sn}")

            elif method == "POST":
                logger.info(f"[TRUEFACE] POST body: {body[:300]}")

                if table == "ATTLOG":
                    records = _parse_attlog(body)
                    _clear_daily_dedup()

                    for record in records:
                        pin = record["pin"]
                        timestamp = record["timestamp"]
                        user = USERS.get(pin)

                        if not user:
                            logger.warning(f"[TRUEFACE] Unknown PIN={pin}")
                            continue

                        logger.info(f"[TRUEFACE] Attendance: {user['name']} PIN={pin} time={timestamp}")

                        today = datetime.now(IST).strftime("%Y-%m-%d")
                        if _notified_today.get(pin) == today:
                            logger.info(f"[TRUEFACE] Already notified {user['name']} today")
                            continue

                        now = datetime.now(IST)
                        if now.weekday() in (5, 6):
                            logger.info(f"[TRUEFACE] Weekend skip for {user['name']}")
                            continue

                        try:
                            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                            time_str = dt.strftime("%I:%M %p")
                        except (ValueError, TypeError):
                            time_str = now.strftime("%I:%M %p")

                        phone = user.get("phone", "")
                        if phone:
                            _notified_today[pin] = today
                            name = user["name"]
                            logger.info(f"[TRUEFACE] Sending WhatsApp to {phone} for {name} at {time_str}")
                            asyncio.create_task(_send_whatsapp(name, phone, time_str))

                http_resp = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
                writer.write(http_resp.encode())

        elif "getrequest" in parsed.path.lower():
            # Device polling for commands
            http_resp = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
            writer.write(http_resp.encode())
            logger.info(f"[TRUEFACE] getrequest poll from SN={sn}")

        elif "devicecmd" in parsed.path.lower():
            http_resp = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
            writer.write(http_resp.encode())

        else:
            http_resp = "HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
            writer.write(http_resp.encode())

        await writer.drain()

    except asyncio.TimeoutError:
        logger.warning(f"[TRUEFACE] Timeout reading from {addr}")
    except Exception as e:
        logger.error(f"[TRUEFACE] Error: {e}", exc_info=True)
        try:
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    port = 8898
    server = await asyncio.start_server(handle_connection, "0.0.0.0", port)
    logger.info(f"[TRUEFACE] Standalone listener started on port {port}")
    logger.info(f"[TRUEFACE] Registered users: {json.dumps(USERS, indent=2)}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
