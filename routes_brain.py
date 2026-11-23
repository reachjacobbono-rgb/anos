#!/usr/bin/env python3
"""
Brain — the agent's knowledge base (Obsidian-style notes).
Notes marked enabled=True are injected into the agent's system prompt,
so the owner literally teaches the agent by writing notes.
"""
import secrets, time

MAX_NOTES = 200
MAX_BODY = 20000
PROMPT_BUDGET = 6000  # chars of brain injected into the system prompt


def _notes(c):
    return c.setdefault('brain_notes', [])


def seed(c):
    """First-run starter notes."""
    if _notes(c):
        return False
    now = int(time.time())
    _notes(c).extend([
        {'id': secrets.token_hex(8), 'title': 'Business Info', 'enabled': True, 'tags': ['business'],
         'body': "# Business Info\n\nEverything the agent should know about the business.\n\n- Services we offer:\n- Service area:\n- Hours:\n- How to reach us:\n",
         'updated': now},
        {'id': secrets.token_hex(8), 'title': 'Services & Pricing', 'enabled': True, 'tags': ['pricing'],
         'body': "# Services & Pricing\n\nTypical jobs and what we charge. The agent uses this when drafting quotes.\n\n| Service | Typical price |\n|---|---|\n|  |  |\n",
         'updated': now},
        {'id': secrets.token_hex(8), 'title': 'FAQ', 'enabled': True, 'tags': ['scripts'],
         'body': "# FAQ\n\nAnswers the agent should give when clients ask common questions.\n\n**Do you offer free quotes?**\n\n**Are you insured?**\n\n**How far out are you booking?**\n",
         'updated': now},
    ])
    return True


def list_notes(c):
    seed(c)
    return sorted(_notes(c), key=lambda n: n.get('updated', 0), reverse=True)


def save_note(c, b):
    notes = _notes(c)
    nid = b.get('id')
    title = str(b.get('title') or 'Untitled').strip()[:120] or 'Untitled'
    body = str(b.get('body') or '')[:MAX_BODY]
    enabled = bool(b.get('enabled', True))
    share = bool(b.get('share', False))
    tags = [str(t).strip().lower()[:24] for t in (b.get('tags') or []) if str(t).strip()][:8]
    if nid:
        n = next((x for x in notes if x.get('id') == nid), None)
        if not n:
            return None, 'note not found'
        # revision history — keep last 10 bodies
        revs = n.setdefault('revisions', [])
        if n.get('body', '') != body:
            revs.append({'ts': int(time.time()), 'body': n.get('body', ''), 'title': n.get('title', '')})
            n['revisions'] = revs[-10:]
        n.update({'title': title, 'body': body, 'enabled': enabled, 'share': share,
                  'tags': tags, 'updated': int(time.time())})
        return n, None
    if len(notes) >= MAX_NOTES:
        return None, 'brain is full (200 notes)'
    n = {'id': secrets.token_hex(8), 'title': title, 'body': body,
         'enabled': enabled, 'share': share, 'tags': tags, 'updated': int(time.time()),
         'revisions': []}
    notes.append(n)
    return n, None


def restore_revision(c, nid, idx):
    n = next((x for x in _notes(c) if x.get('id') == nid), None)
    if not n:
        return None, 'note not found'
    revs = n.get('revisions', [])
    if idx < 0 or idx >= len(revs):
        return None, 'revision not found'
    rev = revs[idx]
    # current body becomes newest revision before restore
    revs[idx] = {'ts': int(time.time()), 'body': n.get('body', ''), 'title': n.get('title', '')}
    n['body'] = rev['body']
    if rev.get('title'):
        n['title'] = rev['title']
    n['updated'] = int(time.time())
    return n, None


def delete_note(c, nid):
    notes = _notes(c)
    before = len(notes)
    c['brain_notes'] = [n for n in notes if n.get('id') != nid]
    return len(c['brain_notes']) < before


def prompt_block(c):
    """Compact text of enabled notes for the agent system prompt."""
    parts, used = [], 0
    for n in list_notes(c):
        if not n.get('enabled'):
            continue
        chunk = f"## {n['title']}\n{n['body'].strip()}"
        if used + len(chunk) > PROMPT_BUDGET:
            break
        parts.append(chunk)
        used += len(chunk)
    if not parts:
        return ''
    return "\n\nKNOWLEDGE BASE (owner's Brain notes — treat as ground truth about the business; answer from these when relevant):\n" + '\n\n'.join(parts) + '\n'


def remember(c, title, body):
    """Agent tool: save a memory to the Brain. Appends to an existing same-title note or creates one."""
    n = next((x for x in _notes(c) if (x.get('title') or '').lower() == title.lower()), None)
    if n:
        n['body'] = (n['body'].rstrip() + '\n' + body.strip())[:MAX_BODY]
        n['updated'] = int(time.time())
    else:
        _notes(c).append({'id': secrets.token_hex(8), 'title': title[:120], 'body': body[:MAX_BODY],
                          'enabled': True, 'updated': int(time.time())})
    return True


def client_prompt_block(c):
    """Notes explicitly shared with clients (voice agent / client portal)."""
    parts = []
    for n in list_notes(c):
        if n.get('share') and n.get('enabled'):
            parts.append(f"## {n['title']}\n{n['body'].strip()}")
    if not parts:
        return ''
    joined = '\n\n'.join(parts)[:4000]
    return ("\n\nWHAT CLIENTS CAN KNOW (answer client questions from this; never reveal other business data):\n"
            + joined + '\n')


HEALTH_KEYS = [
    ('pricing',   'Pricing — what you charge',      ['price', 'pricing', 'charge', 'cost', '$', 'rate']),
    ('insurance', 'Insurance & licensing',           ['insur', 'licen', 'wsib', 'bond']),
    ('area',      'Service area',                    ['area', 'serve', 'region', 'city', 'town', 'km']),
    ('hours',     'Hours & availability',            ['hour', 'open', 'monday', 'weekend', 'schedule', 'booking']),
    ('contact',   'How clients reach you',           ['phone', 'call', 'text', 'email', 'contact', 'website']),
    ('policies',  'Policies (deposits, cancels)',    ['deposit', 'cancel', 'refund', 'policy', 'terms']),
    ('faq',       'FAQ answers',                     ['faq', 'question', 'ask']),
    ('services',  'Services you offer',              ['service', 'offer', 'removal', 'prune', 'plow', 'mow']),
]

def health(c):
    """Brain health checklist — which key business topics are covered."""
    blob = '\n'.join((n.get('title', '') + '\n' + n.get('body', '')).lower()
                      for n in list_notes(c) if n.get('enabled'))
    out = []
    for key, label, words in HEALTH_KEYS:
        out.append({'key': key, 'label': label,
                    'done': any(w in blob for w in words)})
    done = sum(1 for o in out if o['done'])
    return {'items': out, 'done': done, 'total': len(out)}


NOTE_TEMPLATES = [
    {'id': 'pricing',  'name': 'Pricing sheet',
     'title': 'Pricing — ',
     'body': "# Pricing\n\n| Job | Price | Notes |\n|---|---|---|\n|  |  |  |\n\nRates are [flat/hourly]. HST [included/extra].\n"},
    {'id': 'objections', 'name': 'Objection handling',
     'title': 'Objections — ',
     'body': "# Objection handling\n\n**\"That's too expensive\"**\n→ \n\n**\"Can you just top it?\"**\n→ \n\n**\"The other guy quoted less\"**\n→ \n"},
    {'id': 'promo',    'name': 'Seasonal promo',
     'title': 'Promo — ',
     'body': "# Seasonal promo\n\n**Runs:** [dates]\n**Offer:** \n**Code/mention:** \n**Who qualifies:** \n"},
    {'id': 'crew',     'name': 'Crew rules',
     'title': 'Crew rules — ',
     'body': "# Crew rules\n\n- Start time:\n- Site conduct:\n- What we never do:\n- Escalate to owner when:\n"},
    {'id': 'clients',  'name': 'Client notes',
     'title': 'Client — ',
     'body': "# Client notes\n\n**Property:** \n**Gate/parking:** \n**Pets:** \n**Preferences:** \n"},
]


def digest_text(c, raw, make_note):
    """Bulk import: owner pastes raw text (price list, doc dump). We ask the model
    (via the portal's chat infra) — here we just structure notes heuristically:
    split on headings / double newlines into one note, titled from first line."""
    raw = (raw or '').strip()
    if not raw:
        return None, 'nothing to import'
    first = raw.split('\n', 1)[0].strip(' #')[:80] or 'Imported notes'
    n = {'id': secrets.token_hex(8), 'title': first, 'body': raw[:MAX_BODY],
         'enabled': True, 'share': False, 'tags': ['imported'], 'updated': int(time.time()),
         'revisions': []}
    _notes(c).append(n)
    return n, None
