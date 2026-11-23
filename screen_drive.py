"""Drive the kiosk Chromium on DISPLAY :99 over CDP — navigate, click, type, eval.
Used by the portal agent so its actions show up live on the streamed desktop."""
import json, urllib.request, time

CDP = 'http://127.0.0.1:9222/json'

def _ws():
    import websocket
    tabs = json.load(urllib.request.urlopen(CDP))
    page = next((t for t in tabs if t.get('type') == 'page'), None)
    if not page:
        raise RuntimeError('no page target')
    return websocket.create_connection(page['webSocketDebuggerUrl'], timeout=20, suppress_origin=True)

_id = [0]
def _send(ws, method, params=None):
    _id[0] += 1
    mid = _id[0]
    ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
    deadline = time.time() + 20
    while time.time() < deadline:
        m = json.loads(ws.recv())
        if m.get('id') == mid:
            if 'error' in m:
                raise RuntimeError(str(m['error']))
            return m.get('result', {})
    raise RuntimeError('CDP timeout on ' + method)

def evaljs(expr):
    """Run JS in the desktop page, return the value."""
    ws = _ws()
    try:
        r = _send(ws, 'Runtime.evaluate', {
            'expression': expr, 'returnByValue': True, 'awaitPromise': True})
        res = r.get('result', {})
        if res.get('subtype') == 'error' or r.get('exceptionDetails'):
            raise RuntimeError(str(r.get('exceptionDetails', res))[:200])
        return res.get('value')
    finally:
        ws.close()

def navigate(url):
    ws = _ws()
    try:
        _send(ws, 'Page.enable')
        _send(ws, 'Page.navigate', {'url': url})
        time.sleep(4)
    finally:
        ws.close()

def show_view(view):
    """Bring a portal view up on the desktop screen (CRM, estimates, routes...)."""
    return evaljs(
        "(function(){"
        "  try {"
        "    if (typeof showView === 'function') { showView('" + view + "'); return 'view:" + view + "'; }"
        "    const el = document.querySelector('[data-view=\"" + view + "\"],"
        "      a[href*=\"view=" + view + "\"], nav *');"
        "    if (el && el.click) { el.click(); return 'clicked:' + view; }"
        "    location.hash = 'view=" + view + "'; return 'hash:' + view;"
        "  } catch(e){ return 'err:' + e.message; }"
        "})()")

def click_text(txt):
    """Click the first visible element containing txt (buttons, links, nav items)."""
    safe = json.dumps(txt)
    return evaljs(
        "(function(){"
        "  const t = " + safe + ".toLowerCase();"
        "  const els = [...document.querySelectorAll('button,a,[role=button],li,td,span,div')]"
        "    .filter(e => e.offsetParent !== null && (e.innerText||'').trim().toLowerCase().includes(t)"
        "                && (e.innerText||'').length < 120);"
        "  els.sort((a,b) => (a.innerText||'').length - (b.innerText||'').length);"
        "  if (!els.length) return 'not found: ' + t;"
        "  els[0].scrollIntoView({block:'center'}); els[0].click();"
        "  return 'clicked: ' + (els[0].innerText||'').trim().slice(0,60);"
        "})()")

def screen_status():
    return evaljs("location.pathname + location.search + ' | ' + document.title")
