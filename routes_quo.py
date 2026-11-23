#!/usr/bin/env python3
"""
Quo (formerly OpenPhone) integration for the ANOSagent portal.

Per-customer API-key connection (Quo has no OAuth — workspace owners generate
keys in Quo → Workspace Settings → API). Once connected:
- Outbound SMS from the portal via the customer's own Quo number.
- Inbound SMS + call events arrive via Quo webhooks at /api/quo/webhook and are
  logged onto the matching lead's timeline (matched by phone digits).
- Message history backfill on connect (last ~20 conversations).

Storage on the customer record:
  integrations.quo = {
      'api_key': ..., 'phone_number_id': 'PN...', 'number': '+1...',
      'formatted': '(519) ...', 'webhook_msgs': 'WH...', 'webhook_calls': 'WH...',
      'webhook_keys': {'messages': ..., 'calls': ...}, 'connected_at': ts,
  }
"""
import json, time, hmac, hashlib, base64, urllib.request, urllib.parse, urllib.error

QUO_BASE = 'https://api.quo.com/v1'
PORTAL_BASE_DEFAULT = 'https://app.anos.ca'


def _req(api_key, path, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(QUO_BASE + path, data=data, method=method, headers={
        'Authorization': api_key, 'Content-Type': 'application/json',
        'User-Agent': 'QuoAPI/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except Exception:
            return e.code, {'error': 'http ' + str(e.code)}
    except Exception as e:
        return 0, {'error': str(e)}


def _norm(num):
    """Keep digits only; +1XXXXXXXXXX form."""
    if not num:
        return ''
    d = ''.join(ch for ch in str(num) if ch.isdigit())
    if len(d) == 10:
        d = '1' + d
    return '+' + d if d else ''


def _tail(num):
    """Last 10 digits — for matching leads regardless of format."""
    d = ''.join(ch for ch in (num or '') if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def verify_key(api_key):
    """Return (ok, info) — lists phone numbers on the workspace."""
    st, r = _req(api_key, '/phone-numbers')
    if st == 200 and isinstance(r.get('data'), list):
        return True, r['data']
    return False, r


def connect(c, api_key, portal_base=PORTAL_BASE_DEFAULT):
    """Verify key, pick primary number, register both webhooks. Returns (ok, err_or_info)."""
    api_key = (api_key or '').strip()
    ok, info = verify_key(api_key)
    if not ok:
        return False, info.get('message') or info.get('error') or 'key rejected by Quo'
    if not info:
        return False, 'no phone numbers on this Quo workspace'
    num = info[0]
    q = {
        'api_key': api_key,
        'phone_number_id': num['id'],
        'number': num.get('number'),
        'formatted': num.get('formattedNumber') or num.get('number'),
        'connected_at': int(time.time()),
    }
    # webhooks (messages + calls)
    for kind, events in (('messages', ['message.received', 'message.delivered']),
                         ('calls', ['call.completed'])):
        st, r = _req(api_key, '/webhooks/' + kind, 'POST', {
            'url': portal_base + '/api/quo/webhook',
            'events': events,
            'label': 'ANOSagent'})
        if st in (200, 201):
            q['webhook_' + kind] = r['data']['id']
            q.setdefault('webhook_keys', {})[kind] = r['data'].get('key')
    c.setdefault('integrations', {})['quo'] = q
    return True, {'number': q['formatted']}


def disconnect(c):
    q = c.get('integrations', {}).get('quo')
    if q:
        for k in ('webhook_messages', 'webhook_calls'):
            if q.get(k):
                _req(q['api_key'], '/webhooks/' + q[k], 'DELETE')
    c.get('integrations', {}).pop('quo', None)


def send_sms(c, to, text):
    q = (c.get('integrations') or {}).get('quo')
    if not q:
        return False, 'Quo not connected'
    st, r = _req(q['api_key'], '/messages', 'POST', {
        'content': str(text)[:1600],
        'from': q['number'],
        'to': [_norm(to)],
    })
    if st in (200, 202):
        return True, r.get('data', {})
    return False, r.get('message') or r.get('error') or 'send failed'


def send_mms(c, to, text, media_urls):
    q = (c.get('integrations') or {}).get('quo')
    if not q:
        return False, 'Quo not connected'
    payload = {
        'content': str(text)[:1600],
        'from': q['number'],
        'to': [_norm(to)],
        'mediaUrls': [str(u) for u in (media_urls or [])][:5],
    }
    st, r = _req(q['api_key'], '/messages', 'POST', payload)
    if st in (200, 202):
        return True, r.get('data', {})
    return False, r.get('message') or r.get('error') or 'mms failed'


def fetch_history(c, phone, limit=50):
    """Pull the message thread with a given phone number."""
    q = (c.get('integrations') or {}).get('quo')
    if not q:
        return []
    pn = _norm(phone)
    if not pn:
        return []
    st, r = _req(q['api_key'], '/messages?phoneNumberId={}&participants={}&maxResults={}'.format(
        q['phone_number_id'], urllib.parse.quote(pn), limit))
    if st != 200:
        return []
    msgs = r.get('data') or []
    msgs.sort(key=lambda m: m.get('createdAt', ''))
    return msgs


def log_to_lead(c, phone, entry):
    """Append an activity to the lead matching this phone (last-10-digits)."""
    tail = _tail(phone)
    if not tail:
        return None
    for l in c.get('crm_leads', []):
        if _tail(l.get('phone')) == tail:
            l.setdefault('activities', []).append(entry)
            return l
    return None


def verify_signature(raw_body, header_key_b64, signature):
    """Quo signs webhooks: HMAC-SHA256 of the raw body with the webhook key (base64)."""
    try:
        key = base64.b64decode(header_key_b64)
        mac = hmac.new(key, raw_body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(mac).decode(), signature or '')
    except Exception:
        return False


def handle_webhook(db, save_db, raw_body, body, headers):
    """Route an inbound Quo event to the right customer by phoneNumberId / participants.
    Returns True if the event was claimed."""
    etype = body.get('type') or body.get('event') or ''
    data = body.get('data') or body.get('object') or {}
    msg = data.get('object') if isinstance(data.get('object'), dict) else data
    pn_id = msg.get('phoneNumberId')
    direction = msg.get('direction')
    now = int(time.time())

    for email, c in db.get('customers', {}).items():
        q = (c.get('integrations') or {}).get('quo')
        if not q or q.get('phone_number_id') != pn_id:
            continue
        # match found — build the timeline entry
        if 'message' in etype:
            if direction == 'incoming':
                peer = (msg.get('from') or '')
                text = msg.get('text') or msg.get('content') or ''
                entry = {'ts': now, 'type': 'sms', 'by': 'client',
                         'text': f'📩 Text from client: {text[:400]}'}
            else:
                # outbound texts are already logged by /api/quo/send — skip webhook echo
                return True
        elif 'call' in etype:
            peer = msg.get('from') if direction == 'incoming' else (
                (msg.get('to') or [''])[0] if isinstance(msg.get('to'), list) else msg.get('to'))
            dur = msg.get('duration')
            dur_s = f" ({int(dur)//60}m{int(dur)%60}s)" if dur else ''
            arrow = '📞 Incoming' if direction == 'incoming' else '📞 Outgoing'
            entry = {'ts': now, 'type': 'call', 'by': 'client' if direction == 'incoming' else 'owner',
                     'text': f'{arrow} call{dur_s} — {msg.get("status", "completed")}'}
        else:
            return False
        lead = log_to_lead(c, peer, entry)
        if not lead:
            # no matching lead — stash on the account activity so it's not lost
            c.setdefault('activity', []).append(
                {'ts': now, 'kind': 'quo', 'text': entry['text'] + f' [{peer}] (no matching lead)'})
        save_db(db)
        return True
    return False
