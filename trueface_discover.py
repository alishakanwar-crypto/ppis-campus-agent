"""Discover TrueFace record query API — round 5.
Key insight from vendor.js: Dahua RPC uses 'object' field at root level!
RecordFinder calls need: {"method":"RecordFinder.startFind","params":{...},"object":N}
"""
import httpx, hashlib, json, re

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


def rpc(method, params=None, object_id=None):
    rid[0] += 1
    payload = {'method': method, 'id': rid[0], 'session': sid}
    if params is not None:
        payload['params'] = params
    if object_id is not None:
        payload['object'] = object_id
    try:
        resp = c.post('http://192.168.1.112/RPC2', json=payload).json()
        return resp
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


cond = {'condition': {
    'StartTime': '2026-05-22 00:00:00',
    'EndTime': '2026-05-22 23:59:59',
}}

# Test 1: RecordFinder.factory with object field
print("=== Test 1: RecordFinder.factory WITH object field ===")
for obj_id in [0, 1, None]:
    for name in ['AccessControlCardRec', 'TrafficSnapRecord']:
        r = rpc('RecordFinder.factory', {'name': name}, object_id=obj_id)
        ok = r.get('result') if r else None
        err = r.get('error', {}).get('message', '') if r else ''
        print(f"  factory(name={name}, object={obj_id}) -> result={ok} err={err}")
        if r and r.get('result') and isinstance(r['result'], int):
            obj = r['result']
            print(f"    >>> Got object ID: {obj}")

# Test 2: RecordFinder.startFind directly with object field
print("\n=== Test 2: RecordFinder.startFind with object=0,1,2 ===")
for obj_id in [0, 1, 2, 3]:
    r = rpc('RecordFinder.startFind', cond, object_id=obj_id)
    ok = r.get('result') if r else None
    err = r.get('error', {}).get('message', '') if r else ''
    p = r.get('params', {}) if r else {}
    print(f"  startFind(object={obj_id}) -> result={ok} params={p} err={err}")
    if ok:
        print(f"    FULL: {json.dumps(r)[:500]}")
        # Try getQuerySize and doFind
        r2 = rpc('RecordFinder.getQuerySize', None, object_id=obj_id)
        if r2:
            print(f"    getQuerySize: {json.dumps(r2)[:300]}")
        r3 = rpc('RecordFinder.doSeekFind', {'count': 5, 'offset': 0}, object_id=obj_id)
        if r3:
            print(f"    doSeekFind: {json.dumps(r3)[:500]}")
        rpc('RecordFinder.stopFind', None, object_id=obj_id)

# Test 3: Search vendor.js for factory/instance creation
print("\n=== Test 3: Searching vendor.js for instance creation ===")
try:
    vjs = c.get('http://192.168.1.112/static/js/vendor.035771e22764c9e10669.js', timeout=15).text
    # Find how RecordFinder instances are created
    # Search around "RecordFinder" for factory/instance patterns
    for pat in [
        r'instance\(\{name:\s*\w+\}\)\.then.{0,300}RecordFinder',
        r'RecordFinder.{0,20}factory.{0,200}',
        r'\.instance\s*=\s*function.{0,300}',
        r'"([A-Za-z]+\.factory)"',
        r'"([A-Za-z]+\.instance)"',
        r'object:\s*\w+\.result.{0,100}RecordFinder',
        r'RecordFinder.{0,500}',
    ]:
        matches = re.findall(pat, vjs)
        if matches:
            print(f"\n  Pattern: {pat[:50]}")
            for m in matches[:5]:
                text = m if isinstance(m, str) else str(m)
                print(f"    {text[:300]}")
except Exception as e:
    print(f"  Error: {e}")

# Test 4: AccessOpenDoorRecManager with object field
print("\n=== Test 4: AccessOpenDoorRecManager with object field ===")
for obj_id in [0, 1]:
    r = rpc('AccessOpenDoorRecManager.startFind', cond, object_id=obj_id)
    ok = r.get('result') if r else None
    err = r.get('error', {}).get('message', '') if r else ''
    p = r.get('params', {}) if r else {}
    print(f"  startFind(object={obj_id}) -> result={ok} params={p} err={err}")
    if ok:
        token = p.get('Token', p.get('token'))
        if token:
            r2 = rpc('AccessOpenDoorRecManager.doFind', {'Token': token, 'Count': 5}, object_id=obj_id)
            print(f"    doFind: {json.dumps(r2)[:500]}" if r2 else "    doFind: None")
            rpc('AccessOpenDoorRecManager.stopFind', {'Token': token}, object_id=obj_id)

print("\nDone!")
