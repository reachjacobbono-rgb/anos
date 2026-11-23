#!/usr/bin/env python3
"""
ANOSagent Portal -> Anno (Hermes) chat bridge.

Sits between the portal and Hermes. The portal POSTs chat messages here;
the bridge forwards them to Hermes as a real agent turn (full memory, skills,
tools) and returns Anno's reply synchronously to the portal.

Run:  python3 /opt/data/portal_bridge.py
Port: 8786 (localhost only)
"""
import json, os, re, sqlite3, subprocess, threading, time, urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 8786
STATE_DB = Path('/opt/data/state.db')
ENV_FILE = '/opt/data/.env'
BRIDGE_TOKEN_FILE = '/opt/data/.portal_bridge_token'
HERMES = '/opt/hermes/.venv/bin/hermes'
MODEL = 'moonshotai/kimi-k3'
HERMES_TIMEOUT = 300          # seconds to wait for Anno's turn
FALLBACK_MODEL = 'deepseek/deepseek-chat-v3-0324'

ENV = {}
for line in open(ENV_FILE):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        ENV[k] = v
OR_KEY = ENV.get('OPENROUTER_API_KEY', '')

# ---------------------------------------------------------------- state ----
def _db():
    conn = sqlite3.connect(STATE_DB, timeout=30)
    conn.execute('''CREATE TABLE IF NOT EXISTS portal_bridge (
        customer_email TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        updated REAL NOT NULL)''')
    return conn

def get_session(email):
    conn = _db()
    row = conn.execute('SELECT session_id FROM portal_bridge WHERE customer_email=?', (email,)).fetchone()
    conn.close()
    return row[0] if row else None

def set_session(email, sid):
    conn = _db()
    with conn:
        conn.execute('INSERT OR REPLACE INTO portal_bridge VALUES (?,?,?)', (email, sid, time.time()))
    conn.close()

# --------------------------------------------------------------- hermes ----
def hermes_turn(email, message):
    """Run one Anno turn. Returns (reply, err)."""
    sid = get_session(email)
    prompt = (f"[PORTAL CHAT from {email}] {message}\n\n"
              "(This message came from the ANOSagent web portal chat. Reply conversationally — "
              "your reply is shown verbatim in the portal chat box. No Telegram preamble needed.)")
    if sid:
        cmd = [HERMES, 'chat', '--resume', sid, '-q', prompt, '-Q', '-m', MODEL]
    else:
        cmd = [HERMES, 'chat', '-q', prompt, '-Q', '-m', MODEL]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=HERMES_TIMEOUT,
                           cwd='/opt/data', env={**os.environ, 'HERMES_HOME': '/opt/data'})
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    out = (r.stdout or '').strip()
    if r.returncode != 0 or not out:
        # if resume failed (stale/unknown session), retry fresh
        if sid:
            set_session(email, None) if False else None
            conn = _db()
            with conn:
                conn.execute('DELETE FROM portal_bridge WHERE customer_email=?', (email,))
            conn.close()
            return hermes_turn(email, message)
        return None, (r.stderr or 'empty reply')[-300:]
    # capture the session id for future resumes (printed on stderr)
    m = re.findall(r'session_id:\s*(\S+)', r.stderr or '')
    if m and not sid:
        set_session(email, m[-1])
    # strip ANSI codes if any
    out = re.sub(r'\x1b\[[0-9;]*m', '', out)
    return out, None

# ------------------------------------------------------------- fallback ----
def fallback_reply(sys_prompt, history, message):
    msgs = [{'role': 'system', 'content': sys_prompt or 'You are Anno, a helpful business agent.'}]
    for h in (history or [])[-9:]:
        msgs.append({'role': h.get('role', 'user'), 'content': h.get('text', '')})
    msgs.append({'role': 'user', 'content': message})
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps({'model': FALLBACK_MODEL, 'messages': msgs, 'max_tokens': 400}).encode(),
        method='POST', headers={'Authorization': 'Bearer ' + OR_KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    return d['choices'][0]['message']['content'].strip()

# ---------------------------------------------------------------- http -----
class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n) or b'{}')

    def log_message(self, *a):
        print('[bridge]', a[0] % a[1:])

    def do_GET(self):
        if self.path == '/health':
            return self._json(200, {'status': 'ok'})
        self._json(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/chat':
            return self._json(404, {'error': 'not found'})
        tok = self.headers.get('X-Bridge-Token', '')
        want = Path(BRIDGE_TOKEN_FILE).read_text().strip() if Path(BRIDGE_TOKEN_FILE).exists() else ''
        if not want or tok != want:
            return self._json(401, {'error': 'bad token'})
        b = self._body()
        email = (b.get('customer') or 'unknown').strip()
        msg = (b.get('message') or '').strip()
        if not msg:
            return self._json(400, {'error': 'empty message'})
        t0 = time.time()
        reply, err = hermes_turn(email, msg)
        src = 'hermes'
        if reply is None:
            print(f'[bridge] hermes failed ({err}), falling back')
            try:
                reply = fallback_reply(b.get('system_prompt'), b.get('history'), msg)
                src = 'fallback'
            except Exception as e2:
                return self._json(200, {'reply': f'Sorry — I glitched: {e2}', 'source': 'error'})
        print(f'[bridge] {email} served by {src} in {time.time()-t0:.0f}s')
        self._json(200, {'reply': reply, 'source': src})

threading.Thread(target=lambda: None, daemon=True).start()
print(f'portal bridge on :{PORT}')
HTTPServer(('127.0.0.1', PORT), H).serve_forever()
