"""Discover TrueFace device API endpoints for attendance records."""
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


def rpc(method, params=None, rid=3):
    payload = {'method': method, 'id': rid, 'session': sid}
    if params is not None:
        payload['params'] = params
    try:
        resp = c.post('http://192.168.1.112/RPC2', json=payload).json()
        ok = resp.get('result', False)
        err = resp.get('error', {}).get('message', '')
        p = resp.get('params', {})
        tag = 'OK' if ok else f'FAIL({err})'
        print(f"  {method} -> {tag}")
        if ok:
            print(f"    params={json.dumps(p)[:500]}")
        return resp
    except Exception as e:
        print(f"  {method} -> ERROR: {e}")
        return None


cond = {
    'condition': {
        'StartTime': '2026-05-22 00:00:00',
        'EndTime': '2026-05-22 23:59:59',
    }
}

print("\n=== Testing startFind/doFind pattern ===")
services = [
    'AccessOpenDoorRecManager', 'Attendance', 'AccessAttendance',
    'AnyDetectDataFinder', 'AccessAlarmRecordManager',
    'AccessAuthorizeRecord', 'AccessAuthorizeFailRecord',
]
for svc in services:
    r = rpc(f'{svc}.startFind', cond, rid=10)
    if r and r.get('result'):
        token = r.get('params', {}).get('token') or r.get('params', {}).get('object')
        print(f"    >>> startFind SUCCESS! token={token}")
        if token:
            r2 = rpc(f'{svc}.doFind', {'token': token, 'count': 10}, rid=11)
            if r2:
                print(f"    doFind: {json.dumps(r2.get('params',{}))[:500]}")
            rpc(f'{svc}.stopFind', {'token': token}, rid=12)

print("\n=== Testing getCount pattern ===")
for svc in services:
    rpc(f'{svc}.getCount', cond, rid=20)
    rpc(f'{svc}.getRecordCount', cond, rid=21)

print("\n=== Testing object-based finder ===")
for name in ['AccessControlCardRec', 'TrafficEventDetail', 'AccessEvent',
             'AttendanceRecord', 'AccessOpenDoorRecord', 'default']:
    r = rpc('RecordFinder.create', {'name': name}, rid=30)
    r = rpc('recordFinder.factory', {'name': name}, rid=31)

print("\n=== Testing configManager for record types ===")
for name in ['AccessOpenDoorRecord', 'AttendanceLog', 'EventLog', 'AccessRecord']:
    rpc('configManager.getConfig', {'name': name}, rid=40)

print("\n=== Testing direct query methods ===")
rpc('global.getCurrentTime', None, rid=50)
rpc('magicBox.getDeviceType', None, rid=51)
rpc('magicBox.getSerialNo', None, rid=52)

print("\nDone! Share this output.")
