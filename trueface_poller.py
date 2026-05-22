"""
TrueFace Attendance Poller — Dahua JSON-RPC Protocol.

The TrueFace 300 uses a Dahua-based web interface with JSON-RPC API.
This script authenticates, searches for access control records,
and sends WhatsApp notifications for new face recognition events.

Run: python trueface_poller.py
"""
import asyncio
import hashlib
import logging
import json
import re
import time
import httpx
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface_poller")

IST = timezone(timedelta(hours=5, minutes=30))
DEVICE_IP = "192.168.1.112"
DEVICE_URL = f"http://{DEVICE_IP}"
DEVICE_USER = "admin"
DEVICE_PASS = "tipl9910"
POLL_INTERVAL = 10  # seconds between polls

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
_seen_events = set()


def _clear_daily_dedup():
    global _last_dedup_date, _notified_today, _seen_events
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_dedup_date:
        _notified_today.clear()
        _seen_events.clear()
        _last_dedup_date = today


async def _send_whatsapp(name: str, phone: str, time_str: str):
    import httpx as hx
    try:
        async with hx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://ppis-whatsapp-bot.fly.dev/api/send-whatsapp",
                json={
                    "to": phone,
                    "template_name": "ppis_teacher_present_text",
                    "language_code": "en",
                    "body_params": [name, time_str],
                },
            )
            logger.info(f"WhatsApp: {resp.status_code} {resp.text[:200]}")
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
        return False


async def _process_attendance(pin: str, timestamp: str):
    _clear_daily_dedup()
    user = USERS.get(pin)
    if not user:
        logger.warning(f"Unknown PIN={pin}")
        return
    name = user["name"]
    phone = user.get("phone", "")
    logger.info(f"ATTENDANCE: {name} PIN={pin} time={timestamp}")
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if _notified_today.get(pin) == today:
        logger.info(f"Already notified {name} today")
        return
    now = datetime.now(IST)
    if now.weekday() in (5, 6):
        logger.info(f"Weekend skip for {name}")
        return
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        time_str = dt.strftime("%I:%M %p")
    except (ValueError, TypeError):
        time_str = now.strftime("%I:%M %p")
    if phone:
        _notified_today[pin] = today
        logger.info(f">>> Sending WhatsApp to {phone} for {name} at {time_str}")
        await _send_whatsapp(name, phone, time_str)


class DahuaRPC:
    """Dahua JSON-RPC client for TrueFace device."""

    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.base_url = f"http://{host}"
        self.user = user
        self.password = password
        self.session_id = None
        self.request_id = 0
        self.client = None

    async def connect(self):
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=True)

    async def close(self):
        if self.client:
            await self.client.aclose()

    def _next_id(self):
        self.request_id += 1
        return self.request_id

    async def _rpc(self, method: str, params=None, endpoint="/RPC2", session=None):
        """Send a JSON-RPC request."""
        payload = {
            "method": method,
            "id": self._next_id(),
        }
        if params is not None:
            payload["params"] = params
        if session or self.session_id:
            payload["session"] = session or self.session_id

        try:
            resp = await self.client.post(
                f"{self.base_url}{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json()
            return data
        except Exception as e:
            logger.error(f"RPC error ({method}): {e}")
            return None

    async def login(self) -> bool:
        """Authenticate using Dahua digest login protocol."""
        logger.info(f"Logging in to {self.base_url} as {self.user}...")

        # Step 1: Send initial login to get challenge
        resp = await self._rpc(
            "global.login",
            {
                "userName": self.user,
                "password": "",
                "clientType": "Web3.0",
            },
            endpoint="/RPC2_Login",
        )

        if not resp:
            logger.error("No response from login step 1")
            return False

        logger.info(f"Login step 1 response: {json.dumps(resp)[:300]}")

        # Extract challenge parameters
        session = resp.get("session")
        params = resp.get("params", {})
        realm = params.get("realm", "")
        random_str = params.get("random", "")
        encryption = params.get("encryption", "Default")

        if not session or not realm:
            # Maybe the device uses a simpler auth
            logger.warning(f"No challenge received, trying direct login...")
            # Try with password directly
            resp2 = await self._rpc(
                "global.login",
                {
                    "userName": self.user,
                    "password": self.password,
                    "clientType": "Web3.0",
                    "loginType": "Direct",
                },
                endpoint="/RPC2_Login",
            )
            if resp2 and resp2.get("result"):
                self.session_id = resp2.get("session")
                logger.info(f"Direct login successful! Session: {self.session_id}")
                return True
            logger.error(f"Direct login failed: {resp2}")
            return False

        # Step 2: Compute digest authentication
        # Dahua digest: MD5(user:realm:password) then MD5(user:random:ha1)
        ha1 = hashlib.md5(f"{self.user}:{realm}:{self.password}".encode()).hexdigest().upper()
        auth_response = hashlib.md5(f"{self.user}:{random_str}:{ha1}".encode()).hexdigest().upper()

        logger.info(f"Computing auth: realm={realm}, random={random_str[:8]}...")

        # Step 3: Send authenticated login
        resp2 = await self._rpc(
            "global.login",
            {
                "userName": self.user,
                "password": auth_response,
                "clientType": "Web3.0",
                "loginType": "Direct",
                "authorityType": encryption,
            },
            endpoint="/RPC2_Login",
            session=session,
        )

        if not resp2:
            logger.error("No response from login step 2")
            return False

        logger.info(f"Login step 2 response: {json.dumps(resp2)[:300]}")

        if resp2.get("result"):
            self.session_id = resp2.get("session", session)
            logger.info(f"Login successful! Session: {self.session_id}")
            return True
        else:
            error = resp2.get("error", {})
            logger.error(f"Login failed: {error}")
            return False

    async def search_records(self, start_time: str, end_time: str) -> list:
        """Search access control / attendance records.
        
        Dahua uses RecordFinder pattern:
        1. factory -> create finder
        2. startFind -> set search params
        3. doFind -> get results
        4. stopFind -> cleanup
        5. destroy -> release
        """
        records = []

        # Try multiple record finder names
        finder_names = [
            "AccessControlCardRec",
            "TrafficEventDetail",
            "AccessEvent",
            "AttendanceRecord",
        ]

        for finder_name in finder_names:
            try:
                result = await self._search_with_finder(finder_name, start_time, end_time)
                if result:
                    records.extend(result)
                    logger.info(f"Found {len(result)} records with finder '{finder_name}'")
                    break
            except Exception as e:
                logger.debug(f"Finder '{finder_name}' failed: {e}")

        return records

    async def _search_with_finder(self, finder_name: str, start_time: str, end_time: str) -> list:
        """Use RecordFinder to search for records."""
        # Step 1: Create finder
        resp = await self._rpc("RecordFinder.factory", {"name": finder_name})
        if not resp or not resp.get("result"):
            return []

        object_id = resp["result"]
        logger.info(f"Created finder '{finder_name}' -> object_id={object_id}")

        try:
            # Step 2: Start search
            condition = {
                "QueryCondition": {
                    "StartTime": start_time,
                    "EndTime": end_time,
                }
            }
            resp = await self._rpc(f"{object_id}.startFind", condition)
            if not resp or not resp.get("result"):
                logger.debug(f"startFind failed for {finder_name}")
                return []

            # Step 3: Get results
            resp = await self._rpc(f"{object_id}.doFind", {"count": 100})
            if not resp:
                return []

            found = resp.get("params", {}).get("found", 0)
            items = resp.get("params", {}).get("records", [])
            logger.info(f"doFind: found={found}, items={len(items)}")

            # Step 4: Stop and destroy
            await self._rpc(f"{object_id}.stopFind")
            await self._rpc(f"{object_id}.destroy")

            return items

        except Exception as e:
            logger.error(f"Search error: {e}")
            # Cleanup
            try:
                await self._rpc(f"{object_id}.stopFind")
                await self._rpc(f"{object_id}.destroy")
            except Exception:
                pass
            return []

    async def list_services(self):
        """List all available services/methods on the device."""
        logger.info("Listing available services...")
        
        # Try system.listService to discover all available RPC methods
        for method in ["system.listService", "system.listMethod", "magicBox.listMethod"]:
            resp = await self._rpc(method)
            if resp and resp.get("result"):
                logger.info(f"  {method} -> SUCCESS")
                params = resp.get("params", {})
                if isinstance(params, dict):
                    for key, val in params.items():
                        logger.info(f"    {key}: {str(val)[:200]}")
                elif isinstance(params, list):
                    for item in params[:50]:
                        logger.info(f"    {item}")
                return params
            elif resp:
                logger.info(f"  {method} -> {resp.get('error', {})}")
        return None

    async def try_alternative_apis(self):
        """Try various Dahua API methods to find records."""
        now = datetime.now(IST)
        start = now.strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d 23:59:59")
        
        methods_to_try = [
            # List available services first
            ("system.listService", None),
            ("system.listMethod", None),
            # Event/log methods
            ("EventManager.factory", {"name": "AccessControlAlarmRecord"}),
            ("EventManager.factory", {"name": "AccessControl"}),
            ("MediaFinder.factory", {"name": "AccessControlEvent"}),
            ("RecordUpdater.factory", {"name": "AccessControlCardRec"}),
            ("QueryRecordManager.factory", None),
            # Direct query methods
            ("AccessService.getAccessEventList", {
                "StartTime": start, "EndTime": end,
            }),
            ("AccessService.searchAccessEvent", {
                "StartTime": start, "EndTime": end, "type": "All",
            }),
            ("configManager.getConfig", {"name": "AccessControl"}),
            ("accessControlManager.getCaps", None),
            ("accessControlManager.getRecordCount", None),
            ("accessControlManager.searchRecord", {
                "StartTime": start, "EndTime": end,
            }),
            # Attendance specific
            ("attendanceManager.getRecordCount", None),
            ("attendanceManager.findRecord", {
                "StartTime": start, "EndTime": end,
            }),
            # Generic finders with different names
            ("RecordFinder.factory", {"name": "AccessControlRecord"}),
            ("RecordFinder.factory", {"name": "EventRecord"}),
            ("RecordFinder.factory", {"name": "CardRecord"}),
            ("RecordFinder.factory", {"name": "FaceRecord"}),
            # Log manager variants
            ("log.startFind", {
                "condition": {"StartTime": start, "EndTime": end, "Types": ["All"]}
            }),
            ("log.factory", {"name": "All"}),
        ]

        logger.info("Trying alternative API methods...")
        for method, params in methods_to_try:
            resp = await self._rpc(method, params)
            if resp:
                result = resp.get("result", False)
                error = resp.get("error", {})
                p = resp.get("params", {})
                status = "SUCCESS" if result else f"FAIL({error.get('message', '')})"
                logger.info(f"  {method} -> {status} params={str(p)[:200]}")


async def main():
    logger.info("=" * 60)
    logger.info("TrueFace Attendance Poller — Dahua JSON-RPC Protocol")
    logger.info("=" * 60)
    logger.info(f"Device: {DEVICE_URL}")
    logger.info(f"Users: {json.dumps(USERS, indent=2)}")

    rpc = DahuaRPC(DEVICE_IP, DEVICE_USER, DEVICE_PASS)
    await rpc.connect()

    # Step 1: Login
    if not await rpc.login():
        logger.error("Failed to login! Trying alternative endpoints...")

        # Try /RPC2 directly (some devices don't use /RPC2_Login)
        for endpoint in ["/RPC2", "/RPC", "/OutsideCmd"]:
            logger.info(f"Trying endpoint: {endpoint}")
            resp = await rpc._rpc(
                "global.login",
                {"userName": DEVICE_USER, "password": DEVICE_PASS, "clientType": "Web3.0"},
                endpoint=endpoint,
            )
            if resp:
                logger.info(f"  Response: {json.dumps(resp)[:300]}")
                if resp.get("result"):
                    rpc.session_id = resp.get("session")
                    logger.info(f"  Login via {endpoint} successful!")
                    break

    if not rpc.session_id:
        logger.error("All login attempts failed. Trying unauthenticated requests...")

    # Step 2: List available services to discover the right API
    services = await rpc.list_services()

    # Step 3: Try to find records
    now = datetime.now(IST)
    start = now.strftime("%Y-%m-%d 00:00:00")
    end = now.strftime("%Y-%m-%d 23:59:59")

    records = await rpc.search_records(start, end)
    if records:
        logger.info(f"Found {len(records)} attendance records!")
        for r in records:
            logger.info(f"  Record: {json.dumps(r)[:200]}")
    else:
        logger.warning("No records found via RecordFinder. Trying alternatives...")
        await rpc.try_alternative_apis()

    # Step 3: If we found a working method, start polling loop
    if records:
        logger.info(f"\nStarting polling loop (every {POLL_INTERVAL}s)...")
        last_event_time = ""
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            now = datetime.now(IST)
            start = now.strftime("%Y-%m-%d 00:00:00")
            end = now.strftime("%Y-%m-%d 23:59:59")
            new_records = await rpc.search_records(start, end)
            for r in new_records:
                event_id = f"{r.get('UserID', '')}-{r.get('Time', '')}"
                if event_id not in _seen_events:
                    _seen_events.add(event_id)
                    pin = str(r.get("UserID", ""))
                    ts = r.get("Time", "")
                    await _process_attendance(pin, ts)
    else:
        logger.info("\nCould not find records API. Share this output for debugging.")

    await rpc.close()


if __name__ == "__main__":
    asyncio.run(main())
