"""Discover TrueFace record query API — round 4.
Focus on AccessOpenDoorRecManager (exists but params wrong) and JS analysis."""
import httpx, hashlib, json

c = httpx.Client(timeout=10)

# Login
r = c.post('http://192.168.1.112/RPC2_Login', json={
    'method': 'global.login',
    'params': {'userName': 'admin', 'password': '', 'clientType': 'Web3.0'},
    'id': 1
}).json()
s = r['session']
realm = r['params']['realm']
rand = r['params']['random']
ha1 = hashlib.md5(f'admin:{realm}:tipl9910'.encode()).hexdigest().upper()
auth = hashlib.md5(f'admin:{rand}:{ha1}'.encode()).hexdigest().upper()
r2 = c.post('http://192.168.1.112/RPC2_Login', json={
    'method': 'global.login',
    'params': {'userName': 'admin', 'password': auth, 'clientType': 'Web3.0',
               'loginType': 'Direct', 'authorityType': 'Default'},
    'id': 2, 'session': s
}).json()
sid = r2['session']
print(f"Login OK, session={sid}")

rid = [2]


def rpc(method, params=None):
    rid[0] += 1
    payload = {'method': method, 'id': rid[0], 'session': sid}
    if params is not None:
        payload['params'] = params
    try:
        resp = c.post('http://192.168.1.112/RPC2', json=payload).json()
        return resp
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def show(label, resp):
    if not resp:
        print(f"  {label} -> NO RESPONSE")
        return
    ok = resp.get('result')
    err = resp.get('error', {}).get('message', '')
    p = resp.get('params', {})
    tag = f'OK (result={ok})' if ok else f'FAIL({err})'
    print(f"  {label} -> {tag}")
    if ok or (ok is not False and ok is not None):
        print(f"    FULL: {json.dumps(resp)[:800]}")


# Test 1: AccessOpenDoorRecManager with many param variations
# (It returned empty error before = method exists but params wrong)
print("\n=== Test 1: AccessOpenDoorRecManager.startFind (param variations) ===")
conditions = [
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Doors": [0]}},
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Channels": [0]}},
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Type": "All"}},
    {"Condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"}},
    {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"},
    {"condition": {"BeginTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"}},
]
for i, cond in enumerate(conditions):
    r = rpc('AccessOpenDoorRecManager.startFind', cond)
    show(f'Format {i+1}', r)
    if r and r.get('result'):
        p = r.get('params', {})
        token = p.get('Token', p.get('token'))
        if not token:
            token = r['result'] if isinstance(r['result'], int) else None
        if token:
            r2 = rpc('AccessOpenDoorRecManager.doFind', {'Token': token, 'Count': 5})
            show('  doFind', r2)
            rpc('AccessOpenDoorRecManager.stopFind', {'Token': token})

# Test 2: log.startFind returning attendance-type records
# Previous test showed system logs. Try with specific log types
print("\n=== Test 2: log.startFind with different log types ===")
log_types = [
    ['AccessControl'], ['FaceRecognition'], ['DoorOpen'],
    ['Attendance'], ['AccessDoor'], ['AccessVerify'],
]
for lt in log_types:
    cond = {'condition': {
        'StartTime': '2026-05-22 00:00:00',
        'EndTime': '2026-05-22 23:59:59',
        'Types': lt,
    }}
    r = rpc('log.startFind', cond)
    if r and r.get('result'):
        token = r.get('params', {}).get('token')
        if token:
            r2 = rpc('log.getCount', {'token': token})
            count = r2.get('params', {}).get('count', 0) if r2 else 0
            print(f"  Types={lt}: count={count}")
            if count > 0:
                r3 = rpc('log.doFind', {'token': token, 'count': 3})
                if r3:
                    items = r3.get('params', {}).get('items', [])
                    for item in (items or [])[:2]:
                        print(f"    {json.dumps(item)[:200]}")
            rpc('log.stopFind', {'token': token})
        else:
            print(f"  Types={lt}: no token returned")
    else:
        err = r.get('error', {}).get('message', '') if r else 'no response'
        print(f"  Types={lt}: FAIL({err})")

# Test 3: Search vendor.js for more API clues
print("\n=== Test 3: Searching device JS files ===")
# Download vendor.js and search for record-related patterns
try:
    vjs = c.get('http://192.168.1.112/static/js/vendor.035771e22764c9e10669.js', timeout=15).text
    print(f"  vendor.js downloaded ({len(vjs)} chars)")
    import re
    # Search for RPC method calls related to records/attendance
    patterns = re.findall(r'"((?:Access|Record|log|Attendance)[A-Za-z]*\.[a-zA-Z]+)"', vjs)
    unique = sorted(set(patterns))
    if unique:
        print(f"  Found {len(unique)} methods in vendor.js:")
        for m in unique:
            print(f"    {m}")
except Exception as e:
    print(f"  Could not download vendor.js: {e}")

# Also search app.js for the SearchRecord/query page component
try:
    with open('trueface_app.js', 'r', errors='ignore') as f:
        ajs = f.read()
    # Look for the Search Records page handler
    import re
    # Find context around "SearchRecord" or "searchRecord" or "query" near "record"
    for pat in [r'.{0,100}SearchRecord.{0,100}', r'.{0,100}searchRecord.{0,100}',
                r'.{0,100}queryRecord.{0,100}', r'.{0,100}RecordQuery.{0,100}',
                r'.{0,50}startFind.{0,200}condition.{0,100}']:
        matches = re.findall(pat, ajs, re.IGNORECASE)
        for m in matches[:3]:
            print(f"  JS: ...{m[:200]}...")
except Exception as e:
    print(f"  Could not search app.js: {e}")

print("\nDone!")
