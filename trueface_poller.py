"""
TrueFace Attendance Poller.

Instead of waiting for the device to push events (binary protocol),
this script polls the TrueFace device's web API for attendance records.

It checks every POLL_INTERVAL seconds for new events and sends
WhatsApp notifications for new detections.

Run: python trueface_poller.py
"""
import asyncio
import logging
import json
import httpx
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface_poller")

IST = timezone(timedelta(hours=5, minutes=30))
DEVICE_IP = "192.168.1.112"
DEVICE_URL = f"http://{DEVICE_IP}"
DEVICE_USER = "admin"
DEVICE_PASS = "tipl9910"
POLL_INTERVAL = 5  # seconds

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
_seen_events = set()  # Track already-processed event IDs


def _clear_daily_dedup():
    global _last_dedup_date, _notified_today, _seen_events
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_dedup_date:
        _notified_today.clear()
        _seen_events.clear()
        _last_dedup_date = today


async def _send_whatsapp(name: str, phone: str, time_str: str):
    """Send WhatsApp notification via cloud backend."""
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
        await _send_whatsapp(name, phone, time_str)


# Common ZKTeco/TimeWatch web API endpoints for attendance logs
API_ENDPOINTS = [
    # ADMS/iClock style
    "/iclock/cdata?SN=TW30000001260433&table=ATTLOG&Stamp=0",
    "/csl/eventlog",
    "/csl/attendance", 
    # ZKTeco standard API
    "/api/attendance/logs",
    "/api/attlog",
    "/data/attendance",
    "/data/attlog",
    # Device info / status
    "/api/device/info",
    "/api/system/info",
    "/csl/device",
    # Log queries
    "/iclock/attlog",
    "/att/attlog",
    "/transaction/listall",
    "/api/transaction/listall",
    # ZKBio endpoints
    "/api/v1/attendance",
    "/api/v1/logs",
    # Common REST patterns
    "/logs",
    "/records",
    "/attendance",
    "/events",
    # ISAPI (Hikvision-style, some rebranded devices)
    "/ISAPI/AccessControl/AcsEvent",
    "/ISAPI/Event/notification/alertStream",
]


async def discover_api(client: httpx.AsyncClient):
    """Try common API endpoints to find the attendance log API."""
    logger.info(f"[TRUEFACE] Discovering API endpoints on {DEVICE_URL}...")
    
    working_endpoints = []
    
    for endpoint in API_ENDPOINTS:
        url = f"{DEVICE_URL}{endpoint}"
        try:
            resp = await client.get(url, timeout=5)
            status = resp.status_code
            body = resp.text[:200]
            if status != 404:
                logger.info(f"  {endpoint} -> {status}: {body}")
                working_endpoints.append((endpoint, status, body))
            else:
                logger.debug(f"  {endpoint} -> 404")
        except Exception as e:
            logger.debug(f"  {endpoint} -> Error: {e}")
    
    if working_endpoints:
        logger.info(f"[TRUEFACE] Found {len(working_endpoints)} working endpoint(s)")
    else:
        logger.warning(f"[TRUEFACE] No working endpoints found with GET")
    
    # Also try the root API and static paths for hints
    for path in ["/", "/api", "/api/", "/csl", "/data"]:
        try:
            resp = await client.get(f"{DEVICE_URL}{path}", timeout=5)
            if resp.status_code != 404:
                logger.info(f"  {path} -> {resp.status_code}: {resp.text[:300]}")
        except Exception:
            pass
    
    return working_endpoints


async def try_digest_auth(client: httpx.AsyncClient):
    """Try with digest authentication (common for ZKTeco devices)."""
    logger.info("[TRUEFACE] Trying digest authentication...")
    auth = httpx.DigestAuth(DEVICE_USER, DEVICE_PASS)
    for endpoint in ["/iclock/cdata?SN=TW30000001260433&table=ATTLOG&Stamp=0",
                     "/csl/eventlog", "/api/attendance/logs", "/data/attlog"]:
        url = f"{DEVICE_URL}{endpoint}"
        try:
            resp = await client.get(url, auth=auth, timeout=5)
            if resp.status_code != 404:
                logger.info(f"  [DIGEST] {endpoint} -> {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.debug(f"  [DIGEST] {endpoint} -> Error: {e}")


async def try_basic_auth(client: httpx.AsyncClient):
    """Try with basic authentication."""
    logger.info("[TRUEFACE] Trying basic authentication...")
    auth = httpx.BasicAuth(DEVICE_USER, DEVICE_PASS)
    for endpoint in ["/iclock/cdata?SN=TW30000001260433&table=ATTLOG&Stamp=0",
                     "/csl/eventlog", "/api/attendance/logs", "/data/attlog"]:
        url = f"{DEVICE_URL}{endpoint}"
        try:
            resp = await client.get(url, auth=auth, timeout=5)
            if resp.status_code != 404:
                logger.info(f"  [BASIC] {endpoint} -> {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.debug(f"  [BASIC] {endpoint} -> Error: {e}")


async def try_cookie_auth(client: httpx.AsyncClient):
    """Try logging in via the web UI login endpoint and using session cookies."""
    logger.info("[TRUEFACE] Trying cookie/session authentication...")
    
    # Common login endpoints
    login_endpoints = [
        ("/api/login", {"username": DEVICE_USER, "password": DEVICE_PASS}),
        ("/api/auth/login", {"username": DEVICE_USER, "password": DEVICE_PASS}),
        ("/csl/login", {"username": DEVICE_USER, "password": DEVICE_PASS}),
        ("/login", {"username": DEVICE_USER, "password": DEVICE_PASS}),
        ("/api/login", {"user": DEVICE_USER, "pass": DEVICE_PASS}),
    ]
    
    for login_url, payload in login_endpoints:
        try:
            resp = await client.post(f"{DEVICE_URL}{login_url}", json=payload, timeout=5)
            logger.info(f"  [LOGIN] POST {login_url} -> {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 200:
                # Try getting attendance with the session
                for ep in ["/csl/eventlog", "/api/attendance/logs", "/data/attlog"]:
                    try:
                        r = await client.get(f"{DEVICE_URL}{ep}", timeout=5)
                        if r.status_code != 404:
                            logger.info(f"  [SESSION] {ep} -> {r.status_code}: {r.text[:200]}")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"  [LOGIN] {login_url} -> Error: {e}")


async def main():
    logger.info(f"[TRUEFACE] TrueFace Attendance Poller starting...")
    logger.info(f"[TRUEFACE] Device: {DEVICE_URL}")
    logger.info(f"[TRUEFACE] Users: {json.dumps(USERS, indent=2)}")
    
    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Phase 1: Discover working API endpoints
        working = await discover_api(client)
        
        # Phase 2: Try authentication methods
        await try_basic_auth(client)
        await try_digest_auth(client)
        await try_cookie_auth(client)
        
        logger.info("[TRUEFACE] API discovery complete. Check the output above for working endpoints.")
        logger.info("[TRUEFACE] Share this output so we can determine the correct polling endpoint.")


if __name__ == "__main__":
    asyncio.run(main())
