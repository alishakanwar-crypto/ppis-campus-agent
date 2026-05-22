"""
TrueFace 3000 Attendance Notifier.

Connects to the TrueFace device's real-time event stream and CGI API.
When a face is recognized (access granted), sends a WhatsApp notification.

Flow: Face recognized → Access Granted → This script catches event → WhatsApp sent

Run: python trueface_poller.py
"""
import hashlib
import logging
import json
import re
import time
import httpx
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("trueface")

IST = timezone(timedelta(hours=5, minutes=30))
DEVICE_IP = "192.168.1.112"
DEVICE_USER = "admin"
DEVICE_PASS = "tipl9910"
CLOUD_API = "https://ppis-whatsapp-bot.fly.dev/api/send-whatsapp"

USERS_FILE = "trueface_users.json"
try:
    with open(USERS_FILE) as f:
        USERS = json.load(f)
    logger.info(f"Loaded {len(USERS)} users from {USERS_FILE}")
except Exception:
    USERS = {}
    logger.warning(f"No {USERS_FILE} found")

_notified_today = {}
_last_date = ""


def _check_dedup(pin):
    global _last_date, _notified_today
    today = datetime.now(IST).strftime("%Y-%m-%d")
    if today != _last_date:
        _notified_today.clear()
        _last_date = today
    if _notified_today.get(pin) == today:
        return True  # already notified
    now = datetime.now(IST)
    if now.weekday() in (5, 6):
        logger.info("Weekend — skipping notification")
        return True
    return False


def _send_whatsapp(name, phone, time_str):
    try:
        resp = httpx.post(CLOUD_API, json={
            "to": phone,
            "template_name": "ppis_teacher_present_text",
            "language_code": "en",
            "body_params": [name, time_str],
        }, timeout=30)
        logger.info(f"WhatsApp -> {phone}: {resp.status_code} {resp.text[:100]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"WhatsApp error: {e}")
        return False


def _process_event(user_id, timestamp=None):
    pin = str(user_id)
    user = USERS.get(pin)
    if not user:
        logger.warning(f"Unknown user ID: {pin}")
        return
    name = user["name"]
    phone = user.get("phone", "")
    if not phone:
        logger.warning(f"No phone for {name}")
        return
    if _check_dedup(pin):
        logger.info(f"Already notified {name} today")
        return
    if timestamp:
        try:
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%I:%M %p")
        except (ValueError, TypeError):
            time_str = datetime.now(IST).strftime("%I:%M %p")
    else:
        time_str = datetime.now(IST).strftime("%I:%M %p")

    logger.info(f">>> ATTENDANCE: {name} at {time_str} — sending WhatsApp to {phone}")
    _notified_today[pin] = datetime.now(IST).strftime("%Y-%m-%d")
    _send_whatsapp(name, phone, time_str)


def dahua_digest_auth():
    """Create httpx client with Dahua digest authentication."""
    return httpx.DigestAuth(DEVICE_USER, DEVICE_PASS)


def try_cgi_event_stream():
    """Listen to real-time events via CGI event manager (Server-Sent Events).
    This gives instant notifications when access is granted."""
    url = f"http://{DEVICE_IP}/cgi-bin/eventManager.cgi"
    auth = dahua_digest_auth()

    # Subscribe to ALL events to discover what the device sends
    codes = "All"
    try:
        logger.info(f"Connecting to event stream: codes=[{codes}]...")
        with httpx.stream(
            "GET",
            f"{url}?action=attach&codes=[{codes}]&heartbeat=5",
            auth=auth,
            timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
        ) as response:
            logger.info(f"Event stream response: {response.status_code}")
            if response.status_code != 200:
                logger.warning(f"Event stream returned {response.status_code}")
                return False

            logger.info("Connected! Waiting for events (show face to device)...")
            event_buffer = []
            for line in response.iter_lines():
                line = line.strip()
                if not line:
                    if event_buffer:
                        full_event = "\n".join(event_buffer)
                        if "Heartbeat" not in full_event:
                            logger.info(f">>> EVENT:\n{full_event[:800]}")
                            _parse_event_block(full_event)
                        event_buffer = []
                    continue
                event_buffer.append(line)
                if "Heartbeat" not in line and "--myboundary" not in line \
                        and "Content-Type" not in line and "Content-Length" not in line:
                    logger.info(f"EVENT LINE: {line[:500]}")
            return True

    except httpx.TimeoutException:
        logger.warning("Event stream timeout")
    except Exception as e:
        logger.warning(f"Event stream error: {e}")

    return False


def _parse_event_block(block):
    """Parse a multipart event block from Dahua CGI event stream."""
    # Try JSON first
    json_match = re.search(r'\{.*\}', block, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            logger.info(f"Parsed JSON event: {json.dumps(data)[:500]}")
            code = data.get("Code", "")
            action = data.get("Action", data.get("action", ""))
            info = data.get("Data", data.get("data", {}))
            if isinstance(info, dict):
                user_id = info.get("UserID", info.get("userId", ""))
                timestamp = info.get("Time", info.get("time", ""))
                if user_id:
                    _process_event(user_id, timestamp)
            return
        except json.JSONDecodeError:
            pass

    # Try key=value format
    if "UserID=" in block or "UserID\":" in block:
        m = re.search(r'UserID[=:"\s]+(\d+)', block)
        if m:
            user_id = m.group(1)
            tm = re.search(r'Time[=:"\s]+([\d-]+ [\d:]+)', block)
            timestamp = tm.group(1) if tm else None
            _process_event(user_id, timestamp)

    # Log Code if present
    code_match = re.search(r'Code[=:]+(\w+)', block)
    if code_match:
        logger.info(f"Event code: {code_match.group(1)}")


def try_cgi_records():
    """Try CGI-based record finder as fallback."""
    base = f"http://{DEVICE_IP}/cgi-bin/recordFinder.cgi"
    auth = dahua_digest_auth()

    # Step 1: Create a record finder instance
    names = ["AccessControlCardRec", "TrafficSnapRecord", "AccessOpenDoorRecord"]
    for name in names:
        try:
            r = httpx.get(f"{base}?action=factory.create&name={name}", auth=auth, timeout=10)
            logger.info(f"CGI factory({name}): {r.status_code} -> {r.text[:200]}")
            if r.status_code == 200 and "result=" in r.text:
                # Extract object ID
                m = re.search(r'result=(\d+)', r.text)
                if m:
                    obj = m.group(1)
                    logger.info(f"  Created RecordFinder object: {obj}")
                    return _cgi_find_records(base, auth, obj)
        except Exception as e:
            logger.warning(f"CGI factory({name}) error: {e}")

    return False


def _cgi_find_records(base, auth, obj):
    """Use CGI record finder to get today's records."""
    now = datetime.now(IST)
    start = now.strftime("%Y-%m-%d%%2000:00:00")
    end = now.strftime("%Y-%m-%d%%2023:59:59")

    try:
        # Start find
        r = httpx.get(
            f"{base}?action=startFind&object={obj}"
            f"&condition.StartTime={start}&condition.EndTime={end}",
            auth=auth, timeout=10
        )
        logger.info(f"CGI startFind: {r.status_code} -> {r.text[:200]}")

        # Get count
        r = httpx.get(f"{base}?action=getQuerySize&object={obj}", auth=auth, timeout=10)
        logger.info(f"CGI getQuerySize: {r.status_code} -> {r.text[:200]}")

        # Do find
        r = httpx.get(f"{base}?action=doFind&object={obj}&count=100", auth=auth, timeout=10)
        logger.info(f"CGI doFind: {r.status_code} -> {r.text[:500]}")

        if r.status_code == 200:
            # Parse records from response
            records = _parse_cgi_records(r.text)
            logger.info(f"Found {len(records)} records via CGI")
            return records

    except Exception as e:
        logger.error(f"CGI find error: {e}")

    return []


def _parse_cgi_records(text):
    """Parse CGI record finder response into record list."""
    records = []
    current = {}
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if '=' in line:
            key, _, val = line.partition('=')
            current[key.strip()] = val.strip()
    if current:
        records.append(current)
    return records


def try_rpc_approach():
    """Use Dahua JSON-RPC API as another fallback — poll via AccessAttendance."""
    logger.info("Trying JSON-RPC approach...")

    client = httpx.Client(timeout=10)

    # Login
    r = client.post(f'http://{DEVICE_IP}/RPC2_Login', json={
        'method': 'global.login',
        'params': {'userName': DEVICE_USER, 'password': '', 'clientType': 'Web3.0'},
        'id': 1
    }).json()

    session = r.get('session', '')
    realm = r.get('params', {}).get('realm', '')
    random = r.get('params', {}).get('random', '')

    if not realm:
        logger.error("RPC login failed — no challenge")
        return None

    ha1 = hashlib.md5(f'{DEVICE_USER}:{realm}:{DEVICE_PASS}'.encode()).hexdigest().upper()
    auth_resp = hashlib.md5(f'{DEVICE_USER}:{random}:{ha1}'.encode()).hexdigest().upper()

    r2 = client.post(f'http://{DEVICE_IP}/RPC2_Login', json={
        'method': 'global.login',
        'params': {'userName': DEVICE_USER, 'password': auth_resp,
                   'clientType': 'Web3.0', 'loginType': 'Direct', 'authorityType': 'Default'},
        'id': 2, 'session': session
    }).json()

    if not r2.get('result'):
        logger.error("RPC login failed")
        return None

    sid = r2['session']
    logger.info(f"RPC login OK, session={sid}")
    return client, sid


def rpc_poll_loop(client, sid):
    """Poll for new records via RPC."""
    seen = set()
    rid = 2

    while True:
        try:
            now = datetime.now(IST)
            start = now.strftime("%Y-%m-%d 00:00:00")
            end = now.strftime("%Y-%m-%d 23:59:59")

            rid += 1
            r = client.post(f'http://{DEVICE_IP}/RPC2', json={
                'method': 'AccessAttendance.startFind',
                'params': {'condition': {'StartTime': start, 'EndTime': end}},
                'id': rid, 'session': sid
            }).json()

            if r.get('result'):
                token = r.get('params', {}).get('Token')
                if token:
                    rid += 1
                    r2 = client.post(f'http://{DEVICE_IP}/RPC2', json={
                        'method': 'AccessAttendance.doFind',
                        'params': {'Token': token, 'Count': 100},
                        'id': rid, 'session': sid
                    }).json()

                    records = r2.get('params', {}).get('Records', [])
                    for rec in records:
                        uid = rec.get('UserID', '')
                        ts = rec.get('Time', '')
                        key = f"{uid}-{ts}"
                        if key not in seen:
                            seen.add(key)
                            _process_event(uid, ts)

                    rid += 1
                    client.post(f'http://{DEVICE_IP}/RPC2', json={
                        'method': 'AccessAttendance.stopFind',
                        'params': {'Token': token},
                        'id': rid, 'session': sid
                    })

        except Exception as e:
            logger.error(f"RPC poll error: {e}")

        time.sleep(10)


def main():
    logger.info("=" * 50)
    logger.info("TrueFace 3000 Attendance Notifier")
    logger.info("=" * 50)
    logger.info(f"Device: {DEVICE_IP}")
    logger.info(f"Users: {json.dumps(USERS, indent=2)}")
    logger.info("")

    # Method 1: Real-time event stream (best — instant notifications)
    logger.info("=== Method 1: Real-time event stream ===")
    if try_cgi_event_stream():
        return  # This blocks forever listening to events

    # Method 2: CGI record finder
    logger.info("\n=== Method 2: CGI record finder ===")
    records = try_cgi_records()
    if records:
        logger.info(f"CGI found {len(records)} records")
        for rec in records:
            uid = rec.get('UserID', rec.get('records[0].UserID', ''))
            ts = rec.get('Time', rec.get('records[0].Time', ''))
            if uid:
                _process_event(uid, ts)

    # Method 3: RPC polling
    logger.info("\n=== Method 3: RPC polling ===")
    result = try_rpc_approach()
    if result:
        client, sid = result
        logger.info("Starting RPC poll loop (every 10 seconds)...")
        rpc_poll_loop(client, sid)


if __name__ == "__main__":
    main()
