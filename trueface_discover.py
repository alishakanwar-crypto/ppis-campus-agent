"""Discover TrueFace record query API — round 3."""
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
        print(f"    FULL: {json.dumps(resp)[:600]}")


cond = {
    'condition': {
        'StartTime': '2026-05-22 00:00:00',
        'EndTime': '2026-05-22 23:59:59',
    }
}

# Test 1: log.startFind (from JS — different from LogManager.startFind)
print("\n=== Test 1: log.startFind ===")
r = rpc('log.startFind', cond)
show('log.startFind', r)
if r and r.get('result'):
    token = r['result'] if isinstance(r['result'], int) else r.get('params', {}).get('Token')
    print(f"  Token: {token}")
    if token:
        r2 = rpc('log.getCount', {'token': token})
        show('log.getCount', r2)
        r3 = rpc('log.doFind', {'token': token, 'count': 10})
        show('log.doFind', r3)
        rpc('log.stopFind', {'token': token})

# Test 2: log.startFind with Types
print("\n=== Test 2: log.startFind with Types ===")
for types in [['All'], ['Access'], ['Alarm'], ['Event']]:
    cond2 = {'condition': {
        'StartTime': '2026-05-22 00:00:00',
        'EndTime': '2026-05-22 23:59:59',
        'Types': types,
    }}
    r = rpc('log.startFind', cond2)
    show(f'log.startFind Types={types}', r)
    if r and r.get('result'):
        token = r['result'] if isinstance(r['result'], int) else r.get('params', {}).get('Token')
        if token:
            r2 = rpc('log.getCount', {'token': token})
            show(f'  log.getCount', r2)
            r3 = rpc('log.doFind', {'token': token, 'count': 5})
            show(f'  log.doFind', r3)
            rpc('log.stopFind', {'token': token})
        break  # Stop after first success

# Test 3: AccessAttendance.list
print("\n=== Test 3: AccessAttendance.list ===")
r = rpc('AccessAttendance.list', None)
show('AccessAttendance.list (no params)', r)
r = rpc('AccessAttendance.list', {'count': 10})
show('AccessAttendance.list (count=10)', r)
r = rpc('AccessAttendance.list', {'UserID': '1'})
show('AccessAttendance.list (UserID=1)', r)

# Test 4: RecordFinder instance pattern from JS
# JS: this.instance({name:e}).then(...startFind...)
print("\n=== Test 4: RecordFinder instance pattern ===")
for name in ['AccessControlCardRec', 'trafficSnapRecord', 'AccessOpenDoorRecord']:
    # Try creating instance via different method names
    for factory in ['RecordFinder.factory', 'RecordFinder.create', 'RecordFinder.instance']:
        r = rpc(factory, {'name': name})
        if r and r.get('result'):
            show(f'{factory}({name})', r)
            obj = r['result']
            r2 = rpc(f'{obj}.startFind', cond)
            show(f'  {obj}.startFind', r2)
            if r2 and r2.get('result'):
                r3 = rpc(f'{obj}.doFind', {'count': 5})
                show(f'  {obj}.doFind', r3)
            rpc(f'{obj}.destroy', None)
            break

# Test 5: Direct record search via Attendance service
print("\n=== Test 5: Attendance service methods ===")
show('Attendance.webStatis', rpc('Attendance.webStatis', cond))
show('Attendance.getCheckGroup', rpc('Attendance.getCheckGroup', None))

# Test 6: Try the exact JS pattern for Search Records page
# The JS shows the page uses D.AccessAttendance or similar
print("\n=== Test 6: Search pattern from JS (instance-based) ===")
# Maybe AccessAttendance needs instance creation first
for method in ['AccessAttendance.instance', 'AccessAttendance.create', 'AccessAttendance.factory']:
    r = rpc(method, {'name': 'default'})
    show(method, r)
    if r and r.get('result'):
        obj = r['result']
        r2 = rpc(f'{obj}.startFind', cond)
        show(f'  {obj}.startFind', r2)

print("\nDone!")
