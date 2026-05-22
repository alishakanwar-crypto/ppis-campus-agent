"""Discover TrueFace AccessAttendance API query format."""
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


# Try different condition formats for AccessAttendance.startFind
conditions = [
    # Format 1: flat condition
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"}},
    # Format 2: with Channels
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Channels": [0]}},
    # Format 3: with Doors
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Doors": [0]}},
    # Format 4: without condition wrapper
    {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"},
    # Format 5: with Type
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Type": "All"}},
    # Format 6: with UserID
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "UserID": ""}},
    # Format 7: nested QueryCondition
    {"QueryCondition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59"}},
    # Format 8: with Order
    {"condition": {"StartTime": "2026-05-22 00:00:00", "EndTime": "2026-05-22 23:59:59", "Order": "Descend"}},
]

print("\n=== Testing AccessAttendance.startFind with different conditions ===")
for i, cond in enumerate(conditions):
    r = rpc('AccessAttendance.startFind', cond)
    if r:
        ok = r.get('result', False)
        p = r.get('params', {})
        token = p.get('Token', p.get('token'))
        total = p.get('Total', p.get('total', '?'))
        err = r.get('error', {}).get('message', '')
        print(f"  Format {i+1}: result={ok} Token={token} Total={total} err={err}")

        if ok and token:
            # Try doFind
            r2 = rpc('AccessAttendance.doFind', {'Token': token, 'Count': 10})
            if r2:
                ok2 = r2.get('result', False)
                p2 = r2.get('params', {})
                found = p2.get('Found', p2.get('found', 0))
                records = p2.get('Records', p2.get('records', []))
                print(f"    doFind: result={ok2} found={found} records={len(records)}")
                for rec in records[:3]:
                    print(f"      {json.dumps(rec)[:300]}")
            # Cleanup
            rpc('AccessAttendance.stopFind', {'Token': token})

# Also try AccessOpenDoorRecManager with different formats
print("\n=== Testing AccessOpenDoorRecManager.startFind ===")
for i, cond in enumerate(conditions[:4]):
    r = rpc('AccessOpenDoorRecManager.startFind', cond)
    if r:
        ok = r.get('result', False)
        p = r.get('params', {})
        err = r.get('error', {}).get('message', '')
        print(f"  Format {i+1}: result={ok} params={json.dumps(p)[:200]} err={err}")
        if ok:
            token = p.get('Token', p.get('token'))
            if token:
                r2 = rpc('AccessOpenDoorRecManager.doFind', {'Token': token, 'Count': 10})
                if r2:
                    print(f"    doFind: {json.dumps(r2.get('params',{}))[:500]}")
                rpc('AccessOpenDoorRecManager.stopFind', {'Token': token})

# Try to search the JS for clues
print("\n=== Checking trueface_app.js for SearchRecord API calls ===")
try:
    with open('trueface_app.js', 'r', errors='ignore') as f:
        js = f.read()
    # Search for attendance/record related method calls
    import re
    patterns = [
        r'["\']([A-Za-z]+\.(?:startFind|doFind|stopFind|factory|getRecordCount|search|query))["\']',
        r'["\']([A-Za-z]*(?:Record|Attendance|Event|Log)[A-Za-z]*\.[a-zA-Z]+)["\']',
        r'startFind.*?condition',
    ]
    found_methods = set()
    for pat in patterns:
        for m in re.finditer(pat, js):
            found_methods.add(m.group(0)[:100])
    if found_methods:
        print("  Found in JS:")
        for m in sorted(found_methods):
            print(f"    {m}")
    else:
        print("  No specific record methods found in app.js")
except FileNotFoundError:
    print("  trueface_app.js not found")

print("\nDone!")
