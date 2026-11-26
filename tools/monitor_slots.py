import json, time, urllib.request

def get(path):
    req = urllib.request.Request('http://127.0.0.1:8965' + path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as r:
        return json.loads(r.read().decode('utf-8'))

end = time.time() + 600
last = None
while time.time() < end:
    try:
        st = get('/state')['state']
        night = st.get('night') or {}
        slots = {s['label']: s['count'] for s in get('/debug/slots').get('slots', [])}
        line = (f"store={st['store_state']} outside={night.get('is_outside')} "
                f"loc={night.get('current_location')} att={night.get('scavenging_attempts')} "
                f"inv={len(st['inventory']['owned'])} slots={slots}")
        if line != last:
            print(time.strftime('%H:%M:%S'), line, flush=True)
            last = line
    except Exception as e:
        print('ERR', e, flush=True)
    time.sleep(6)
