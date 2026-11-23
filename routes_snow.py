#!/usr/bin/env python3
"""
Snow-plowing vertical: geocoding, route optimization, route runs, GPS tracking, client ETA.
Imported by server.py. All data lives on the customer record in customers.json.
Free stack: Nominatim (geocode) + OSRM (durations). No API keys.
"""
import json, time, math, itertools, urllib.request, urllib.parse

NOMINATIM = 'https://nominatim.openstreetmap.org/search'
OSRM_TABLE = 'https://router.project-osrm.org/table/v1/driving/'
UA = {'User-Agent': 'anosagent-routes/1.0'}

COMPANY_TYPES = ['tree', 'snow', 'landscaping', 'general']
DEFAULT_SERVICE_MIN = 15
# routes_snow module

# ---------- storage helpers ----------
def company_type(c):
    return c.get('company_type', 'tree')

def set_company_type(c, t):
    if t not in COMPANY_TYPES:
        raise ValueError('bad company type')
    c['company_type'] = t

TRUCKS = {'A': 'Truck 1', 'B': 'Truck 2'}

def _route_runs(c):
    """Multi-truck runs: {'A': run, 'B': run}. Migrates legacy single route_run on first touch."""
    runs = c.setdefault('route_runs', {})
    legacy = c.pop('route_run', None)
    if legacy and 'A' not in runs:
        runs['A'] = legacy
    for k in TRUCKS:
        runs.setdefault(k, {'active': False, 'id': None, 'date': '', 'started_ts': 0,
                            'plow_pos': None, 'stops': [], 'truck': k})
        runs[k].setdefault('truck', k)
    return runs

def _route_run(c, truck='A'):
    return _route_runs(c).get(truck if truck in TRUCKS else 'A')

def _active_truck(c):
    """Truck key with an active run, preferring A."""
    runs = _route_runs(c)
    for k in TRUCKS:
        if runs[k].get('active'):
            return k
    return 'A'

def driver_tokens(c):
    """Per-truck tracker tokens: {'A': tok, 'B': tok} — minted lazily, stable."""
    toks = c.setdefault('driver_tokens', {})
    import secrets as _s
    legacy = c.pop('driver_token', None)
    if legacy and 'A' not in toks:
        toks['A'] = legacy
    for k in TRUCKS:
        if not toks.get(k):
            toks[k] = _s.token_hex(16)
    return toks

def driver_token(c):
    """Back-compat: Truck 1's token."""
    return driver_tokens(c)['A']

def service_min(c, lead):
    """Plow time on site for a stop: per-stop override -> route default -> 15."""
    try:
        v = int((lead or {}).get('service_min') or 0)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    try:
        return int(c.get('route_service_min') or DEFAULT_SERVICE_MIN)
    except (TypeError, ValueError):
        return DEFAULT_SERVICE_MIN

def route_history(c):
    return c.setdefault('route_history', [])

def archive_run(c, leads, outcome='completed', truck='A'):
    """Snapshot the truck's run into route_history (storm log) and reset it. No-op without stops."""
    run = _route_run(c, truck)
    if not run.get('stops'):
        run.update({'active': False, 'id': None, 'plow_pos': None})
        return None
    entry = {
        'id': run.get('id'), 'date': run.get('date') or time.strftime('%Y-%m-%d'),
        'started_ts': run.get('started_ts') or 0, 'ended_ts': time.time(),
        'outcome': outcome, 'truck': truck,
        'planned_drive_min': run.get('planned_drive_min'),
        'planned_service_min': run.get('planned_service_min'),
        'stops': [],
    }
    for s in run['stops']:
        l = _lead_by_id(leads, s.get('lead_id')) or {}
        entry['stops'].append({
            'lead_id': s.get('lead_id'), 'seq': s.get('seq'),
            'name': l.get('client') or l.get('name', ''), 'address': l.get('address', ''),
            'status': s.get('status'), 'done_ts': s.get('done_ts'),
            'service_min': s.get('service_min')})
    hist = route_history(c)
    hist.insert(0, entry)
    del hist[50:]
    run.update({'active': False, 'id': None, 'date': '', 'started_ts': 0,
                'plow_pos': None, 'stops': []})
    return entry

# ---------- geocoding ----------
def geocode(address):
    q = urllib.parse.urlencode({'q': address, 'format': 'json', 'limit': 1})
    req = urllib.request.Request(f'{NOMINATIM}?{q}', headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    if not d:
        return None
    return {'lat': float(d[0]['lat']), 'lon': float(d[0]['lon']), 'label': d[0].get('display_name', '')}

# ---------- OSRM duration matrix ----------
def _table(points):
    """points: [{lat,lon},...] -> durations matrix (seconds)"""
    coords = ';'.join(f"{p['lon']},{p['lat']}" for p in points)
    url = f'{OSRM_TABLE}{coords}?annotations=duration'
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.load(r)
    if d.get('code') != 'Ok':
        raise Exception('OSRM: ' + d.get('code', 'error'))
    return d['durations']

# ---------- sequencing ----------
def _nn_2opt(T, start_idx, idxs):
    """Nearest-neighbor + 2-opt on a duration matrix subset. Returns ordered idxs."""
    remaining = list(idxs)
    order = []
    cur = start_idx
    while remaining:
        nxt = min(remaining, key=lambda j: (T[cur][j] if T[cur][j] is not None else 1e9))
        order.append(nxt)
        remaining.remove(nxt)
        cur = nxt
    def total(o):
        t, cur = 0, start_idx
        for j in o:
            t += T[cur][j] or 1e9
            cur = j
        return t
    improved = True
    while improved:
        improved = False
        for i in range(len(order) - 1):
            for j in range(i + 1, len(order)):
                cand = order[:i] + order[i:j + 1][::-1] + order[j + 1:]
                if total(cand) < total(order) - 1:
                    order = cand
                    improved = True
    return order

def optimize(c, leads, truck=None):
    """Build optimized stop order from route-flagged, geocoded leads.
    Hard tier ordering: plow_priority 1 (Priority Service) before 2 (Essential) before 3.
    Within a tier: stops with a manual route_order (pinned by the user) stay in that
    relative order at the front of the tier; the rest are NN-2opt optimized after them.
    truck='A'/'B': only stops assigned to that truck (or unassigned). Stops already on
    another truck's ACTIVE run are always excluded."""
    busy = set()
    for k, r in _route_runs(c).items():
        if r.get('active'):
            busy.update(s.get('lead_id') for s in r.get('stops', []))
    stops = [l for l in leads if l.get('route_stop') and l.get('lat') and l.get('lon')
             and l.get('id') not in busy
             and (not truck or (l.get('route_truck') or 'A') == truck)]
    if len(stops) < 2:
        return {'error': 'need at least 2 geocoded route stops', 'stops': []}
    depot = {'lat': c.get('depot_lat') or stops[0]['lat'],
             'lon': c.get('depot_lon') or stops[0]['lon']}
    points = [depot] + [{'lat': s['lat'], 'lon': s['lon']} for s in stops]
    T = _table(points)
    order_idxs = []
    for tier in (1, 2, 3):
        tier_idxs = [i + 1 for i, s in enumerate(stops) if int(s.get('plow_priority') or 2) == tier]
        if not tier_idxs:
            continue
        # Split: manually-ordered stops first (sorted by route_order), then free stops
        pinned = sorted([i for i in tier_idxs if stops[i - 1].get('route_order') is not None],
                        key=lambda i: stops[i - 1]['route_order'])
        free = [i for i in tier_idxs if stops[i - 1].get('route_order') is None]
        start = 0 if not order_idxs else order_idxs[-1]
        if pinned:
            order_idxs.extend(pinned)
            start = pinned[-1]
        if free:
            order_idxs.extend(_nn_2opt(T, start, free))
    legs, cum, cur, svc_cum = [], 0, 0, 0
    for j in order_idxs:
        leg = (T[cur][j] or 0) / 60.0
        cum += leg
        smin = service_min(c, stops[j - 1])
        # ETA = drive so far + plow time spent at every earlier stop
        legs.append({'lead_id': stops[j - 1]['id'], 'drive_min': round(leg, 1),
                     'eta_offset_min': round(cum + svc_cum, 1), 'service_min': smin})
        svc_cum += smin
        cur = j
    return {'stops': [stops[j - 1]['id'] for j in order_idxs], 'legs': legs,
            'total_drive_min': round(cum, 1), 'total_service_min': round(svc_cum, 1),
            'stop_count': len(order_idxs)}

# ---------- route run lifecycle ----------
def start_run(c, leads, truck='A'):
    plan = optimize(c, leads, truck=truck)
    if plan.get('error'):
        return plan
    run = _route_run(c, truck)
    now = time.time()
    run.update({
        'active': True, 'id': f'RUN-{int(now)}', 'date': time.strftime('%Y-%m-%d'),
        'started_ts': now, 'plow_pos': None,
        'planned_drive_min': plan.get('total_drive_min'),
        'planned_service_min': plan.get('total_service_min'),
        'stops': [{'lead_id': lid, 'seq': i + 1, 'eta_ts': now + leg['eta_offset_min'] * 60,
                   'status': 'pending', 'notified': False, 'service_min': leg.get('service_min')}
                  for i, (lid, leg) in enumerate(zip(plan['stops'], plan['legs']))],
    })
    return {'ok': True, 'run': run, 'total_drive_min': plan['total_drive_min']}

def _lead_by_id(leads, lid):
    return next((l for l in leads if l.get('id') == lid), None)

def recompute_etas(c, leads, truck='A'):
    """Recompute ETAs for pending stops from current plow position (or last done stop)."""
    run = _route_run(c, truck)
    if not run.get('active'):
        return
    pending = [s for s in run['stops'] if s['status'] == 'pending']
    if not pending:
        return
    if run.get('plow_pos'):
        origin = {'lat': run['plow_pos']['lat'], 'lon': run['plow_pos']['lon']}
    else:
        done = [s for s in run['stops'] if s['status'] == 'done']
        if done:
            l = _lead_by_id(leads, done[-1]['lead_id'])
            origin = {'lat': l['lat'], 'lon': l['lon']} if l and l.get('lat') else None
        else:
            origin = {'lat': c.get('depot_lat'), 'lon': c.get('depot_lon')}
        if not origin or origin['lat'] is None:
            return
    pts = [origin]
    for s in pending:
        l = _lead_by_id(leads, s['lead_id'])
        pts.append({'lat': l['lat'], 'lon': l['lon']})
    try:
        T = _table(pts)
    except Exception:
        return
    now = time.time()
    cum, cur, svc_cum = 0, 0, 0
    for i, s in enumerate(pending):
        cum += (T[cur][i + 1] or 0)
        # ETA = drive from current position + plow time at every earlier pending stop.
        # While the truck is ON a stop, that stop's remaining plow time counts down
        # from its actual start — so ETAs pull earlier when a driveway goes quick
        # and push later when one runs long.
        remaining_svc = 0
        if i > 0:
            prev = pending[i - 1]
            started = prev.get('started_ts')
            if started:
                pl = _lead_by_id(leads, prev['lead_id']) or {}
                planned = service_min(c, pl) * 60
                remaining_svc = max(0, planned - (now - started))
        s['eta_ts'] = now + cum + svc_cum + remaining_svc
        l = _lead_by_id(leads, s['lead_id']) or {}
        svc_cum += service_min(c, l)
        cur = i + 1

def mark_stop(c, leads, lead_id, status, truck='A'):
    run = _route_run(c, truck)
    if not run.get('active'):
        return {'error': 'no active route run'}
    stop = next((s for s in run['stops'] if s['lead_id'] == lead_id), None)
    if not stop:
        return {'error': 'stop not on this run'}
    stop['status'] = status  # 'done' | 'skipped'
    if status == 'done':
        stop['done_ts'] = time.time()
    # stamp the new current stop's start so its remaining plow time counts down live
    nxt = next((x for x in run['stops'] if x['status'] == 'pending'), None)
    if nxt and not nxt.get('started_ts'):
        nxt['started_ts'] = time.time()
    recompute_etas(c, leads, truck)
    if all(s['status'] != 'pending' for s in run['stops']):
        archive_run(c, leads, 'completed', truck)
    return {'ok': True, 'run': run}

def gps_ping(c, leads, lat, lon, truck='A'):
    run = _route_run(c, truck)
    run['plow_pos'] = {'lat': lat, 'lon': lon, 'ts': time.time()}
    if run.get('active'):
        nxt = next((x for x in run['stops'] if x['status'] == 'pending'), None)
        if nxt and not nxt.get('started_ts'):
            nxt['started_ts'] = time.time()
    recompute_etas(c, leads, truck)
    return {'ok': True, 'active': run.get('active', False)}

def run_status(c, leads, truck='A'):
    run = _route_run(c, truck)
    out = dict(run)
    out['stops'] = []
    for s in run['stops']:
        l = _lead_by_id(leads, s['lead_id']) or {}
        out['stops'].append({**s, 'name': l.get('name', ''), 'address': l.get('address', ''),
                             'lat': l.get('lat'), 'lon': l.get('lon'),
                             'phone': l.get('phone', ''),
                             'notes': l.get('notes', ''),
                             'op_notes': l.get('op_notes', '')})
    done = sum(1 for s in out['stops'] if s['status'] == 'done')
    out['progress'] = {'done': done, 'total': len(out['stops'])}
    return out

def driver_auth(db, tok):
    """Which customer owns this driver token? Returns (customer, truck) or (None, None)."""
    if not tok:
        return None, None
    for email, c in db['customers'].items():
        toks = c.get('driver_tokens') or {}
        for k, t in toks.items():
            if t and t == tok:
                return c, k
        # legacy single token = Truck 1
        if c.get('driver_token') and c['driver_token'] == tok:
            return c, 'A'
    return None, None

def ensure_driver_token(c):
    """Idempotent — guarantees a token exists (driver_token already generates lazily)."""
    return driver_token(c)

# ---------- client-facing ETA ----------
def client_route_view(c, leads, client_email):
    """What one client sees: stop position, ETA, plow position. Checks every truck's run.
    Multi-property clients: tracks ALL their stops — the view follows the pending one
    (property 2 becomes the tracked stop the moment property 1 is done), and only reads
    complete when every property is done."""
    ce = (client_email or '').lower().strip()
    for tk, run in _route_runs(c).items():
        if not run.get('active'):
            continue
        mine = []
        for s in run['stops']:
            l = _lead_by_id(leads, s['lead_id'])
            if l and (l.get('email') or '').lower().strip() == ce:
                mine.append((s, l))
        if not mine:
            continue
        mine.sort(key=lambda x: x[0].get('seq') or 0)
        pending = [m for m in mine if m[0]['status'] == 'pending']
        s, l = pending[0] if pending else mine[-1]
        ahead = [x for x in run['stops'] if x['status'] == 'pending' and x['seq'] < s['seq']]
        done = sum(1 for x in run['stops'] if x['status'] == 'done')
        my_done = sum(1 for x in mine if x[0]['status'] != 'pending')
        return {
            'active': True, 'status': s['status'], 'truck': tk,
            'stop_number': s['seq'], 'total_stops': len(run['stops']),
            'stops_done': done, 'stops_ahead': len(ahead),
            'eta_ts': s.get('eta_ts'),
            'plow': run.get('plow_pos'),
            'you': {'lat': l.get('lat'), 'lon': l.get('lon'), 'address': l.get('address', '')},
            'business': c.get('business', ''),
            'my_total': len(mine), 'my_done': my_done,
            'my_stops': [{'address': (ml or {}).get('address', ''), 'status': ms['status'],
                          'seq': ms.get('seq'), 'eta_ts': ms.get('eta_ts')} for ms, ml in mine],
        }
    return {'active': False}

def stops_due_notification(c, leads):
    """Clients exactly 2 pending stops away who haven't been notified. Returns [(lead, stop)].
    Checks every truck's active run."""
    out = []
    for tk, run in _route_runs(c).items():
        if not run.get('active'):
            continue
        pending = [s for s in run['stops'] if s['status'] == 'pending']
        if len(pending) >= 3:
            s = pending[2]  # two stops ahead of this one
            if not s.get('notified'):
                l = _lead_by_id(leads, s['lead_id'])
                if l and l.get('phone'):
                    out.append((l, s))
    return out

DRIVER_PAGE_HTML = '''<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Plow Tracker</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{margin:0;background:#0a0d14;color:#e8ecf3;font-family:system-ui;display:flex;flex-direction:column;align-items:center;min-height:100vh;text-align:center;padding:20px}
 h1{font-size:1.4rem;margin:6px 0 0} #dot{width:74px;height:74px;border-radius:50%;background:#333;margin:18px auto 14px;transition:background .3s;box-shadow:0 0 0 0 rgba(255,255,255,.3)}
 #dot.on{background:#e5e5e5;animation:p 2s infinite} @keyframes p{0%{box-shadow:0 0 0 0 rgba(255,255,255,.35)}70%{box-shadow:0 0 0 26px rgba(255,255,255,0)}100%{box-shadow:0 0 0 0 rgba(255,255,255,0)}}
 button{background:linear-gradient(135deg,#ffffff,#9c9c9c);color:#0a0a0a;border:0;border-radius:12px;padding:15px 30px;font-size:1.05rem;font-weight:800;cursor:pointer}
 #st{color:#8b93a7;font-size:.88rem;margin-top:14px;white-space:pre-line;min-height:2.4em}
 #map{width:100%;max-width:560px;height:42vh;min-height:260px;border-radius:14px;margin-top:16px;display:none;border:1px solid #242d3f}
 #nextCard{width:100%;max-width:560px;background:#10141d;border:1px solid #242d3f;border-radius:14px;padding:14px 16px;margin-top:14px;display:none;text-align:left}
 #nextCard .lbl{font-size:.62rem;text-transform:uppercase;letter-spacing:.14em;color:#7c8494;font-weight:700}
 #nextCard .nm{font-weight:800;font-size:1.05rem;margin-top:4px}
 #nextCard .ad{color:#8b93a7;font-size:.84rem;margin-top:2px}
 #nextCard .row{display:flex;gap:10px;margin-top:12px}
 #nextCard .row button{flex:1}
 #navBtn{background:linear-gradient(135deg,#4ade80,#16a34a);color:#052e12}
 #doneBtn{background:#151a26;color:#e8ecf3;border:1px solid #2e3a50}
</style></head><body>
<h1>🚜 Plow Tracker <span id="tkLbl" style="font-size:.8rem;color:#8b93a7;font-weight:600"></span></h1>
<div id="dot"></div>
<button id="btn">Start Sharing Location</button>
<div id="st">Keep this page open while plowing.\\nScreen lock may pause sharing — keep it charging.</div>
<div id="map"></div>
<div id="nextCard">
  <div class="lbl">Next stop</div>
  <div class="nm" id="nxName">—</div>
  <div class="ad" id="nxAddr">—</div>
  <div class="ad" id="nxSvc"></div>
  <div id="nxNotes" style="display:none;margin-top:10px;padding:10px 12px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.35);border-radius:10px;font-size:.84rem;color:#fbbf24;line-height:1.45;text-align:left"></div>
  <div class="row">
    <button id="navBtn">🧭 Navigate</button>
    <button id="doneBtn">✓ Mark Done</button>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="skipBtn" style="background:#151a26;color:#e8ecf3;border:1px solid #2e3a50">⊘ Skip — come back later</button>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="photoBtn" style="background:#151a26;color:#e8ecf3;border:1px solid #2e3a50">📷 Add proof photo</button>
    <input type="file" id="photoInput" accept="image/*" capture="environment" style="display:none">
  </div>
  <div id="photoPrev" style="display:none;margin-top:10px;text-align:left">
    <img id="photoImg" style="width:100%;max-height:180px;object-fit:cover;border-radius:10px;border:1px solid #2e3a50">
    <div style="display:flex;gap:10px;margin-top:8px">
      <span style="flex:1;color:#4ade80;font-size:.8rem;align-self:center">✓ Photo attached — will send with Mark Done</span>
      <button id="photoClear" style="background:none;border:1px solid #2e3a50;color:#8b93a7;border-radius:8px;padding:6px 12px;font-size:.75rem">Remove</button>
    </div>
  </div>
</div>
<script>
let watch=null, map=null, meMarker=null, stopMarkers=[], poly=null, RUN=null, NEXT=null;
let token=new URLSearchParams(location.search).get('token')||'';
const st=document.getElementById('st'), dot=document.getElementById('dot'), btn=document.getElementById('btn');
const mapEl=document.getElementById('map'), nx=document.getElementById('nextCard');
if(!token){st.textContent='Missing token — use the link from the Routes page.';btn.style.display='none';}
if(location.protocol!=='https:'&&location.hostname!=='localhost'&&location.hostname!=='127.0.0.1'){st.textContent='⚠ GPS needs HTTPS — open the secure link.';btn.style.display='none';}
function numIcon(seq,done,next){return L.divIcon({className:'',iconSize:[30,30],iconAnchor:[15,15],
 html:`<div style="width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:.8rem;border:2px solid ${done?'#4ade80':(next?'#fbbf24':'#e5e5e5')};background:${done?'rgba(74,222,128,.18)':(next?'rgba(251,191,36,.15)':'#10141d')};color:${done?'#4ade80':(next?'#fbbf24':'#e5e5e5')}">${done?'✓':seq}</div>`});}
async function loadRun(){
 try{const r=await fetch('/api/routes/driver-run?token='+encodeURIComponent(token)); const j=await r.json(); if(j.error) return; RUN=j;
  if(RUN.truck) document.getElementById('tkLbl').textContent='· '+(RUN.truck==='B'?'Truck 2':'Truck 1');
  renderRun();}catch(e){}
}
function renderRun(){
 if(!RUN||!RUN.stops||!RUN.stops.length) return;
 const pts=RUN.stops.filter(s=>s.lat&&s.lon);
 if(!pts.length) return;
 mapEl.style.display='block';
 if(!map){map=L.map('map').setView([pts[0].lat,pts[0].lon],13);
  L.tileLayer('/tiles/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);}
 stopMarkers.forEach(m=>m.remove()); stopMarkers=[]; if(poly){poly.remove();poly=null;}
 const nextStop=RUN.stops.find(s=>s.status==='pending');
 pts.forEach(s=>{const m=L.marker([s.lat,s.lon],{icon:numIcon(s.seq,s.status==='done',nextStop&&s.lead_id===nextStop.lead_id)}).addTo(map).bindPopup('<b>#'+s.seq+' '+s.name+'</b><br>'+s.address); stopMarkers.push(m);});
 const order=pts.slice().sort((a,b)=>a.seq-b.seq).map(s=>[s.lat,s.lon]);
 poly=L.polyline(order,{color:'#e5e5e5',weight:3,opacity:.5,dashArray:'6 8'}).addTo(map);
 NEXT=nextStop||null;
 if(NEXT){
  nx.style.display='block';
  document.getElementById('nxName').textContent='#'+NEXT.seq+' '+NEXT.name;
  document.getElementById('nxAddr').textContent=NEXT.address;
  document.getElementById('nxSvc').textContent=NEXT.service_min?('~'+NEXT.service_min+' min on site'):'';
  const nn=document.getElementById('nxNotes');
  const noteText=(NEXT.op_notes||NEXT.notes||'').trim();
  if(noteText){nn.textContent='⚠ '+noteText;nn.style.display='block';}else{nn.style.display='none';}
 } else { nx.style.display='none'; }
 if(!meMarker) map.fitBounds(L.latLngBounds(order).pad(0.12));
}
document.getElementById('navBtn').onclick=()=>{if(NEXT) window.open('https://www.google.com/maps/dir/?api=1&destination='+encodeURIComponent(NEXT.address),'_blank');};
let PROOF=null; // compressed data URL, optional
const photoInput=document.getElementById('photoInput');
document.getElementById('photoBtn').onclick=()=>photoInput.click();
document.getElementById('photoClear').onclick=()=>{PROOF=null;document.getElementById('photoPrev').style.display='none';photoInput.value='';};
photoInput.onchange=()=>{const f=photoInput.files[0];if(!f)return;
 const img=new Image();
 img.onload=()=>{const cv=document.createElement('canvas');const s=Math.min(1,1400/img.width);
  cv.width=img.width*s;cv.height=img.height*s;
  cv.getContext('2d').drawImage(img,0,0,cv.width,cv.height);
  PROOF=cv.toDataURL('image/jpeg',.82);
  document.getElementById('photoImg').src=PROOF;
  document.getElementById('photoPrev').style.display='block';
  URL.revokeObjectURL(img.src);};
 img.src=URL.createObjectURL(f);};
document.getElementById('doneBtn').onclick=async()=>{if(!NEXT) return;
 const body={token,lead_id:NEXT.lead_id,status:'done'};
 if(PROOF) body.photo=PROOF;
 document.getElementById('doneBtn').textContent='Saving…';
 try{
  const r=await fetch('/api/routes/stop-driver',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  if(j.error){st.textContent='⚠ '+j.error;document.getElementById('doneBtn').textContent='✓ Mark Done';return;}
  PROOF=null;document.getElementById('photoPrev').style.display='none';photoInput.value='';
 }catch(e){st.textContent='Save failed — check signal';}
 document.getElementById('doneBtn').textContent='✓ Mark Done';
 loadRun();};
document.getElementById('skipBtn').onclick=async()=>{if(!NEXT) return;
 if(!confirm('Skip '+NEXT.name+' for now? It stays on the route as skipped.')) return;
 try{await fetch('/api/routes/stop-driver',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,lead_id:NEXT.lead_id,status:'skipped'})});}catch(e){st.textContent='Save failed — check signal';}
 loadRun();};
async function ping(p){try{const r=await fetch('/api/routes/gps',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,lat:p.coords.latitude,lon:p.coords.longitude})});
 const j=await r.json(); if(j.error){st.textContent='⚠ '+j.error;dot.classList.remove('on');btn.style.display='';btn.textContent='Retry';if(watch){navigator.geolocation.clearWatch(watch);watch=null;}return;}
 st.textContent='Sharing live · '+new Date().toLocaleTimeString();
 if(map){const ll=[p.coords.latitude,p.coords.longitude];
  if(!meMarker){meMarker=L.marker(ll,{icon:L.divIcon({className:'',iconSize:[18,18],iconAnchor:[9,9],html:'<div style="width:14px;height:14px;border-radius:50%;background:#4ade80;border:3px solid #052e12;box-shadow:0 0 10px #4ade80"></div>'})}).addTo(map).bindPopup('You');}
  else meMarker.setLatLng(ll);}}catch(e){st.textContent='Ping failed — check signal';}}
btn.onclick=()=>{if(!navigator.geolocation){st.textContent='No GPS on this device';return;}
 st.textContent='Requesting location…';
 navigator.geolocation.getCurrentPosition(p=>{ping(p);
  watch=navigator.geolocation.watchPosition(ping,e=>{st.textContent='GPS error: '+e.message;dot.classList.remove('on');btn.style.display='';btn.textContent='Retry';},{enableHighAccuracy:true,maximumAge:30000,timeout:20000});
  dot.classList.add('on');btn.style.display='none';st.textContent='Starting…';
  loadRun(); setInterval(loadRun,30000);
 },e=>{st.textContent='⚠ '+e.message+' — allow location access and tap Retry.';});};
loadRun();
</script></body></html>'''
