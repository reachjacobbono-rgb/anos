#!/usr/bin/env python3
"""
AI Agent Client Portal — backend.
Customers log in, see live AI usage (from their own OpenRouter key),
upgrade/downgrade tiers (auto-adjusts the key's daily limit), view billing.

Data: /opt/data/portal/customers.json
OpenRouter Management API: https://openrouter.ai/api/v1/keys
  - requires a MANAGEMENT key (create at openrouter.ai/settings/keys, separate from the inference key)
"""
import json, os, re, secrets, sqlite3, time, urllib.request, urllib.error, urllib.parse, subprocess, threading, uuid
from concurrent import futures
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import routes_snow as snow
import routes_connect as connect
import routes_brain as brain
import routes_quo as quo
import invoice_pdf

BASE = Path(os.environ.get('PORTAL_DIR', '/opt/data/portal'))
DB = BASE / 'customers.json'
SQLITE_DB = BASE / 'portal.db'

ENV = {}
for line in open(os.environ.get('ANOS_ENV_FILE', '/opt/data/.env')):
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        ENV[k] = v

# Management key for provisioning. Falls back to regular key for read-only dev mode.
MGMT_KEY = ENV.get('OPENROUTER_MANAGEMENT_KEY', '')
STRIPE_KEY = ENV.get('STRIPE_SECRET_KEY', '')

# ---- Plans: monthly price -> OpenRouter daily credit limit (USD of credits/day) ----
PLANS = {
    'lite':    {'name': 'Lite',    'monthly': 147, 'daily_limit': 0.40, 'seats': 2,           'credits': 500,  'price_id': 'price_1UQVKZRkROG1jjD7IRGx45wA'},
    'starter': {'name': 'Starter', 'monthly': 297, 'daily_limit': 1.0,  'seats': 5,           'credits': 1500, 'price_id': 'price_1UQVKaRkROG1jjD7cjQHZ9m4'},
    'pro':     {'name': 'Pro',     'monthly': 497, 'daily_limit': 3.0,  'seats': 'Unlimited', 'credits': 4000, 'price_id': 'price_1UQVKaRkROG1jjD77X9leq3v'},
}
TRIAL_DAYS = 7

# Credit reloads: price_id -> (charge CAD, credit boost USD added to today's limit)
RELOADS = {
    'reload10': {'charge': 10, 'credit': 5.0,  'price_id': 'price_1UQVKbRkROG1jjD7cZUxf1eY', 'label': '$10 → +$5 credits'},
    'reload25': {'charge': 25, 'credit': 15.0, 'price_id': 'price_1UQVKbRkROG1jjD7rIwe9JXn', 'label': '$25 → +$15 credits'},
    'reload50': {'charge': 50, 'credit': 40.0, 'price_id': 'price_1UQVKcRkROG1jjD7iJOf9ezf', 'label': '$50 → +$40 credits'},
}
PORTAL_BASE = os.environ.get('PORTAL_BASE', 'https://education-economic-montana-committee.trycloudflare.com')

def stripe_request(method, path, params=None, on_account=None):
    """Minimal Stripe call via form-encoded body (no SDK). on_account = connected acct id (Connect)."""
    data = urllib.parse.urlencode(params or {}, doseq=True).encode() if params else None
    if method == 'GET' and params:
        path += '?' + urllib.parse.urlencode(params)
        data = None
    headers = {'Authorization': f'Bearer {STRIPE_KEY}', 'Content-Type': 'application/x-www-form-urlencoded'}
    if on_account:
        headers['Stripe-Account'] = on_account
    req = urllib.request.Request(f'https://api.stripe.com/v1{path}', data=data, method=method, headers=headers)
    with urllib.request.urlopen(req) as r:
        return json.load(r)

# ---- Storage: SQLite primary, JSON mirror as live backup ----
def _sql():
    conn = sqlite3.connect(SQLITE_DB, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS customers (
        email TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        updated REAL NOT NULL)''')
    return conn

def _migrate_json_to_sql():
    """One-time import: JSON file -> SQLite. Safe to run repeatedly (skips if SQL already has rows)."""
    conn = _sql()
    n = conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]
    if n or not DB.exists():
        conn.close()
        return
    db = json.loads(DB.read_text())
    with conn:
        for email, c in db.get('customers', {}).items():
            conn.execute('INSERT OR REPLACE INTO customers (email, data, updated) VALUES (?,?,?)',
                         (email, json.dumps(c), time.time()))
    conn.close()
    print(f'[db] migrated {len(db.get("customers", {}))} customers from JSON to SQLite')

def load_db():
    _migrate_json_to_sql()
    conn = _sql()
    rows = conn.execute('SELECT email, data FROM customers').fetchall()
    conn.close()
    out = {'customers': {}}
    for email, data in rows:
        if email == '__meta__':
            out.update(json.loads(data))  # restore top-level keys (activity, etc.)
        else:
            out['customers'][email] = json.loads(data)
    return out

def save_db(db):
    now = time.time()
    conn = _sql()
    with conn:
        live = set(db.get('customers', {}).keys())
        existing = {r[0] for r in conn.execute('SELECT email FROM customers').fetchall()}
        for gone in existing - live:
            conn.execute('DELETE FROM customers WHERE email=?', (gone,))
        for email, c in db.get('customers', {}).items():
            conn.execute('INSERT OR REPLACE INTO customers (email, data, updated) VALUES (?,?,?)',
                         (email, json.dumps(c), now))
        # top-level keys (activity feed etc.) persist under a reserved row
        extra = {k: v for k, v in db.items() if k != 'customers'}
        conn.execute('INSERT OR REPLACE INTO customers (email, data, updated) VALUES (?,?,?)',
                     ('__meta__', json.dumps(extra), now))
    conn.close()
    try:
        DB.write_text(json.dumps(db, indent=2))  # JSON mirror as live backup
    except Exception as e:
        print(f'[db] JSON mirror write failed (non-fatal): {e}')

def or_request(method, path, key, payload=None):
    body = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(f'https://openrouter.ai/api/v1{path}', data=body, method=method, headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

def get_key_usage(api_key):
    """Read a customer key's usage/limits. Works with the key itself."""
    try:
        return or_request('GET', '/key', api_key)['data']
    except Exception as e:
        return {'error': str(e)}

def provision_key(name, daily_limit):
    """Create a new customer key with a daily-reset limit. Needs management key."""
    return or_request('POST', '/keys', MGMT_KEY, {
        'name': name, 'limit': daily_limit, 'limit_reset': 'daily'})

def update_key_limit(key_hash, daily_limit):
    return or_request('PATCH', f'/keys/{key_hash}', MGMT_KEY, {
        'limit': daily_limit, 'limit_reset': 'daily'})





# ---- Auto-fix snippet generator ----
def seo_fix_snippets(page_audit, business_name='', url=''):
    """Generate copy-paste HTML fixes for failing checks."""
    fixes = []
    for ch in page_audit.get('checks', []):
        if ch['ok']:
            continue
        name = ch['name']
        if name == 'Title tag present' or name == 'Title length 30–65 chars':
            fixes.append({'check': name, 'fix': f'<title>{business_name or "Your Business"} — {(page_audit.get("h1") or ["Services"])[0] or "Services"}</title>',
                          'where': 'In the <head> of your page', 'impact': 'Controls the clickable headline in Google search results.'})
        elif name == 'Meta description present' or name == 'Description length 70–160 chars':
            desc = f'{business_name or "We"} provide professional services in your area. Free quotes, insured, 5-star rated. Call today for a free estimate.'
            fixes.append({'check': name, 'fix': f'<meta name="description" content="{desc[:160]}">',
                          'where': 'In the <head>, right after the <title> tag', 'impact': 'This text appears under your title in Google — it directly affects click-through rate.'})
        elif name == 'Exactly one H1':
            fixes.append({'check': name, 'fix': f'<h1>{business_name or "Your Business"} — {page_audit.get("title", "Professional Services")}</h1>',
                          'where': 'The main visible heading at the top of your page (replace any existing H1s)', 'impact': 'Google uses the H1 to understand what the page is about. Multiple H1s confuse it.'})
        elif name == 'Structured data (schema.org)':
            schema = {
                "@context": "https://schema.org", "@type": "LocalBusiness",
                "name": business_name or "Your Business",
                "url": url,
                "description": page_audit.get('description', '') or f"Professional services by {business_name or 'Your Business'}",
                "address": {"@type": "PostalAddress", "addressLocality": "Your City", "addressRegion": "ON", "addressCountry": "CA"},
                "telephone": "(555) 555-5555",
                "priceRange": "$$"
            }
            fixes.append({'check': name, 'fix': f'<script type="application/ld+json">\n{json.dumps(schema, indent=2)}\n</script>',
                          'where': 'In the <head> or right before </body>', 'impact': 'Enables rich snippets (stars, hours, phone) in Google search results.'})
        elif name == 'Canonical tag':
            fixes.append({'check': name, 'fix': f'<link rel="canonical" href="{url or page_audit.get("url", "https://yourdomain.com")}">',
                          'where': 'In the <head>', 'impact': 'Prevents duplicate content penalties if the same page is accessible via multiple URLs.'})
        elif name == 'Mobile viewport tag':
            fixes.append({'check': name, 'fix': '<meta name="viewport" content="width=device-width, initial-scale=1">',
                          'where': 'In the <head>', 'impact': 'Required for mobile-first indexing — Google ranks mobile-friendly pages higher.'})
        elif name == 'Content length':
            fixes.append({'check': name, 'fix': f'Expand your page content to 800+ words. Cover: what you do, who you serve, why choose you, service area, process, testimonials, FAQ. Current: ~{page_audit.get("text_len", 0)} chars.',
                          'where': 'Main page body', 'impact': 'Thin pages rank poorly. Google wants comprehensive, helpful content.'})
        elif name == 'Internal links':
            fixes.append({'check': name, 'fix': 'Add 3–5 internal links to your most important pages (services, about, contact). Use descriptive anchor text like "tree removal services" instead of "click here".',
                          'where': 'Throughout your page content', 'impact': 'Internal links help Google discover and rank your other pages.'})
        elif name == 'Images have alt text':
            alt_example = 'Professional tree removal in Kitchener by ' + (business_name or 'our team')
            fixes.append({'check': name, 'fix': 'Add alt text to every image: <img src="tree-removal.jpg" alt="' + alt_example + '">. Describe what\'s in the image and include your location/service.',
                          'where': 'Every <img> tag', 'impact': 'Google Images drives traffic. Alt text is also required for accessibility.'})
        elif name == 'HTTPS':
            fixes.append({'check': name, 'fix': "Install an SSL certificate. Most hosts (Hostinger, cPanel) offer free Let's Encrypt SSL. Then force HTTPS in your .htaccess: RewriteEngine On / RewriteCond %{HTTPS} off / RewriteRule ^(.*)$ https://%{HTTP_HOST}%{REQUEST_URI} [L,R=301]",
                          'where': 'Server config / .htaccess', 'impact': 'Google flags non-HTTPS sites as "Not Secure" and ranks them lower.'})
        elif name == 'Fast load (<2.5s)':
            fixes.append({'check': name, 'fix': f'Page took {page_audit.get("load_ms", 0)}ms. Fixes: compress images (WebP), enable caching, minify CSS/JS, use a CDN. Test with PageSpeed Insights.',
                          'where': 'Server + asset optimization', 'impact': 'Slow pages lose rankings and visitors. 53% of mobile users leave after 3s.'})
    return fixes

# ---- Multi-page crawler ----
def seo_crawl_site(base_url, max_pages=20):
    """Crawl a site up to max_pages, return per-page audit + aggregate issues."""
    from urllib.parse import urljoin, urlparse
    import html.parser
    if not base_url.startswith(('http://', 'https://')):
        base_url = 'https://' + base_url
    base_domain = urlparse(base_url).netloc
    visited = set()
    queue = [base_url]
    pages = []
    all_issues = []

    class P(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ''; self._in_title = False
            self.meta = {}; self.headings = {'h1': [], 'h2': [], 'h3': []}
            self._h = None; self._buf = ''
            self.imgs = 0; self.imgs_no_alt = 0
            self.links = []
            self.text_len = 0
            self.has_viewport = False; self.canonical = ''; self.schema = False
            self._skip = 0
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'title': self._in_title = True
            if tag in ('script', 'style'): self._skip += 1
            if tag == 'meta':
                n = (a.get('name') or a.get('property') or '').lower()
                if n: self.meta[n] = a.get('content', '')
                if n == 'viewport': self.has_viewport = True
            if tag in self.headings: self._h = tag; self._buf = ''
            if tag == 'img':
                self.imgs += 1
                if not a.get('alt', '').strip(): self.imgs_no_alt += 1
            if tag == 'a':
                h = a.get('href', '')
                if h: self.links.append(h)
            if tag == 'link' and a.get('rel') == 'canonical': self.canonical = a.get('href', '')
            if tag == 'script' and a.get('type') == 'application/ld+json': self.schema = True
        def handle_endtag(self, tag):
            if tag == 'title': self._in_title = False
            if tag in ('script', 'style') and self._skip: self._skip -= 1
            if tag == self._h:
                txt = ' '.join(self._buf.split())[:120]
                if txt: self.headings[tag].append(txt)
                self._h = None
        def handle_data(self, d):
            if self._in_title: self.title += d
            if self._h is not None: self._buf += d + ' '
            if not self._skip: self.text_len += len(d)

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ANOSagent-SEO/1.0)'})
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read(512 * 1024).decode('utf-8', 'replace')
                final_url = r.geturl()
                status = r.status
            load_ms = int((time.time() - t0) * 1000)
        except Exception as e:
            pages.append({'url': url, 'error': str(e)[:200], 'score': 0})
            continue

        p = P(); p.feed(raw)
        title = ' '.join(p.title.split())[:200]
        desc = p.meta.get('description', '')
        checks = []
        def chk(name, ok, weight, hint):
            checks.append({'name': name, 'ok': bool(ok), 'weight': weight, 'hint': hint})
        chk('Title tag present', title, 10, 'Add a unique <title> describing the page.')
        chk('Title length 30–65 chars', 30 <= len(title) <= 65, 8, f'Title is {len(title)} chars — aim for 30–65.')
        chk('Meta description present', desc, 10, 'Add a <meta name="description"> — it controls the search snippet.')
        chk('Description length 70–160 chars', 70 <= len(desc) <= 160, 8, f'Description is {len(desc)} chars — aim for 70–160.')
        chk('Exactly one H1', len(p.headings['h1']) == 1, 10, f'Found {len(p.headings["h1"])} H1s — use exactly one per page.')
        chk('H2 structure', len(p.headings['h2']) >= 1, 6, 'Add H2 subheadings so Google understands the page structure.')
        chk('Images have alt text', p.imgs == 0 or p.imgs_no_alt == 0, 8, f'{p.imgs_no_alt} of {p.imgs} images missing alt text.')
        chk('Mobile viewport tag', p.has_viewport, 8, 'Missing <meta name="viewport"> — critical for mobile ranking.')
        chk('Canonical tag', p.canonical, 5, 'Add a <link rel="canonical"> to avoid duplicate-content issues.')
        chk('Structured data (schema.org)', p.schema, 8, 'Add JSON-LD schema (LocalBusiness, Service, FAQ) for rich results.')
        chk('Internal links', len([l for l in p.links if l.startswith('/') or base_domain in l]) >= 3, 6, f'Only {len([l for l in p.links if l.startswith("/") or base_domain in l])} internal links — link your key pages together.')
        chk('Content length', p.text_len >= 800, 8, f'~{p.text_len} chars of text — thin pages rank poorly; aim 800+.')
        chk('HTTPS', final_url.startswith('https://'), 5, 'Serve the site over HTTPS.')
        chk('Fast load (<2.5s)', load_ms < 2500, 5, f'Page took {load_ms}ms to respond — aim under 2.5s.')
        total = sum(c['weight'] for c in checks)
        got = sum(c['weight'] for c in checks if c['ok'])
        score = round(100 * got / total) if total else 0

        # collect internal links for crawling
        for l in p.links:
            abs_url = urljoin(final_url, l)
            parsed = urlparse(abs_url)
            if parsed.netloc == base_domain and parsed.scheme in ('http', 'https'):
                clean = parsed.scheme + '://' + parsed.netloc + parsed.path.rstrip('/')
                if clean not in visited and clean not in queue and not any(x in clean for x in ['.jpg', '.png', '.pdf', '.css', '.js', '#']):
                    queue.append(clean)

        page = {'url': final_url, 'status': status, 'score': score, 'load_ms': load_ms,
                'title': title, 'description': desc, 'h1': p.headings['h1'], 'h2': p.headings['h2'][:10],
                'imgs': p.imgs, 'imgs_no_alt': p.imgs_no_alt,
                'links_internal': len([l for l in p.links if l.startswith('/') or base_domain in l]),
                'links_external': len([l for l in p.links if l.startswith('http') and base_domain not in l]),
                'text_len': p.text_len, 'checks': checks, 'ts': time.time()}
        pages.append(page)

        for ch in checks:
            if not ch['ok']:
                all_issues.append({'page': final_url, 'check': ch['name'], 'hint': ch['hint']})

    avg_score = round(sum(p['score'] for p in pages) / len(pages)) if pages else 0
    return {'pages': pages, 'avg_score': avg_score, 'total_pages': len(pages),
            'issues': all_issues, 'base_url': base_url, 'ts': time.time()}

# ---- SEO audit engine ----
def seo_audit_url(url):
    """Fetch a page and produce a basic on-page SEO audit. Returns dict with score + checks."""
    import html.parser
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; ANOSagent-SEO/1.0)'})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(1024 * 1024).decode('utf-8', 'replace')
        final_url = r.geturl()
        status = r.status
    load_ms = int((time.time() - t0) * 1000)

    class P(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.title = ''; self._in_title = False
            self.meta = {}
            self.headings = {'h1': [], 'h2': [], 'h3': []}
            self._h = None; self._buf = ''
            self.imgs = 0; self.imgs_no_alt = 0
            self.links_internal = 0; self.links_external = 0
            self.text_len = 0
            self.has_viewport = False
            self.canonical = ''
            self.schema = False
            self._skip = 0
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'title': self._in_title = True
            if tag in ('script', 'style'): self._skip += 1
            if tag == 'meta':
                n = (a.get('name') or a.get('property') or '').lower()
                if n: self.meta[n] = a.get('content', '')
                if n == 'viewport': self.has_viewport = True
            if tag in self.headings: self._h = tag; self._buf = ''
            if tag == 'img':
                self.imgs += 1
                if not a.get('alt', '').strip(): self.imgs_no_alt += 1
            if tag == 'a':
                h = a.get('href', '')
                if h.startswith('http'):
                    from urllib.parse import urlparse
                    if urlparse(h).netloc != urlparse(final_url).netloc: self.links_external += 1
                    else: self.links_internal += 1
                elif h.startswith('/'):
                    self.links_internal += 1
            if tag == 'link' and a.get('rel') == 'canonical': self.canonical = a.get('href', '')
            if tag == 'script' and a.get('type') == 'application/ld+json': self.schema = True
        def handle_endtag(self, tag):
            if tag == 'title': self._in_title = False
            if tag in ('script', 'style') and self._skip: self._skip -= 1
            if tag == self._h:
                txt = ' '.join(self._buf.split())[:120]
                if txt: self.headings[tag].append(txt)
                self._h = None
        def handle_data(self, d):
            if self._in_title: self.title += d
            if self._h is not None: self._buf += d + ' '
            if not self._skip: self.text_len += len(d)

    p = P(); p.feed(raw)
    title = ' '.join(p.title.split())[:200]
    desc = p.meta.get('description', '')
    checks = []
    def chk(name, ok, weight, hint):
        checks.append({'name': name, 'ok': bool(ok), 'weight': weight, 'hint': hint})
    chk('Title tag present', title, 10, 'Add a unique <title> describing the page.')
    chk('Title length 30–65 chars', 30 <= len(title) <= 65, 8, f'Title is {len(title)} chars — aim for 30–65.')
    chk('Meta description present', desc, 10, 'Add a <meta name="description"> — it controls the search snippet.')
    chk('Description length 70–160 chars', 70 <= len(desc) <= 160, 8, f'Description is {len(desc)} chars — aim for 70–160.')
    chk('Exactly one H1', len(p.headings['h1']) == 1, 10, f'Found {len(p.headings["h1"])} H1s — use exactly one per page.')
    chk('H2 structure', len(p.headings['h2']) >= 1, 6, 'Add H2 subheadings so Google understands the page structure.')
    chk('Images have alt text', p.imgs == 0 or p.imgs_no_alt == 0, 8, f'{p.imgs_no_alt} of {p.imgs} images missing alt text.')
    chk('Mobile viewport tag', p.has_viewport, 8, 'Missing <meta name="viewport"> — critical for mobile ranking.')
    chk('Canonical tag', p.canonical, 5, 'Add a <link rel="canonical"> to avoid duplicate-content issues.')
    chk('Structured data (schema.org)', p.schema, 8, 'Add JSON-LD schema (LocalBusiness, Service, FAQ) for rich results.')
    chk('Internal links', p.links_internal >= 3, 6, f'Only {p.links_internal} internal links — link your key pages together.')
    chk('Content length', p.text_len >= 800, 8, f'~{p.text_len} chars of text — thin pages rank poorly; aim 800+.')
    chk('HTTPS', final_url.startswith('https://'), 5, 'Serve the site over HTTPS.')
    chk('Fast load (<2.5s)', load_ms < 2500, 5, f'Page took {load_ms}ms to respond — aim under 2.5s.')
    total = sum(c['weight'] for c in checks)
    got = sum(c['weight'] for c in checks if c['ok'])
    score = round(100 * got / total) if total else 0
    return {'url': final_url, 'status': status, 'score': score, 'load_ms': load_ms,
            'title': title, 'description': desc,
            'h1': p.headings['h1'], 'h2': p.headings['h2'][:10],
            'imgs': p.imgs, 'imgs_no_alt': p.imgs_no_alt,
            'links_internal': p.links_internal, 'links_external': p.links_external,
            'text_len': p.text_len, 'checks': checks, 'ts': time.time()}

# ---------- Agent tools (real actions, gated by feature access) ----------
def notion_headers(c):
    integ = c.get('integrations', {}).get('notion', {})
    key = integ.get('api_key', '')
    dbid = integ.get('database', '')
    return key, dbid

def crm_query(c):
    key, dbid = notion_headers(c)
    if not key: raise Exception('Notion not connected')
    req = urllib.request.Request(f'https://api.notion.com/v1/data_sources/{dbid}/query',
        data=json.dumps({}).encode(), method='POST', headers={
            'Authorization': f'Bearer {key}', 'Notion-Version': '2025-09-03', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    rows = []
    for p in d.get('results', [])[:20]:
        props = p.get('properties', {})
        row = {k: _plain_val(v) for k, v in props.items() if k != 'id'}
        rows.append(row)
    return rows or 'CRM is empty'

CRM_STAGES = ['Lead', 'Quote Sent', 'In Progress', 'Booked/Approved', 'Job Completed', 'Lost Lead']

def _crm_leads(c):
    return c.setdefault('crm_leads', [])

def _crm_stages(c):
    """Per-customer pipeline stages (owner can add/rename/reorder from the pipeline page)."""
    return c.setdefault('crm_stages', list(CRM_STAGES))

def _crm_stats(leads, stages=None):
    open_stages = tuple(s for s in (stages or CRM_STAGES) if s not in ('Job Completed', 'Lost Lead')) \
                  or ('Lead', 'Quote Sent', 'In Progress', 'Booked/Approved')
    open_quotes = sum((l.get('amount') or 0) for l in leads if l.get('stage') == 'Quote Sent')
    pipeline_val = sum((l.get('amount') or 0) for l in leads if l.get('stage') in open_stages)
    won = [l for l in leads if l.get('stage') == 'Job Completed']
    decided = [l for l in leads if l.get('stage') in ('Job Completed', 'Lost Lead')]
    return {
        'total': len(leads),
        'open': sum(1 for l in leads if l.get('stage') in open_stages),
        'open_quotes': open_quotes,
        'pipeline_value': pipeline_val,
        'won_value': sum((l.get('amount') or 0) for l in won),
        'win_rate': round(100 * len(won) / len(decided)) if decided else 0,
    }


# ---------- Twilio (per-customer BYO keys) ----------
# Platform numbers: one master Twilio account; each customer gets a subaccount
# and buys numbers into it from the portal. Per-text charges come out of a
# per-customer SMS wallet (CAD), topped up via Stripe.
TWILIO_MASTER_SID = ENV.get('TWILIO_MASTER_SID', '')
TWILIO_MASTER_TOKEN = ENV.get('TWILIO_MASTER_TOKEN', '')
SMS_PRICE_CAD = 0.03           # charged per text on a platform-bought number
NUMBER_ACTIVATION_CAD = 5.00   # one-time activation fee, from wallet

def twilio_conf(c):
    t = c.get('twilio') or {}
    if t.get('sid') and t.get('token') and t.get('from'):
        return t
    sub = c.get('twilio_sub') or {}
    if sub.get('sid') and sub.get('token') and sub.get('number'):
        return {'sid': sub['sid'], 'token': sub['token'], 'from': sub['number'], 'platform': True}
    return None

def twilio_api(sid, token, method, path, params=None):
    """Minimal Twilio REST call (no SDK). Returns (ok, dict_or_errstr)."""
    import base64
    url = 'https://api.twilio.com/2010-04-01' + path
    data = None
    if method == 'POST':
        data = urllib.parse.urlencode(params or {}).encode()
    elif params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Authorization', 'Basic ' + base64.b64encode(f'{sid}:{token}'.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode() or '{}')
        except Exception:
            d = {}
        return False, d.get('message') or f'Twilio HTTP {e.code}'
    except Exception as e:
        return False, str(e)

def _twilio_sub(c):
    return c.setdefault('twilio_sub', {})

def _sms_wallet(c):
    return c.setdefault('sms_wallet', {'balance': 0.0, 'ledger': []})

def _wallet_charge(c, amount, desc):
    w = _sms_wallet(c)
    w['balance'] = round((w.get('balance') or 0) - amount, 2)
    w.setdefault('ledger', []).append({'ts': time.time(), 'amount': -round(amount, 2), 'desc': desc})
    w['ledger'] = w['ledger'][-100:]

def twilio_send(c, to, body):
    """Send SMS via the customer's Twilio account. Returns (ok, err)."""
    t = twilio_conf(c)
    if not t:
        return False, 'Twilio not configured — add it in Settings → Integrations'
    if t.get('platform') and (_sms_wallet(c).get('balance') or 0) < SMS_PRICE_CAD:
        return False, 'Out of SMS credit — top up in Settings → Integrations'
    import base64
    url = f'https://api.twilio.com/2010-04-01/Accounts/{t["sid"]}/Messages.json'
    data = urllib.parse.urlencode({'To': to, 'From': t['from'], 'Body': body}).encode()
    req = urllib.request.Request(url, data=data)
    auth = base64.b64encode(f'{t["sid"]}:{t["token"]}'.encode()).decode()
    req.add_header('Authorization', 'Basic ' + auth)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
            ok, err = bool(d.get('sid')), d.get('message')
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
            ok, err = False, d.get('message', f'Twilio HTTP {e.code}')
        except Exception:
            ok, err = False, f'Twilio HTTP {e.code}'
    except Exception as e:
        ok, err = False, str(e)
    if ok and t.get('platform'):
        # charge the platform wallet (reload the record so we don't clobber other writes)
        try:
            db = load_db()
            live = (db.get('customers') or {}).get(c.get('email') or '')
            if live is not None:
                _wallet_charge(live, SMS_PRICE_CAD, 'SMS to ' + str(to))
                save_db(db)
        except Exception:
            pass
    return ok, err

def stamp_signed_pdf(c, doc, submitted_fields):
    """Burn signature images + text fields into the PDF. Returns output path or None."""
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
        import io as _io, base64 as _b64s, os as _oss

        url = doc.get('url', '')
        if str(url).startswith('http'):
            return None  # only stamp local uploads
        src_path = BASE / 'uploads' / str(url).lstrip('/')[len('uploads/'):] if str(url).startswith('/uploads/') else BASE / str(url).lstrip('/')
        if not src_path.exists():
            return None

        reader = PdfReader(str(src_path))
        writer = PdfWriter()
        # group fields by page
        by_page = {}
        for f in submitted_fields:
            v = f.get('value') or {}
            has_sig = bool(v.get('dataUrl'))
            has_text = bool((v.get('text') or '').strip())
            if not (has_sig or has_text):
                continue
            by_page.setdefault(int(f.get('page', 1)), []).append(f)

        for pnum, page in enumerate(reader.pages, start=1):
            pw = float(page.mediabox.width)
            ph = float(page.mediabox.height)
            if pnum in by_page:
                buf = _io.BytesIO()
                cv = rl_canvas.Canvas(buf, pagesize=(pw, ph))
                for f in by_page[pnum]:
                    v = f.get('value') or {}
                    # fractions -> PDF points (y measured from bottom in PDF)
                    x = float(f.get('x', 0)) * pw
                    w = float(f.get('w', 0.3)) * pw
                    h = float(f.get('h', 0.07)) * ph
                    y = (1.0 - float(f.get('y', 0)) - float(f.get('h', 0.07))) * ph
                    dv = v.get('dataUrl') or ''
                    mm = re.match(r'^data:image/\w+;base64,(.+)$', dv, re.S)
                    if mm:
                        try:
                            img = ImageReader(_io.BytesIO(_b64s.b64decode(mm.group(1))))
                            cv.drawImage(img, x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
                        except Exception:
                            pass
                    txt = (v.get('text') or '').strip()
                    if txt:
                        try:
                            fs = max(8, min(16, h * 0.55))
                            cv.setFont('Helvetica', fs)
                            cv.setFillColorRGB(0.1, 0.13, 0.2)
                            cv.drawCentredString(x + w / 2, y + (h - fs) / 2 + fs * 0.35, txt[:120])
                        except Exception:
                            pass
                cv.save()
                buf.seek(0)
                overlay = PdfReader(buf).pages[0]
                page.merge_page(overlay)
            writer.add_page(page)

        owner = re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
        out_dir = BASE / 'uploads' / owner / 'docs'
        _oss.makedirs(out_dir, exist_ok=True)
        out_path = out_dir / (doc['id'] + '_signed.pdf')
        with open(out_path, 'wb') as fh:
            writer.write(fh)
        return '/uploads/' + owner + '/docs/' + doc['id'] + '_signed.pdf'
    except Exception as e:
        print(f'[sign] pdf stamp failed: {e!r}')
        return None


def send_photo_to_client(c, lead, photo, caption=''):
    """MMS via Quo if connected; else SMS with link via Twilio. Returns (ok, detail)."""
    phone = (lead.get('phone') or '').strip()
    if not phone:
        return False, 'No phone number on this lead'
    biz = c.get('business') or (c.get('settings') or {}).get('business_name') or 'your contractor'
    base = _public_base(c)
    ptok = photo.get('ptok') if isinstance(photo, dict) else None
    if isinstance(photo, str):
        photo_url = photo
    else:
        photo_url = (photo or {}).get('url', '')
    if ptok:
        full_url = base + '/photo/' + ptok
    else:
        full_url = photo_url if str(photo_url).startswith('http') else base + photo_url
    if not caption and isinstance(photo, dict):
        caption = photo.get('caption', '')
    text = f"{biz}: photo from {lead.get('address', 'your property')}" + (f" — {caption}" if caption else '')
    try:
        integ = (c.get('integrations') or {}).get('quo') or {}
        if integ.get('api_key'):
            ok, info = quo.send_mms(c, phone, text, [full_url])
            if ok:
                return True, 'MMS sent via Quo'
            print('[quo mms fallback]', info)
    except Exception as e:
        print('[quo mms error]', e)
    try:
        t = twilio_conf(c)
        if t:
            import base64 as _b64m
            url = f'https://api.twilio.com/2010-04-01/Accounts/{t["sid"]}/Messages.json'
            data = urllib.parse.urlencode({'To': phone, 'From': t['from'], 'Body': text, 'MediaUrl': full_url}).encode()
            req = urllib.request.Request(url, data=data)
            auth = _b64m.b64encode(f'{t["sid"]}:{t["token"]}'.encode()).decode()
            req.add_header('Authorization', 'Basic ' + auth)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = json.load(r)
                    if d.get('sid'):
                        return True, 'MMS sent via Twilio'
            except urllib.error.HTTPError:
                ok, err = twilio_send(c, phone, text + ' ' + full_url)
                if ok:
                    return True, 'SMS with photo link sent'
                return False, err
    except Exception as e:
        print('[twilio mms error]', e)
    return False, 'No messaging route — connect Quo or Twilio in Settings → Integrations'


def client_notify(c, client_email, kind, text, ts=None):
    if not client_email:
        return
    feed = c.setdefault('client_notifications', {})
    lst = feed.setdefault(client_email.lower().strip(), [])
    lst.append({'kind': kind, 'text': str(text)[:200], 'ts': ts or time.time(), 'read': False})
    del lst[:-40]


def _extra_notify_phones(c, lead_id):
    """Extra phone numbers clients registered for SMS alerts on a given property (lead)."""
    phones = set()
    for p in _client_portals(c):
        for entry in (p.get('notify_phones') or []):
            if lead_id in (entry.get('property_ids') or []):
                ph = (entry.get('phone') or '').strip()
                if ph:
                    phones.add(ph)
    # legacy single portal
    cp = c.get('client_portal') or {}
    for entry in (cp.get('notify_phones') or []):
        if lead_id in (entry.get('property_ids') or []):
            ph = (entry.get('phone') or '').strip()
            if ph:
                phones.add(ph)
    return phones

def fire_route_notifications(c, leads):
    """After a stop update: text clients who are 2 stops away. Best-effort, never raises."""
    try:
        due = snow.stops_due_notification(c, leads)
        for lead, stop in due:
            msg = f"Heads up from {c.get('business', 'your snow crew')}: the plow is 2 stops away from {lead.get('address', 'you')}. Please clear the driveway. 🚜"
            targets = set()
            if lead.get('phone'):
                targets.add(lead['phone'])
            targets |= _extra_notify_phones(c, lead.get('id'))
            any_ok = False
            for ph in targets:
                ok, err = twilio_send(c, ph, msg)
                if ok:
                    any_ok = True
                else:
                    print(f"[SMS] failed to {ph}: {err}")
            if any_ok:
                stop['notified'] = True
                try:
                    client_notify(c, (lead.get('email') or '').lower().strip(), 'route',
                                  'Plow is 2 stops away from ' + (lead.get('address') or 'you'))
                except Exception:
                    pass
    except Exception as e:
        print(f"[SMS] notify error: {e}")

def crm_leads(c):
    """Per-customer lead list stored on the customer record, newest first."""
    leads = _crm_leads(c)
    return sorted(leads, key=lambda l: l.get('created', 0), reverse=True)

# ---- Clients & client portal ----

def _expenses(c):
    return c.setdefault('expenses', [])

def _expense_by_id(c, eid):
    return next((x for x in _expenses(c) if x.get('id') == eid), None)

def _clients(c):
    """New model: clients — each has many properties. Property = a lead record
    promoted (keeps its id so routes/docs/invoices keep working)."""
    return c.setdefault('clients', [])

def _client_by_id(c, cid):
    return next((x for x in _clients(c) if x.get('id') == cid), None)

def _find_client_for_lead(c, lead):
    """Match an existing client by email (primary) then phone."""
    em = (lead.get('email') or '').lower().strip()
    ph = ''.join(ch for ch in (lead.get('phone') or '') if ch.isdigit())[-10:]
    for x in _clients(c):
        if em and (x.get('email') or '').lower().strip() == em:
            return x
    if ph and len(ph) == 10:
        for x in _clients(c):
            xp = ''.join(ch for ch in (x.get('phone') or '') if ch.isdigit())[-10:]
            if xp and xp == ph:
                return x
    return None

def convert_lead_to_client(c, lead):
    """Create/find the client for this lead; mark the lead as a property of that client.
    Returns (client, is_new_client)."""
    import uuid as _u
    cl = _find_client_for_lead(c, lead)
    is_new = False
    if not cl:
        cl = {'id': _u.uuid4().hex[:12],
              'name': lead.get('client') or lead.get('title') or 'Client',
              'email': (lead.get('email') or '').strip(),
              'phone': (lead.get('phone') or '').strip(),
              'created': time.time()}
        _clients(c).append(cl)
        is_new = True
    lead['client_id'] = cl['id']
    lead['prop_status'] = 'Active' if lead.get('stage') != 'Job Completed' else 'Completed'
    return cl, is_new

def _migrate_clients(c):
    """One-time: every lead in Booked/Approved or Job Completed becomes a property;
    leads group into clients by email/phone. Idempotent (skips leads with client_id)."""
    if c.get('_clients_migrated'):
        return False
    changed = False
    for l in _crm_leads(c):
        if l.get('client_id'):
            continue
        if l.get('stage') in ('Booked/Approved', 'Job Completed'):
            convert_lead_to_client(c, l)
            changed = True
    c['_clients_migrated'] = True
    return changed

def _properties_of_client(c, client_id):
    return [l for l in _crm_leads(c) if l.get('client_id') == client_id]

def _all_properties(c):
    return [l for l in _crm_leads(c) if l.get('client_id')]

def _client_portal(c):
    return c.setdefault('client_portal', {'enabled': False, 'client_email': '', 'client_name': '',
                                          'temp_password': '', 'token': ''})

def _client_profile(c):
    return c.setdefault('client_profile', {'phone': '', 'address': '', 'notes': ''})

def _client_invoices(c):
    return c.setdefault('client_invoices', [])

def _contracts(c):
    """Seasonal service contracts (snow etc.) — billing plan + roll-up; invoicing stays manual."""
    return c.setdefault('contracts', [])

def _contract_rollup(c, ct):
    billed = collected = 0.0
    for iv in _client_invoices(c):
        if iv.get('contract_id') != ct.get('id') or iv.get('status') == 'void':
            continue
        billed += iv.get('total') or 0
        collected += sum(p.get('amount') or 0 for p in (iv.get('payments') or []))
    # contract total is pre-tax; with hst the client owes gross (total × 1.13)
    expected = round((ct.get('seasonal_total') or 0) * (1.13 if ct.get('hst') else 1), 2)
    remaining = round(expected - collected, 2)
    return {'billed': round(billed, 2), 'collected': round(collected, 2),
            'expected': expected, 'remaining': remaining}

def get_client_status(c, portal_override=None):
    """Summarize client-facing project state from the owner's leads/estimates.
    portal_override: a multi-portal login dict — if given, its email/property_ids drive the filter."""
    leads = crm_leads(c)
    ests = sorted(c.get('estimates', []), key=lambda e: e.get('created', 0), reverse=True)
    cp = _client_portal(c)
    if portal_override:
        client_email = (portal_override.get('email') or '').lower().strip()
        linked_ids = set(portal_override.get('property_ids') or [])
        client_name = portal_override.get('name', '')
    else:
        client_email = (cp.get('client_email') or '').lower().strip()
        linked_ids = set(cp.get('property_ids') or [])
        client_name = cp.get('client_name', '')

    def _is_clients(lead=None, est=None):
        if not client_email and not linked_ids:
            return portal_override is None  # legacy preview only — a real (multi-)portal login with no email/properties sees nothing
        if lead is not None:
            if (lead.get('email') or '').lower().strip() == client_email:
                return True
            if lead.get('id') in linked_ids:
                return True
            return False
        if est is not None:
            if (est.get('client_email') or '').lower().strip() == client_email:
                return True
            if est.get('lead_id') in linked_ids:
                return True
            if est.get('lead_id'):
                linked = next((l for l in leads if l.get('id') == est['lead_id']), None)
                return bool(linked) and (linked.get('email') or '').lower().strip() == client_email
            return False
        return False

    timeline = []
    media = []
    projects = []
    for l in leads:
        if not _is_clients(lead=l):
            continue
        stage = l.get('stage', 'Lead')
        if stage in ('Booked/Approved', 'Job Completed'):
            proj = {
                'type': 'project',
                'id': l.get('id'),
                'title': l.get('title') or l.get('address') or 'Your project',
                'stage': stage,
                'amount': l.get('amount'),
                'date': l.get('job_date') or l.get('created'),
                'address': l.get('address', ''),
            }
            timeline.append(proj)
            projects.append(proj)
        for a in (l.get('activities') or []):
            if a.get('type') == 'note':
                # only client-visible notes reach the portal timeline; internal CRM
                # notes (migrated data, call logs, staff updates) stay internal
                if not a.get('client_visible'):
                    continue
                timeline.append({
                    'type': 'note',
                    'title': l.get('title') or l.get('client') or 'Project',
                    'text': a.get('text', ''),
                    'by': a.get('by', 'owner'),
                    'ts': a.get('ts', 0),
                })
            elif a.get('type') == 'photo':
                timeline.append({
                    'type': 'photo',
                    'title': l.get('title') or l.get('client') or 'Project',
                    'text': a.get('text', ''),
                    'url': a.get('url', ''),
                    'by': a.get('by', 'owner'),
                    'ts': a.get('ts', 0),
                })
        for p in (l.get('photos') or []):
            media.append({
                'url': p.get('url', ''), 'caption': p.get('caption', ''),
                'ts': p.get('ts', 0), 'by': p.get('by', 'owner'),
                'project': l.get('title') or l.get('client') or 'Project',
                'lead_id': l.get('id'),
            })
    for e in ests:
        if e.get('status') in ('sent', 'approved', 'declined'):
            if not _is_clients(est=e):
                continue
            timeline.append({
                'type': 'estimate',
                'number': e.get('number'),
                'status': e.get('status'),
                'total': e.get('total'),
                'client': e.get('client'),
                'date': e.get('created'),
            })
    def _sortkey(x):
        v = x.get('ts') or x.get('date') or 0
        if isinstance(v, str):
            try:
                return time.mktime(time.strptime(v[:10], '%Y-%m-%d'))
            except Exception:
                return 0
        return v
    timeline.sort(key=_sortkey, reverse=True)
    media.sort(key=_sortkey, reverse=True)
    # invoices visible to THIS client login: attached lead's email matches the
    # portal login, or the lead is one of their linked properties. Preview mode
    # (no email/properties on the login) sees everything.
    cinvs = []
    for iv in _client_invoices(c):
        if iv.get('status') not in ('sent', 'paid'):
            continue  # drafts and voided invoices stay internal
        if not client_email and not linked_ids:
            cinvs.append(iv)
            continue
        lid = iv.get('client_key') or ''
        if lid and lid in linked_ids:
            cinvs.append(iv)
            continue
        if lid:
            l = next((x for x in leads if x.get('id') == lid), None)
            if l and (l.get('email') or '').lower().strip() == client_email:
                cinvs.append(iv)
    # seasonal contracts tied to this client's properties (active only) + live roll-up
    ccontracts = []
    for ct in c.get('contracts', []):
        if ct.get('status') != 'active':
            continue
        pid = ct.get('property_id')
        own = (pid and pid in linked_ids) if (client_email or linked_ids) else (portal_override is None)
        if not own and client_email:
            ct_client = _client_by_id(c, ct.get('client_id'))
            own = bool(ct_client) and (ct_client.get('email') or '').lower().strip() == client_email
        if not own:
            continue
        roll = _contract_rollup(c, ct)
        ccontracts.append({
            'id': ct.get('id'), 'season': ct.get('season', ''), 'type': ct.get('type'),
            'start': ct.get('start'), 'end': ct.get('end'),
            'seasonal_total': ct.get('seasonal_total') or 0, 'hst': bool(ct.get('hst')),
            'monthly_rate': ct.get('monthly_rate') or 0,
            'package': ct.get('package') or '', 'trigger_cm': ct.get('trigger_cm') or 0,
            'billed': roll['billed'], 'collected': roll['collected'],
            'expected': roll['expected'], 'remaining': roll['remaining'],
        })
    return {
        'business': c.get('business', 'Your Contractor'),
        'client_name': client_name,
        'client_email': client_email,
        'branding_theme': (c.get('portal_branding') or {}).get('theme', 'dark'),
        'profile': _client_profile(c),
        'invoices': sorted(cinvs, key=lambda x: x.get('created', 0), reverse=True),
        'contracts': ccontracts,
        'payment_methods': (c.get('invoice_settings') or {}).get('payment_methods') or [],
        'timeline': timeline[:40],
        'projects': projects,
        'media': media[:60],
    }

# ---- Project photos (owner uploads → client timeline + gallery) ----

def _photo_dir(c):
    d = BASE / 'uploads' / re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
    d.mkdir(parents=True, exist_ok=True)
    return d

def _save_social_photo(c, data_url):
    """Store one base64 image for a social post under uploads/<owner>/social/; returns the url."""
    import base64, uuid
    m = re.match(r'^data:image/(png|jpeg|jpg|webp|gif);base64,(.+)$', data_url or '', re.S)
    if not m:
        raise ValueError('photo must be a png/jpg/webp/gif data URL')
    ext = {'jpeg': 'jpg'}.get(m.group(1), m.group(1))
    raw = base64.b64decode(m.group(2))
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('photo too large (8MB max)')
    d = _photo_dir(c) / 'social'
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    (d / fname).write_bytes(raw)
    return f'/uploads/{_photo_dir(c).name}/social/{fname}'

def _save_photo(c, lead, data_url, caption='', by='owner'):
    """Store one base64 photo on a lead; returns the public url or raises."""
    import base64, uuid
    m = re.match(r'^data:image/(png|jpeg|jpg|webp|gif|heic|heif);base64,(.+)$', data_url or '', re.S)
    if not m:
        raise ValueError('photo must be a png/jpg/webp/gif/heic data URL')
    ext = {'jpeg': 'jpg'}.get(m.group(1), m.group(1))
    raw = base64.b64decode(m.group(2))
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('photo too large (8MB max)')
    fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
    (_photo_dir(c) / fname).write_bytes(raw)
    url = f'/uploads/{_photo_dir(c).name}/{fname}'
    lead.setdefault('photos', []).append({
        'id': uuid.uuid4().hex[:10], 'url': url, 'caption': str(caption or '')[:500],
        'ptok': uuid.uuid4().hex[:20], 'ts': time.time(), 'by': by})
    lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'photo',
        'text': str(caption or 'Photo added')[:500], 'url': url, 'by': by})
    try:
        client_notify(c, (lead.get('email') or '').lower().strip(), 'photo',
                      'New photo from ' + (lead.get('address') or 'your property') + ((caption and (': ' + caption[:60])) or ''))
    except Exception:
        pass
    return url

def _find_customer_by_client_token(tok):
    """Look up which customer owns this client-portal token (legacy + multi)."""
    if not tok:
        return None, None
    db = load_db()
    for email, c in db['customers'].items():
        cp = c.get('client_portal') or {}
        if cp.get('token') and cp['token'] == tok:
            return db, c
        for p in _client_portals(c):
            if p.get('token') and p['token'] == tok:
                return db, c
    return None, None

def _get_bearer(self):
    return self.headers.get('Authorization', '').replace('Bearer ', '').strip()

def _plain_val(v):
    t = v.get('type')
    if t == 'title': return ''.join(x['plain_text'] for x in v.get('title', []))
    if t == 'rich_text': return ''.join(x['plain_text'] for x in v.get('rich_text', []))
    if t == 'number': return v.get('number')
    if t == 'select': return (v.get('select') or {}).get('name')
    if t == 'email' or t == 'phone_number': return v.get(t)
    if t == 'status': return (v.get('status') or {}).get('name')
    if t == 'date': return (v.get('date') or {}).get('start')
    if t == 'checkbox': return v.get('checkbox')
    if t in ('created_time', 'last_edited_time'): return v.get(t)
    return str(v.get(t, ''))[:100]

def _google_refresh(c, integ):
    """Refresh an OAuth access token; persists the new one. Returns token or ''."""
    rt = integ.get('refresh_token')
    if not rt:
        return ''
    try:
        data = urllib.parse.urlencode({
            'client_id': ENV.get('GOOGLE_OAUTH_CLIENT_ID', ''),
            'client_secret': ENV.get('GOOGLE_OAUTH_CLIENT_SECRET', ''),
            'refresh_token': rt, 'grant_type': 'refresh_token'}).encode()
        req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data,
            method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
        integ['access_token'] = tok.get('access_token', '')
        integ['expires'] = int(time.time()) + int(tok.get('expires_in', 3600))
        db = load_db()
        if c['email'] in db['customers']:
            db['customers'][c['email']]['integrations']['gmail'] = integ
            save_db(db)
        return integ['access_token']
    except Exception as e:
        print('[OAUTH refresh]', e)
        return ''

def gmail_api_send(c, to, subject, body, integ):
    """Send via Gmail API using OAuth token."""
    import base64
    from email.utils import formataddr
    prof = c.get('settings_profile') or {}
    addr = integ.get('email') or ''
    frm = formataddr((prof.get('from_name'), addr)) if prof.get('from_name') else addr
    rt = f"Reply-To: {prof.get('reply_to')}\r\n" if prof.get('reply_to') else ''
    raw = f"From: {frm}\r\nTo: {to}\r\n{rt}Subject: {subject}\r\n\r\n{body}"
    msg = base64.urlsafe_b64encode(raw.encode()).decode()
    tok = integ.get('access_token', '')
    if int(integ.get('expires', 0)) - 60 < int(time.time()):
        tok = _google_refresh(c, integ) or tok
    req = urllib.request.Request('https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        data=json.dumps({'raw': msg}).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=25) as r:
        json.load(r)
    return True

def gmail_send(c, to, subject, body):
    integ = c.get('integrations', {}).get('gmail', {})
    if integ.get('via') == 'oauth' and (integ.get('access_token') or integ.get('refresh_token')):
        try:
            return gmail_api_send(c, to, subject, body, integ)
        except Exception as e:
            print('[GMAIL API]', e)  # fall through to SMTP if configured
    email = integ.get('email', ''); pw = integ.get('app_password', '')
    if not email: raise Exception('Gmail not connected')
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr
    prof = c.get('settings_profile') or {}
    m = MIMEText(body)
    m['From'] = formataddr((prof.get('from_name'), email)) if prof.get('from_name') else email
    m['To'] = to; m['Subject'] = subject
    if prof.get('reply_to'):
        m['Reply-To'] = prof['reply_to']
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=20) as s:
        s.login(email, pw); s.send_message(m)
    return f'Email sent to {to}: "{subject}"'

# ---------- Custom subdomains for client portals ----------
def _domain_clean(s):
    """Sanitize a domain or subdomain: lowercase, alphanumeric + hyphens + dots."""
    s = (s or '').strip().lower()
    s = re.sub(r'[^a-z0-9.-]', '', s)
    s = re.sub(r'\.{2,}', '.', s).strip('.')
    s = re.sub(r'-{2,}', '-', s).strip('-')
    return s[:253] if len(s) >= 3 else ''

def _domain_available(db, dom, exclude_email=None):
    """Check if a domain is free (not taken by another customer)."""
    for email, cust in db.get('customers', {}).items():
        if email == exclude_email:
            continue
        if (cust.get('portal_domain') or '') == dom:
            return False
    return True

def _find_customer_by_domain(dom):
    """Return (db, customer) for a given domain, or (None, None)."""
    if not dom:
        return None, None
    db = load_db()
    for email, c in db['customers'].items():
        if (c.get('portal_domain') or '') == dom:
            return db, c
    return None, None

def _provision_ssl(domain):
    """Provision a Let's Encrypt cert for a custom domain via certbot --nginx."""
    try:
        import subprocess
        # First check if cert already exists
        result = subprocess.run(['certbot', 'certificates', '--cert-name', domain],
                              capture_output=True, text=True, timeout=30)
        if 'Certificate Name' in result.stdout:
            print(f'[ssl] cert already exists for {domain}')
            return
        # Provision new cert
        result = subprocess.run([
            'certbot', '--nginx', '-d', domain,
            '--non-interactive', '--agree-tos',
            '-m', ENV.get('ADMIN_EMAIL', 'admin@anos.ca'),
            '--redirect'
        ], capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            print(f'[ssl] cert provisioned for {domain}')
            # Reload nginx to pick up new server block
            subprocess.run(['systemctl', 'reload', 'nginx'], timeout=10)
        else:
            print(f'[ssl] failed for {domain}: {result.stderr[:500]}')
    except Exception as e:
        print(f'[ssl] error provisioning {domain}: {e}')

# ---------- Password reset ----------
def _reset_token(c, email, ttl=3600):
    """Generate a password reset token for a client portal user."""
    import secrets
    token = secrets.token_urlsafe(32)
    resets = c.setdefault('password_resets', [])
    resets = [r for r in resets if r.get('expires', 0) > time.time()]  # prune expired
    resets.append({'token': token, 'email': email.lower().strip(), 'expires': time.time() + ttl, 'used': False})
    c['password_resets'] = resets
    return token

def _validate_reset_token(db, token):
    """Return (customer, email) if token is valid, else (None, None)."""
    for email, c in db.get('customers', {}).items():
        for r in (c.get('password_resets') or []):
            if r.get('token') == token and r.get('expires', 0) > time.time() and not r.get('used'):
                return c, r['email']
    return None, None

def _mark_reset_used(db, token):
    for email, c in db.get('customers', {}).items():
        for r in (c.get('password_resets') or []):
            if r.get('token') == token:
                r['used'] = True
                save_db(db)
                return True
    return False

# ---------- API key auth for settings ----------
def _api_key_auth(self, scope=None):
    """Check X-Api-Key header against stored API keys. Returns (db, customer) or (None, None).
    If scope is given, the key must list it in its 'scopes' array (keys without a
    scopes array are treated as full-access for backwards compatibility)."""
    key = self.headers.get('X-Api-Key', '').strip()
    if not key or not key.startswith('ak_'):
        return None, None
    db = load_db()
    for email, c in db['customers'].items():
        for k in (c.get('api_keys') or []):
            if k.get('key') == key and not k.get('revoked'):
                if scope:
                    scopes = k.get('scopes')
                    if scopes is not None and scope not in scopes:
                        return None, None
                return db, c
    return None, None


# ---------- API email providers (Brevo, Mailgun, SendGrid) ----------
def brevo_send(c, to, subject, body):
    """Send via Brevo (formerly Sendinblue) API. Free tier: 300 emails/day."""
    s = c.get('smtp') or {}
    api_key = s.get('api_key', '')
    if not api_key:
        return False, 'Brevo API key not configured'
    payload = {
        'sender': {'name': s.get('from_name', ''), 'email': s.get('from_email', '')},
        'to': [{'email': to}],
        'subject': subject,
        'textContent': body,
    }
    req = urllib.request.Request('https://api.brevo.com/v3/smtp/email',
        data=json.dumps(payload).encode(), method='POST',
        headers={'api-key': api_key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, None
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
            return False, d.get('message', f'Brevo HTTP {e.code}')
        except Exception:
            return False, f'Brevo HTTP {e.code}'
    except Exception as e:
        return False, str(e)

def mailgun_send(c, to, subject, body):
    """Send via Mailgun API. Free tier: 100 emails/day."""
    s = c.get('smtp') or {}
    api_key = s.get('api_key', '')
    domain = s.get('api_domain', '')
    if not api_key or not domain:
        return False, 'Mailgun API key or domain not configured'
    import base64
    auth = base64.b64encode(f'api:{api_key}'.encode()).decode()
    data = urllib.parse.urlencode({
        'from': f'{s.get("from_name", "")} <{s.get("from_email", "")}>',
        'to': to,
        'subject': subject,
        'text': body,
    }).encode()
    req = urllib.request.Request(f'https://api.mailgun.net/v3/{domain}/messages',
        data=data, method='POST',
        headers={'Authorization': 'Basic ' + auth, 'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, None
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode())
            return False, d.get('message', f'Mailgun HTTP {e.code}')
        except Exception:
            return False, f'Mailgun HTTP {e.code}'
    except Exception as e:
        return False, str(e)

def api_email_send(c, to, subject, body):
    """Send via whichever API provider is configured."""
    s = c.get('smtp') or {}
    provider = (s.get('provider') or '').lower()
    if provider == 'brevo':
        return brevo_send(c, to, subject, body)
    elif provider == 'mailgun':
        return mailgun_send(c, to, subject, body)
    return False, 'No API email provider configured'

# ---------- Client documents (contracts, files) ----------
def _find_doc_by_sign_token(token):
    db = load_db()
    for email, c in db['customers'].items():
        for d in _client_documents(c):
            if d.get('sign_token') == token:
                return db, c, d
    return None, None, None

def _public_base(c):
    dom = (c.get('settings') or {}).get('custom_domain') or (c.get('portal_domain') or '')
    if dom:
        return 'https://' + dom
    return 'https://app.anos.ca'

def _client_documents(c):
    return c.setdefault('client_documents', [])

def _save_document(c, lead, filename, raw_bytes, mime, note, doc_type):
    """Store a document linked to a lead. Returns the doc dict."""
    import uuid as _uuid
    doc_id = _uuid.uuid4().hex[:10]
    fname = f'{int(time.time())}_{doc_id}_{re.sub(r"[^a-zA-Z0-9._-]", "_", filename)}'
    d = _photo_dir(c) / 'docs'
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_bytes(raw_bytes)
    url = f'/uploads/{_photo_dir(c).name}/docs/{fname}'
    doc = {'id': doc_id, 'lead_id': lead.get('id') if lead else None,
           'name': filename, 'url': url, 'mime': mime, 'size': len(raw_bytes),
           'doc_type': doc_type, 'note': note, 'ts': time.time(),
           'client_email': (lead.get('email') or '').lower().strip() if lead else ''}
    _client_documents(c).append(doc)
    if lead:
        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'document',
            'text': f'Document added: {filename}', 'url': url, 'by': 'owner'})
    return doc

# ---------- Multi-client portals (multiple client logins per account) ----------
def _route_view_by_property_ids(c, leads, linked_ids):
    """Client route view matched by linked property ids instead of lead email."""
    run = snow._route_run(c)
    if not run.get('active'):
        return {'active': False}
    ids = set(linked_ids or [])
    mine = None
    for s in run['stops']:
        if s.get('lead_id') in ids:
            mine = s
            break
    if not mine:
        return {'active': False}
    l = next((x for x in leads if x.get('id') == mine['lead_id']), None)
    ahead = [x for x in run['stops'] if x['status'] == 'pending' and x['seq'] < mine['seq']]
    done = sum(1 for x in run['stops'] if x['status'] == 'done')
    return {
        'active': True, 'status': mine['status'],
        'stop_number': mine['seq'], 'total_stops': len(run['stops']),
        'stops_done': done, 'stops_ahead': len(ahead),
        'eta_ts': mine.get('eta_ts'),
        'plow': run.get('plow_pos'),
        'you': {'lat': (l or {}).get('lat'), 'lon': (l or {}).get('lon'), 'address': (l or {}).get('address', '')},
        'business': c.get('business', ''),
    }

def _client_portals(c):
    """List of all client portal logins on this account. Migrates legacy single client_portal."""
    portals = c.setdefault('client_portals', [])
    # migrate legacy single record into the list
    cp = c.get('client_portal') or {}
    if cp.get('enabled') and cp.get('client_email'):
        email = cp['client_email'].lower().strip()
        if not any((p.get('email') or '').lower() == email for p in portals):
            portals.append({
                'id': 'legacy',
                'name': cp.get('client_name', ''),
                'email': cp.get('client_email', ''),
                'password': cp.get('temp_password', ''),
                'token': cp.get('token', ''),
                'property_ids': cp.get('property_ids') or [],
                'enabled': True,
                'created': cp.get('created', 0),
            })
    return portals

def _find_client_by_token_multi(c, token):
    """Find a client portal login by token in the multi list."""
    for p in _client_portals(c):
        if p.get('token') and p['token'] == token:
            return p
    return None

def _client_props_for(c, portal):
    """Properties visible to a given portal login."""
    client_email = (portal.get('email') or '').lower().strip()
    linked_ids = set(portal.get('property_ids') or [])
    out = []
    for l in _crm_leads(c):
        if (l.get('email') or '').lower().strip() == client_email or l.get('id') in linked_ids:
            out.append(l)
    return out

# ---------- Multi-property client portal ----------
def _client_properties(c):
    """Return all properties (leads) visible to this client portal."""
    cp = _client_portal(c)
    if not cp.get('enabled'):
        return []
    client_email = (cp.get('client_email') or '').lower().strip()
    if not client_email:
        return []
    leads = _crm_leads(c)
    # properties = leads where email matches, OR leads explicitly linked via property_ids
    linked_ids = set(cp.get('property_ids') or [])
    props = []
    for l in leads:
        if (l.get('email') or '').lower().strip() == client_email:
            props.append(l)
        elif l.get('id') in linked_ids:
            props.append(l)
    return props

def _client_portal_summary(c):
    """Summary of client portal state for display."""
    cp = _client_portal(c)
    props = _client_properties(c)
    return {
        'enabled': cp.get('enabled', False),
        'client_name': cp.get('client_name', ''),
        'client_email': cp.get('client_email', ''),
        'property_count': len(props),
        'properties': [{'id': l.get('id'), 'address': l.get('address'), 'stage': l.get('stage'), 'amount': l.get('amount')} for l in props[:10]],
    }

# ---------- Custom SMTP (send client emails from your own domain) ----------
def smtp_conf(c):
    """Per-customer custom SMTP config stored in c['smtp']."""
    s = c.get('smtp') or {}
    if s.get('host') and s.get('user') and s.get('password') and s.get('from_email'):
        return s
    return None

def smtp_send(c, to, subject, body):
    """Send via the customer's own SMTP server. Returns (ok, err)."""
    s = smtp_conf(c)
    if not s:
        return False, 'Custom SMTP not configured'
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr
    m = MIMEText(body)
    m['From'] = formataddr((s.get('from_name') or s['from_email'], s['from_email']))
    m['To'] = to
    m['Subject'] = subject
    prof = c.get('settings_profile') or {}
    if prof.get('reply_to'):
        m['Reply-To'] = prof['reply_to']
    host = s['host']; port = int(s.get('port') or 587)
    use_ssl = str(s.get('security') or 'tls').lower() == 'ssl'
    try:
        if use_ssl or port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            srv = smtplib.SMTP(host, port, timeout=10)
            srv.ehlo(); srv.starttls(); srv.ehlo()
        with srv:
            srv.login(s['user'], s['password'])
            srv.send_message(m)
        return True, None
    except Exception as e:
        return False, str(e)[:200]

def send_client_email(c, to, subject, body):
    """Preferred send path: API provider (Brevo/Mailgun) → custom SMTP → Gmail. Returns (ok, err)."""
    s = c.get('smtp') or {}
    if s.get('provider') in ('brevo', 'mailgun'):
        ok, err = api_email_send(c, to, subject, body)
        if ok:
            return True, f'Email sent to {to} via {s["provider"].title()}'
        print('[API email fallback]', err)
    if smtp_conf(c):
        ok, err = smtp_send(c, to, subject, body)
        if ok:
            return True, f'Email sent to {to} via {c["smtp"]["host"]}'
        print('[SMTP fallback]', err)
    try:
        gmail_send(c, to, subject, body)
        return True, f'Email sent to {to} via Gmail'
    except Exception as e:
        return False, f'No working email route — API provider failed ({err if s.get("provider") else "not configured"}), SMTP failed ({err if smtp_conf(c) else "not configured"}), Gmail failed ({str(e)[:120]})'


TOOLS = [
    {'name': 'crm_query', 'desc': 'List leads/jobs from the built-in CRM (client, job, stage, amount, phone, notes)',
     'params': []},
    {'name': 'crm_add_lead', 'desc': 'Add a new lead to the CRM pipeline',
     'params': [{'name': 'client', 'required': False}, {'name': 'title', 'required': False}, {'name': 'phone', 'required': False}, {'name': 'email', 'required': False}, {'name': 'address', 'required': False}, {'name': 'amount', 'required': False}, {'name': 'notes', 'required': False}]},
    {'name': 'crm_update_stage', 'desc': 'Move a lead to a new pipeline stage (Lead, Quote Sent, In Progress, Booked/Approved, Job Completed, Lost Lead)',
     'params': [{'name': 'match', 'required': True}, {'name': 'stage', 'required': True}]},
    {'name': 'crm_update_lead', 'desc': 'Update fields on an existing lead (amount, phone, email, address, notes, title, client)',
     'params': [{'name': 'match', 'required': True}]},
    {'name': 'draft_estimate', 'desc': 'Draft an estimate for a lead from a scope of work the owner describes',
     'params': [{'name': 'match', 'required': True}, {'name': 'line_items', 'required': True}]},
    {'name': 'save_template', 'desc': 'Save or update an estimate template (reusable job template the owner designs by talking). Use when the owner says things like "make me a tree removal template" or "save this as a template".',
     'params': [{'name': 'name', 'required': True}, {'name': 'items', 'required': True}, {'name': 'notes', 'required': False}]},
    {'name': 'estimate_from_template', 'desc': 'Draft an estimate from a saved template for a lead. Use when the owner names a template + client, e.g. "tree removal template for Dave at 45 Westmount". Prices: use the owner\'s numbers when given; otherwise template defaults; 0 means "leave for review".',
     'params': [{'name': 'template', 'required': True}, {'name': 'match', 'required': True}, {'name': 'overrides', 'required': False}, {'name': 'notes', 'required': False}]},
    {'name': 'brain_remember', 'desc': "Save something to the Brain (the agent's knowledge base) so it's remembered forever — preferences, facts about the business, client details worth keeping. Use when the owner says 'remember this', 'add this to your brain', or states a durable fact (e.g. 'we charge 13% HST', 'Dave is allergic to bees at 45 Westmount').",
     'params': [{'name': 'title', 'required': True}, {'name': 'body', 'required': True}]},
    {'name': 'send_email', 'desc': 'Send an email via connected Gmail',
     'params': [{'name': 'to', 'required': True}, {'name': 'subject', 'required': True}, {'name': 'body', 'required': True}]},
]

def _crm_find(c, match):
    """Fuzzy-find a lead by client name, title, or phone."""
    m = (match or '').strip().lower()
    if not m:
        return None
    leads = _crm_leads(c)
    for l in leads:
        if m in (l.get('client') or '').lower() or m in (l.get('title') or '').lower():
            return l
    for l in leads:
        if m in (l.get('phone') or '').replace('-', '').replace(' ', ''):
            return l
    return None

def run_tool(c, name, args, db=None):
    if name == 'crm_query':
        leads = crm_leads(c)
        if not leads:
            return 'CRM is empty — no leads yet.'
        return json.dumps([{k: l.get(k) for k in ('client', 'title', 'stage', 'amount', 'phone', 'job_date')} for l in leads[:25]])
    if name == 'crm_add_lead':
        import uuid
        lead = {'id': uuid.uuid4().hex[:12], 'created': time.time(), 'stage': 'Lead',
                'activities': [{'ts': time.time(), 'type': 'created', 'text': 'Lead captured by agent', 'by': 'agent'}]}
        for f in ('client', 'title', 'phone', 'email', 'address', 'notes'):
            if args.get(f):
                lead[f] = str(args[f])[:500]
        if args.get('amount') not in (None, ''):
            try:
                lead['amount'] = float(args['amount'])
            except (TypeError, ValueError):
                pass
        if args.get('stage') in _crm_stages(c):
            lead['stage'] = args['stage']
        _crm_leads(c).append(lead)
        c.setdefault('activity', []).append({'type': 'crm', 'ts': time.time(),
            'desc': f"New lead captured: {lead.get('client') or lead.get('title')}"})
        if db: save_db(db)
        return f"Lead added to pipeline: {lead.get('client') or lead.get('title')} (stage: {lead['stage']})"
    if name == 'crm_update_stage':
        lead = _crm_find(c, args.get('match'))
        if not lead:
            return f"No lead matching '{args.get('match')}'"
        stage = args.get('stage', '')
        if stage not in _crm_stages(c):
            return f"Invalid stage '{stage}'. Valid: {', '.join(_crm_stages(c))}"
        old_stage = lead.get('stage')
        lead['stage'] = stage
        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'stage',
            'text': f"Stage changed: {old_stage or '—'} → {stage}", 'by': 'agent'})
        c.setdefault('activity', []).append({'type': 'crm', 'ts': time.time(),
            'desc': f"Moved {lead.get('client') or lead.get('title')} to {stage}"})
        if db: save_db(db)
        return f"Moved {lead.get('client') or lead.get('title')} to {stage}"
    if name == 'crm_update_lead':
        lead = _crm_find(c, args.get('match'))
        if not lead:
            return f"No lead matching '{args.get('match')}'"
        changed = []
        for f in ('client', 'title', 'phone', 'email', 'address', 'notes', 'job_date'):
            if args.get(f) not in (None, ''):
                lead[f] = str(args[f])[:500]
                changed.append(f)
        if args.get('amount') not in (None, ''):
            try:
                lead['amount'] = float(args['amount'])
                changed.append('amount')
            except (TypeError, ValueError):
                pass
        if db: save_db(db)
        return f"Updated {lead.get('client') or lead.get('title')}: {', '.join(changed) or 'nothing changed'}"
    if name == 'draft_estimate':
        lead = _crm_find(c, args.get('match'))
        if not lead:
            return f"No lead matching '{args.get('match')}'"
        items = args.get('line_items')
        if isinstance(items, str):
            # accept "desc|price; desc|price" shorthand from the model
            items = [{'desc': p.split('|')[0].strip(), 'price': p.split('|')[1].strip() if '|' in p else 0}
                     for p in items.split(';') if p.strip()]
        items = items or []
        ests = c.setdefault('estimates', [])
        est_num = c.get('est_counter', 0) + 1
        c['est_counter'] = est_num
        import uuid
        est = {'id': uuid.uuid4().hex[:10], 'number': f"EST-{est_num:03d}", 'created': time.time(),
               'status': 'draft', 'lead_id': lead['id'],
               'client': lead.get('client', ''), 'address': lead.get('address', ''),
               'line_items': [], 'notes': str(args.get('notes') or '')[:2000]}
        for item in items[:50]:
            try:
                price = float(item.get('price') or 0)
            except (TypeError, ValueError, AttributeError):
                price = 0
            est['line_items'].append({'desc': str(item.get('desc') or '')[:300], 'price': price})
        est['total'] = round(sum(i['price'] for i in est['line_items']), 2)
        ests.append(est)
        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
            'text': f"Estimate {est['number']} drafted by agent (${est['total']:,.0f})", 'by': 'agent'})
        c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
            'desc': f"Estimate {est['number']} drafted for {est['client']}"})
        if db: save_db(db)
        return f"Estimate {est['number']} drafted for {est['client']}: " + ', '.join(f"{i['desc']} ${i['price']:,.0f}" for i in est['line_items']) + f" — total ${est['total']:,.0f}. Check the Estimates page to review and mark sent."
    if name == 'brain_remember':
        t = str(args.get('title') or '').strip()
        b = str(args.get('body') or '').strip()
        if not t or not b:
            return 'brain_remember needs a title and body'
        brain.remember(c, t, b)
        if db: save_db(db)
        return f'Saved to Brain under "{t}" — I\'ll know this from now on. (Visible on the Brain page.)'
    if name == 'send_email':
        ok, msg = send_client_email(c, args.get('to',''), args.get('subject',''), args.get('body',''))
        if not ok:
            raise Exception(msg)
        return msg
    if name == 'save_template':
        tname = (args.get('name') or '').strip()[:100]
        items = args.get('items')
        if isinstance(items, str):
            items = [{'desc': p.split('|')[0].strip(), 'price': p.split('|')[1].strip() if '|' in p else 0}
                     for p in items.split(';') if p.strip()]
        items = items or []
        if not tname or not items:
            return 'Template needs a name and at least one line item.'
        norm = []
        for item in items[:40]:
            try:
                price = float(item.get('price') or 0)
            except (TypeError, ValueError, AttributeError):
                price = 0
            norm.append({'desc': str(item.get('desc') or '')[:300], 'price': price})
        tpls = c.setdefault('estimate_templates', [])
        existing = next((t for t in tpls if (t.get('name') or '').strip().lower() == tname.lower()), None)
        if existing:
            existing.update({'items': norm, 'notes': str(args.get('notes') or '')[:500]})
            action = 'updated'
        else:
            tpls.append({'id': secrets.token_hex(8), 'name': tname, 'items': norm,
                         'notes': str(args.get('notes') or '')[:500], 'created': int(time.time())})
            action = 'saved'
        c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
            'desc': f'Template "{tname}" {action} by agent ({len(norm)} items)'})
        if db: save_db(db)
        return (f'Template "{tname}" {action} with {len(norm)} line items: ' +
                ', '.join(f"{i['desc']} (${i['price']:,.0f})" for i in norm) +
                '. It\'s on the Estimates → Templates tab. Say the template name + a client to draft from it.')
    if name == 'estimate_from_template':
        tpl_name = (args.get('template') or '').strip().lower()
        tpl = next((t for t in c.get('estimate_templates', [])
                    if tpl_name and tpl_name in (t.get('name') or '').lower()), None)
        if not tpl:
            names = ', '.join(t.get('name', '?') for t in c.get('estimate_templates', [])) or 'none saved yet'
            return f"No template matching '{args.get('template')}'. Saved templates: {names}"
        lead = _crm_find(c, args.get('match'))
        if not lead:
            return f"No lead matching '{args.get('match')}' — add them first or give me their name as it appears in the CRM."
        # start from template items, apply owner overrides by keyword match on desc
        items = [{'desc': i['desc'], 'price': i.get('price', 0)} for i in tpl.get('items', [])]
        overrides = args.get('overrides')
        if isinstance(overrides, str):
            overrides = [{'desc': p.split('|')[0].strip(), 'price': p.split('|')[1].strip() if '|' in p else None}
                         for p in overrides.split(';') if p.strip()]
        for ov in (overrides or []):
            od = (ov.get('desc') or '').lower()
            tgt = next((i for i in items if od and od in i['desc'].lower()), None)
            if tgt and ov.get('price') not in (None, ''):
                try:
                    tgt['price'] = float(ov['price'])
                except (TypeError, ValueError):
                    pass
            elif not tgt and ov.get('desc'):
                try:
                    p = float(ov.get('price') or 0)
                except (TypeError, ValueError):
                    p = 0
                items.append({'desc': str(ov['desc'])[:300], 'price': p})
        ests = c.setdefault('estimates', [])
        est_num = c.get('est_counter', 0) + 1
        c['est_counter'] = est_num
        import uuid
        notes = str(args.get('notes') or tpl.get('notes') or '')[:2000]
        est = {'id': uuid.uuid4().hex[:10], 'number': f"EST-{est_num:03d}", 'created': time.time(),
               'status': 'draft', 'lead_id': lead['id'],
               'client': lead.get('client', ''), 'address': lead.get('address', ''),
               'line_items': items[:50], 'notes': notes}
        est['total'] = round(sum(i['price'] for i in est['line_items']), 2)
        ests.append(est)
        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
            'text': f"Estimate {est['number']} drafted from template \"{tpl['name']}\" (${est['total']:,.0f})", 'by': 'agent'})
        c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
            'desc': f"Estimate {est['number']} drafted for {est['client']} from template"})
        if db: save_db(db)
        zero = sum(1 for i in est['line_items'] if not i['price'])
        return (f"Estimate {est['number']} drafted for {est['client']} from \"{tpl['name']}\": " +
                ', '.join(f"{i['desc']} ${i['price']:,.0f}" for i in est['line_items']) +
                f" — total ${est['total']:,.0f}." +
                (f" {zero} line item(s) have no price — fill them on the Estimates page." if zero else ' Review on the Estimates page and mark sent when ready.'))
    return 'unknown tool'

PL_FEATURES_ORDER = ['crm_all', 'voice', 'multi_location']
PLANS_LEVEL = {'lite': 1, 'starter': 2, 'pro': 3}

GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

def gemini_chat(messages, key):
    """Call Google Gemini (v1beta generateContent). messages = [{'role','content'},...]."""
    # convert to Gemini format
    contents = []
    sys_parts = []
    for m in messages:
        if m['role'] == 'system':
            sys_parts.append(m['content'])
        else:
            contents.append({'role': 'model' if m['role'] == 'assistant' else 'user',
                             'parts': [{'text': m['content']}]})
    payload = {'contents': contents,
               'generationConfig': {'maxOutputTokens': 400, 'temperature': 0.7}}
    if sys_parts:
        payload['systemInstruction'] = {'parts': [{'text': '\n'.join(sys_parts)}]}
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={key}'
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    return d['candidates'][0]['content']['parts'][0]['text'].strip()

AGENT_RUNS = {}  # run_id -> live steps list (in-memory, per process)
OAUTH_STATES = {}  # state -> {email, ts} for in-flight Google OAuth connects

def _llm_simple(c, system, user, max_tokens=300):
    """One-shot completion via the customer's key (Gemini or OpenRouter)."""
    key = c.get('openrouter_key') or ENV.get('OPENROUTER_API_KEY', '') or MGMT_KEY
    msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
    if GEMINI_KEY:
        return gemini_chat(msgs, GEMINI_KEY)
    req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
        data=json.dumps({'model': 'deepseek/deepseek-chat-v3-0324', 'messages': msgs, 'max_tokens': max_tokens}).encode(),
        method='POST', headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    return d['choices'][0]['message']['content'].strip()


def agent_loop(sys_prompt, chat_log, c, key, db=None, steps=None, run_id=None):
    def step(icon, text, detail=None):
        s = {'icon': icon, 'text': text, 'ts': int(time.time())}
        if detail:
            s['detail'] = detail
        if steps is not None:
            steps.append(s)
        if run_id:
            AGENT_RUNS[run_id] = list(steps or [])
    step('🧠', 'Thinking about your request…')
    tool_names = '; '.join(t['name'] for t in TOOLS)
    sys_extra = (sys_prompt + "\n\nTools available: " + tool_names +
     ". To use a tool put it ALONE in one message, exactly: TOOL: <name> PARAMS: <json>. Never mix a tool call with conversational text. NEVER simulate a tool result — only the system can produce TOOL RESULT lines. If you decide to use a tool, your entire reply must be exactly the TOOL: line and nothing else. "
     "Tool details: crm_query takes {} — returns all leads. crm_add_lead takes {client,title,phone,email,address,amount,notes} — use it whenever the owner mentions a new customer/lead/job request. "
     "crm_update_stage takes {match,stage} where match is a client name or job fragment. crm_update_lead takes {match,...fields to change}. "
     "draft_estimate takes {match, line_items:[{desc,price}], notes} — when the owner describes a scope of work for a lead (e.g. 'draft an estimate for Maria: two ash removals 2000, stump grinding 600'), YOU MUST call the tool — never claim you created an estimate without calling it. After the tool result, confirm the line items + total to the owner. If the lead doesn't exist yet, call crm_add_lead first, then draft_estimate. "
     "save_template takes {name, items:[{desc,price}], notes} — when the owner designs a reusable template ('make me a pruning template with X and Y'), call it, then read back what you saved. If they describe a template vaguely, ask one short clarifying question, then save. "
     "estimate_from_template takes {template, match, overrides:[{desc,price}], notes} — when the owner names a saved template + client ('tree removal template for Dave'), call it. List saved templates first with crm_query-style knowledge if unsure of the name; if no template matches, say what's saved. "
     "TEMPLATES FLOW: owner says the job type → you save_template with sensible default line items → confirm. Later, owner says 'use the X template for Y' → estimate_from_template with any price overrides they mention. "
     "brain_remember takes {title, body} — when the owner tells you something worth remembering permanently ('remember that...', 'HST is 13%'), save it with a short title. Don't re-ask things already in your KNOWLEDGE BASE. "
     "When the owner tells you about a lead in chat, ADD it without being asked twice, then confirm with the details you saved.")
    msgs = [{'role': 'system', 'content': sys_prompt}] + [( {'role': m['role'], 'content': m['text']}) for m in chat_log[-9:]]
    # Tool instructions go as a user message so the model treats them as a direct command
    msgs.append({'role': 'user', 'content': sys_extra})
    for _ in range(4):  # tool-call rounds
        if GEMINI_KEY:
            content = gemini_chat(msgs, GEMINI_KEY)
        else:
            req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                data=json.dumps({'model': 'deepseek/deepseek-chat-v3-0324', 'messages': msgs, 'max_tokens': 400}).encode(),
                method='POST', headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
            content = d['choices'][0]['message']['content'].strip()
        m = re.search(r'TOOL:\s*(\S+)\s+PARAMS:', content)
        if m:
            name = m.group(1)
            TOOL_LABELS = {'crm_query': ('🔍', 'Checking your CRM…'),
                           'crm_add_lead': ('➕', 'Adding lead to pipeline…'),
                           'crm_update_stage': ('📊', 'Moving deal to a new stage…'),
                           'crm_update_lead': ('✏️', 'Updating lead details…'),
                           'draft_estimate': ('📝', 'Drafting estimate…'),
                           'save_template': ('📋', 'Saving template…'),
                           'estimate_from_template': ('📝', 'Drafting from template…'),
                           'brain_remember': ('🧠', 'Saving to Brain…'),
                           'send_email': ('📧', 'Sending email…')}
            ic, lb = TOOL_LABELS.get(name, ('⚙️', f'Running {name}…'))
            step(ic, lb, detail={'tool': name})
            # extract first {...} block from rest of message
            rest = content[m.end():]
            brace = rest.find('{'); depth = 0; end = -1
            for i in range(brace, len(rest)):
                if rest[i] == '{': depth += 1
                elif rest[i] == '}':
                    depth -= 1
                    if depth == 0: end = i; break
            params_raw = rest[brace:end+1] if end != -1 else rest.strip()
            try:
                args = json.loads(params_raw or '{}')
                result = run_tool(c, name, args, db)
                step('✓', str(result)[:140], detail={'tool': name, 'params': args, 'result': str(result)[:300]})
                msgs.append({'role': 'assistant', 'content': content})
                msgs.append({'role': 'user', 'content': f'TOOL RESULT for {name}: {result}'})
                continue
            except Exception as e:
                step('⚠️', f'{name} hit an error: {e}')
                msgs.append({'role': 'assistant', 'content': content})
                msgs.append({'role': 'user', 'content': f'TOOL ERROR: {e}'})
                continue
        # Hallucination guard: if the reply claims an estimate was created but no draft_estimate ran,
        # try to auto-extract lead + items and call the tool ourselves.
        if 'draft_estimate' not in [s.get('detail', {}).get('tool') for s in (steps or [])]:
            est_match = re.search(r'EST-(\d+)', content)
            if est_match and ('estimate' in content.lower() or 'draft' in content.lower()):
                # try to pull lead name + line items from the reply text
                lead_guess = re.search(r'for\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)', content)
                items = re.findall(r'([^\n:£$]+?)\s*\|\s*\$?([\d,]+(?:\.\d{2})?)', content)
                if not items:
                    items = re.findall(r'([^\n:£$]+?):\s*\$?([\d,]+(?:\.\d{2})?)', content)
                if items and lead_guess:
                    lead = _crm_find(c, lead_guess.group(1))
                    if lead:
                        try:
                            args = {'match': lead_guess.group(1), 'line_items': [{'desc': d.strip(), 'price': p.replace(',', '')} for d, p in items], 'notes': 'Auto-captured from agent reply'}
                            result = run_tool(c, 'draft_estimate', args, db)
                            step('🛡️', 'Auto-captured estimate from reply', detail={'tool': 'draft_estimate', 'auto': True, 'params': args, 'result': str(result)[:300]})
                            content = content + f"\n\n*(I actually created that estimate for you — {result})*"
                        except Exception as e:
                            step('⚠️', f'Auto-capture failed: {e}')
        return content
    return 'I hit a snag running that — try narrowing the request.'

def tts_edge(text, voice):
    """Edge TTS (dev-file trick, no key, free). voice = Edge voice name like en-GB-SoniaNeural"""
    import subprocess, tempfile
    if not voice.startswith('en-'):
        voice = 'en-GB-SoniaNeural'
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        tmp = f.name
    r = subprocess.run(['edge-tts', '--voice', voice, '--text', text, '--write-media', tmp],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-200:] if r.stderr else 'edge-tts failed')
    return tmp

VOICES = {
    'sonia':    {'label': 'Sonia (UK female)',     'edge': 'en-GB-SoniaNeural'},
    'thomas':   {'label': 'Thomas (UK male)',      'edge': 'en-GB-ThomasNeural'},
    'aria':     {'label': 'Aria (US female)',      'edge': 'en-US-AriaNeural'},
    'guy':      {'label': 'Guy (US male)',         'edge': 'en-US-GuyNeural'},
    'andrew':   {'label': 'Andrew (US male, deep)','edge': 'en-US-AndrewNeural'},
    'emma':     {'label': 'Emma (US female)',      'edge': 'en-US-EmmaNeural'},
}

# ---------- ElevenLabs (customer's own key → custom/premium voices) ----------
def el_key(c):
    return ((c.get('integrations') or {}).get('elevenlabs') or {}).get('api_key', '')

def el_list_voices(c):
    """Fetch the customer's ElevenLabs voice library. Returns list of {id, label} or raises."""
    req = urllib.request.Request('https://api.elevenlabs.io/v1/voices',
        headers={'xi-api-key': el_key(c)})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.load(r)
    out = []
    for v in d.get('voices', []):
        cat = v.get('category', '')
        tag = {'premade': 'EL', 'cloned': 'clone', 'generated': 'clone', 'professional': 'pro'}.get(cat, cat or 'EL')
        out.append({'id': 'el:' + v['voice_id'], 'label': f"{v['name']} ({tag})"})
    return out

def tts_elevenlabs(text, voice_id, api_key):
    """Call ElevenLabs TTS, return path to mp3."""
    import tempfile
    payload = {'text': text, 'model_id': 'eleven_turbo_v2_5',
               'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}}
    req = urllib.request.Request(f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
        data=json.dumps(payload).encode(), method='POST',
        headers={'xi-api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
    with urllib.request.urlopen(req, timeout=60) as r:
        audio = r.read()
    with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as f:
        f.write(audio)
        return f.name
# ---------- Telegram channel bridge (per-customer bot, polling loop) ----------
CHANNEL_PROCS = {}  # email -> subprocess.Popen

def telegram_bridge_path(email):
    return BASE / (re.sub(r'[^a-z0-9@._-]', '', email) + '.telegram.json')

def channel_start(email, token):
    stop_channel(email)
    state = {'token': token, 'offset': 0, 'email': email}
    (telegram_bridge_path(email)).write_text(json.dumps(state))
    log = open(BASE / (re.sub(r'[^a-z0-9@._-]', '', email) + '.telegram.log'), 'ab')
    p = subprocess.Popen(
        ['python3', str(BASE / 'telegram_bridge.py'), email],
        cwd=str(BASE), stdout=log, stderr=log)
    CHANNEL_PROCS[email] = p
    return p.pid

def channel_stop(email):
    p = CHANNEL_PROCS.pop(email, None)
    if p and p.poll() is None:
        p.terminate()
    for f in [telegram_bridge_path(email)]:
        try: f.unlink()
        except: pass

def channel_status(email):
    p = CHANNEL_PROCS.get(email)
    return 'running' if p and p.poll() is None else 'stopped'

CHANNEL_CATALOG = [
    {'service': 'telegram', 'name': 'Telegram',  'desc': 'Chat with your agent from Telegram (text + voice).', 'fields': ['bot_token', 'chat_id'], 'kind': 'channel'},
    {'service': 'whatsapp', 'name': 'WhatsApp',  'desc': 'Coming in Phase 2 — Cloud API setup.', 'fields': ['phone_number_id', 'access_token'], 'kind': 'channel'},
    {'service': 'signal',   'name': 'Signal',    'desc': 'Advanced — via linked phone (later).', 'fields': ['phone_number'], 'kind': 'channel'},
]

INTEGRATIONS_CATALOG = [
    {'service': 'gmail',     'name': 'Google Gmail',      'desc': 'Email follow-ups, quote sending, client replies.', 'fields': ['email', 'app_password'], 'kind': 'email', 'oauth': 'google'},
    {'service': 'outlook',   'name': 'Microsoft Outlook', 'desc': 'Email follow-ups, quote sending, client replies.', 'fields': ['email', 'password'],     'kind': 'email'},
    {'service': 'notion',    'name': 'Notion',            'desc': 'CRM pipeline, quotes, jobs — auto-logged.',          'fields': ['api_key', 'database'],   'kind': 'crm'},
    {'service': 'googlecal', 'name': 'Google Calendar',   'desc': 'Appointment booking & scheduling.',                  'fields': ['api_key'],               'kind': 'calendar', 'oauth': 'google'},
]

SESSIONS = {}  # token -> email (owner) OR {'email': owner_email, 'member': member_dict} (team)
SESSIONS_FILE = BASE / 'sessions.json'
PENDING_LOGINS = {}  # pick_token -> {'ts': epoch, 'matches': [...]} for multi-account login picker

def _account_matches(db, email, pw):
    """Every identity this email+password unlocks: owner account, team memberships, client portals."""
    matches = []
    c = db['customers'].get(email)
    if c and c.get('password') == pw:
        matches.append({'kind': 'owner', 'owner': email,
                        'business': c.get('business', '') or c.get('name', '') or email,
                        'name': c.get('name', ''), 'role': 'owner'})
    for owner_email, cust in db['customers'].items():
        m = next((x for x in cust.get('team_members', [])
                  if x['email'] == email and x.get('password') == pw), None)
        if m:
            matches.append({'kind': 'team', 'owner': owner_email, 'member': m,
                            'business': cust.get('business', '') or owner_email,
                            'name': m.get('name', ''), 'role': m.get('role', '')})
    return matches

def _issue_session(match):
    token = secrets.token_hex(16)
    if match['kind'] == 'owner':
        SESSIONS[token] = match['owner']
    else:
        SESSIONS[token] = {'email': match['owner'], 'member': match['member']}
    _sessions_save()
    return token

def _sessions_load():
    """Restore sessions from disk so restarts don't log everyone out."""
    global SESSIONS
    try:
        if SESSIONS_FILE.exists():
            SESSIONS = json.loads(SESSIONS_FILE.read_text())
    except Exception as e:
        print('[sessions] load failed:', e)
        SESSIONS = {}

_sessions_save_timer = [None]
def _sessions_save():
    """Debounced persist (sessions change on every login)."""
    def _write():
        _sessions_save_timer[0] = None
        try:
            SESSIONS_FILE.write_text(json.dumps(SESSIONS))
        except Exception as e:
            print('[sessions] save failed:', e)
    t = _sessions_save_timer[0]
    if t:
        t.cancel()
    t = threading.Timer(2.0, _write)
    t.daemon = True
    t.start()
    _sessions_save_timer[0] = t

_sessions_load()

TEAM_ROLES = {
    'admin':     {'label': 'Admin',     'can_edit': True,
                  'desc': 'Full access — can edit leads, send estimates, change settings. Cannot manage team or billing.'},
    'moderator': {'label': 'Moderator', 'can_edit': False,
                  'desc': 'View-only — can see dashboard, leads, routes & inbox, but cannot change anything.'},
}

def _session_user(tok):
    """Normalize a session value to (owner_email, member_dict_or_None)."""
    s = SESSIONS.get(tok)
    if not s:
        return None, None
    if isinstance(s, dict):
        return s.get('email'), s.get('member')
    return s, None

# ---------- Referral program ----------
def _referral(c):
    """Per-customer referral account: code, payout coords, attributed signups, commissions."""
    return c.setdefault('referral', {
        'code': '', 'payout_email': '', 'payout_method': 'etransfer',
        'signups': [],      # [{email, name, business, ts, status: free|trialing|active|churned}]
        'commissions': [],  # [{id, ts, period, amount, status: pending|approved|paid, note, paid_ts}]
    })

def _ref_code(c):
    """Lazy-generate a short, URL-safe referral code from the business name."""
    r = _referral(c)
    if not r.get('code'):
        base = re.sub(r'[^a-z0-9]', '', (c.get('business') or c.get('name') or 'ref').lower())[:8] or 'ref'
        r['code'] = base + secrets.token_hex(2)
    return r['code']

def _ref_totals(c):
    r = _referral(c)
    earned = sum(x.get('amount') or 0 for x in r['commissions'])
    paid = sum(x.get('amount') or 0 for x in r['commissions'] if x.get('status') == 'paid')
    pending = sum(x.get('amount') or 0 for x in r['commissions'] if x.get('status') == 'pending')
    return {'earned': round(earned, 2), 'paid': round(paid, 2),
            'pending': round(pending, 2), 'balance': round(earned - paid, 2)}

def _ref_find_by_code(db, code):
    for email, c in db.get('customers', {}).items():
        if c.get('referral', {}).get('code') and c['referral']['code'] == code:
            return c
    return None

def _ref_attach_signup(db, new_cust, ref_code):
    """Attribute a fresh signup to a referrer. Returns the referrer or None."""
    if not ref_code:
        return None
    ref = _ref_find_by_code(db, str(ref_code).strip())
    if not ref or ref.get('email') == new_cust.get('email'):
        return None
    new_cust['referred_by'] = ref['referral']['code']
    _referral(ref)['signups'].append({
        'email': new_cust.get('email'), 'name': new_cust.get('name', ''),
        'business': new_cust.get('business', ''), 'ts': int(time.time()), 'status': 'free'})
    return ref

def _ref_status_sync(db):
    """Refresh referral signup statuses from live billing_status. Callers save."""
    for c in db.get('customers', {}).values():
        r = c.get('referral') or {}
        for s in (r.get('signups') or []):
            tgt = db['customers'].get((s.get('email') or '').lower())
            if tgt:
                s['status'] = tgt.get('billing_status', 'free')

def log_activity(db, email, kind, text, save=True):
    """Append to the global admin activity feed (capped at 300 entries)."""
    db.setdefault('activity', []).append(
        {'ts': int(time.time()), 'kind': kind, 'email': email, 'text': text})
    db['activity'] = db['activity'][-300:]
    if save:
        save_db(db)

def cust_health(c):
    """Per-customer health badge for the admin panel: green/yellow/red + reasons."""
    now = time.time()
    reasons = []
    if not c.get('openrouter_key'):
        reasons.append('no AI key provisioned')
    if c.get('provision_error'):
        reasons.append('provisioning error')
    last_chat = max((m.get('ts', 0) for m in c.get('chat_log', [])), default=0)
    last_seen = max(c.get('last_seen', 0), last_chat, c.get('created', 0))
    days_idle = (now - last_seen) / 86400 if last_seen else 99
    if c.get('billing_status') in ('past_due', 'unpaid', 'paused'):
        reasons.append('billing ' + c['billing_status'])
    if c.get('billing_status') == 'paused':
        return 'red', reasons
    if days_idle > 7:
        reasons.append(f'quiet {int(days_idle)}d')
    if not c.get('openrouter_key') or reasons:
        return ('red' if (not c.get('openrouter_key') or 'past_due' in str(reasons)) else 'yellow'), reasons
    return 'green', reasons
TILE_CACHE = {}  # (z,x,y) -> png bytes

class H(BaseHTTPRequestHandler):
    def _html_redirect(self, url):
        self.send_response(302)
        self.send_header('Location', url)
        self.end_headers()

    def _customer_or_api(self, scope=None):
        """Auth via session Bearer token OR X-Api-Key header (optional key scope)."""
        db, c, member = self._customer()
        if c:
            return db, c, member
        db2, c2 = _api_key_auth(self, scope=scope)
        if c2:
            return db2, c2, None
        return None, None, None

    def _json(self, code, obj):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _body(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self._raw_body = raw
        ct = self.headers.get('Content-Type', '')
        if 'application/x-www-form-urlencoded' in ct:
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode('utf-8', 'replace')).items()}
        if not raw:
            return {}
        return json.loads(raw)

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(getattr(self, '_raw_path', self.path)).query)

    def _customer(self):
        """Returns (db, customer, member) — member is None for the account owner,
        or the team-member dict (with 'role') when logged in via team invite."""
        auth = self.headers.get('Authorization', '').replace('Bearer ', '')
        email, member = _session_user(auth)
        if not email or email == '__meta__':
            return None, None, None
        db = load_db()
        c = db['customers'].get(email)
        if c and member:
            # re-resolve member from the live record (role may have changed)
            live = next((m for m in c.get('team_members', []) if m.get('email') == member.get('email')), None)
            if not live:
                return None, None, None  # removed from team
            member = live
        return db, c, member

    def do_OPTIONS(self):
        self._json(204, {})

    def do_GET(self):
        self._raw_path = self.path
        if '?' in self.path:
            self.path = self.path.split('?', 1)[0]
        if self.path.startswith('/uploads/'):
            rel = urllib.parse.unquote(self.path[len('/uploads/'):]).split('?')[0]
            if '/' in rel.replace('\\', '/').strip('/').replace('/', '') or '..' in rel:
                return self._json(400, {'error': 'bad path'})
            parts = rel.split('/')
            if len(parts) == 2:
                cust_dir, fname = parts
                sub = ''
            elif len(parts) == 3 and parts[1] in ('docs', 'leaddocs', 'social'):
                cust_dir, sub, fname = parts[0], parts[1], parts[2]
            else:
                return self._json(404, {'error': 'not found'})
            if '..' in fname or '/' in fname:
                return self._json(400, {'error': 'bad path'})
            # auth: owner session OR client token of the customer whose folder this is
            db = load_db()
            owner_email = None
            for email, c in db['customers'].items():
                if re.sub(r'[^a-z0-9@._-]', '', email) == cust_dir:
                    owner_email = email
                    break
            tok = _get_bearer(self) or self._qs().get('tok', [''])[0]
            _su, _ = _session_user(tok)
            ok = _su == owner_email
            if not ok and owner_email:
                cust = db['customers'][owner_email]
                cp = (cust.get('client_portal') or {})
                ok = bool(cp.get('token')) and cp['token'] == tok
                # multi-portal tokens
                if not ok:
                    ok = any(p.get('token') == tok for p in _client_portals(cust))
            # sign-link access: a valid doc sign_token authorizes THAT doc only
            if not ok and sub == 'docs' and owner_email and False:
                stok = self._qs().get('sigtok', [''])[0]
                if stok:
                    cust = db['customers'][owner_email]
                    for d in _client_documents(cust):
                        if d.get('sign_token') == stok and str(d.get('url', '')).endswith('/' + fname):
                            ok = True
                            break
            if not ok:
                return self._json(401, {'error': 'unauthorized'})
            f = BASE / 'uploads' / cust_dir / (sub + '/' if sub else '') / fname
            if not f.exists():
                return self._json(404, {'error': 'not found'})
            ext = f.suffix.lower().lstrip('.')
            ctype = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                     'webp': 'image/webp', 'gif': 'image/gif', 'pdf': 'application/pdf'}.get(ext, 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            self.wfile.write(f.read_bytes())
            return
        if self.path == '/mock-lead.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(open(BASE / 'mock-lead.html', 'rb').read())
            return
        if self.path == '/' or self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write((BASE / 'index.html').read_bytes())
            return
        if self.path == '/setup' or self.path == '/setup.html':
            # public setup-fee quote form (no login)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write((BASE / 'setup.html').read_bytes())
            return
        if self.path.startswith('/logos/'):
            name = self.path[len('/logos/'):]
            if '/' in name or '..' in name:
                return self._json(400, {'error': 'bad path'})
            f = BASE / 'logos' / name
            if not f.exists():
                return self._json(404, {'error': 'not found'})
            self.send_response(200)
            self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(f.read_bytes())
            return
        if self.path.startswith('/tiles/'):
            # proxy OSM tiles so the map never depends on a third-party CDN's referer/IP rules.
            # cache in-process; be polite upstream (single UA, no hammering).
            parts = self.path[len('/tiles/'):].rstrip('.png').split('/')
            try:
                z, x, y = int(parts[0]), int(parts[1]), int(parts[2])
                if not (0 <= z <= 19): raise ValueError
            except (ValueError, IndexError):
                return self._json(400, {'error': 'bad tile'})
            key = (z, x, y)
            tile = TILE_CACHE.get(key)
            if tile is None:
                url = f'https://tile.openstreetmap.org/{z}/{x}/{y}.png'
                req = urllib.request.Request(url, headers={'User-Agent': 'anosagent-portal/1.0 (app.anos.ca)'})
                try:
                    with urllib.request.urlopen(req, timeout=15) as r:
                        tile = r.read()
                    if len(TILE_CACHE) > 4000:
                        TILE_CACHE.clear()
                    TILE_CACHE[key] = tile
                except Exception:
                    return self._json(502, {'error': 'tile fetch failed'})
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'max-age=604800')
            self.end_headers()
            self.wfile.write(tile)
            return

        if self.path == '/manifest.webmanifest':
            self.send_response(200)
            self.send_header('Content-Type', 'application/manifest+json')
            self.send_header('Cache-Control', 'max-age=3600')
            self.end_headers()
            self.wfile.write((BASE / 'manifest.webmanifest').read_bytes())
            return
        if self.path == '/sw.js':
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write((BASE / 'sw.js').read_bytes())
            return
        if self.path in ('/icon-192.png', '/icon-512.png', '/icon-maskable-512.png', '/apple-touch-icon.png', '/agent-avatar-512.png', '/agent-avatar-256.png'):
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write((BASE / self.path.lstrip('/')).read_bytes())
            return
        if self.path in ('/anos-logo-white.png', '/anos-lockup.png', '/anos-emblem.png', '/anos-text.png'):
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write((BASE / self.path.lstrip('/')).read_bytes())
            return
        if self.path == '/admin':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write((BASE / 'admin.html').read_bytes())
            return
        if self.path == '/api/plans':
            self._json(200, {'plans': PLANS})
            return
        if self.path == '/driver' or self.path.startswith('/driver?'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(snow.DRIVER_PAGE_HTML.encode())
            return

        if self.path.startswith('/api/routes/driver-run'):
            qs = urllib.parse.urlparse(getattr(self, '_raw_path', self.path)).query
            tok = urllib.parse.parse_qs(qs).get('token', [''])[0]
            db = load_db()
            c = snow.driver_auth(db, tok)
            if not c:
                return self._json(401, {'error': 'bad driver token'})
            return self._json(200, snow.run_status(c, crm_leads(c)))

        if self.path.startswith('/subscribe/'):
            token = self.path.split('/subscribe/', 1)[1].strip('/')
            if not token or '..' in token:
                return self._json(404, {'error': 'not found'})
            db = load_db()
            c, plan = connect.public_plan(db, token)
            if not plan:
                return self._json(404, {'error': 'plan not found'})
            done = self._qs().get('done', [''])[0] == '1'
            html = connect.signup_page(c, plan, PORTAL_BASE, done=done)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(html.encode())
            return
        if self.path == '/api/brain/health':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, brain.health(c))

        if self.path == '/api/brain/templates':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, {'templates': brain.NOTE_TEMPLATES})

        if self.path == '/api/brain':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            brain.seed(c)
            save_db(db)
            return self._json(200, {'notes': brain.list_notes(c)})
        if self.path == '/api/connect/status':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            st = connect.acct_status(stripe_request, c) if STRIPE_KEY else {'connected': False}
            save_db(db)
            return self._json(200, st)
        if self.path == '/api/subs':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            out = connect.summary(c)
            out['connect'] = connect.acct_status(stripe_request, c) if STRIPE_KEY else {'connected': False}
            return self._json(200, out)
        if self.path == '/client':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write((BASE / 'client.html').read_bytes())
            return
        if self.path == '/api/client/status':
            tok = _get_bearer(self)
            db, c = _find_customer_by_client_token(tok)
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            # resolve which portal login this token belongs to (multi-portal aware)
            p_login = None
            for p in _client_portals(c):
                if p.get('token') and p['token'] == tok:
                    p_login = p
                    break
            status = get_client_status(c, portal_override=p_login)
            return self._json(200, status)
        if self.path == '/api/admin/customers':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            out = []
            for email, c in db['customers'].items():
                usage = get_key_usage(c['openrouter_key']) if c.get('openrouter_key') else {}
                out.append({'name': c['name'], 'email': email, 'business': c.get('business', ''),
                            'plan': c['plan'], 'plan_name': PLANS[c['plan']]['name'],
                            'monthly': PLANS[c['plan']]['monthly'],
                            'usage_today': usage.get('usage_daily', 0),
                            'usage_month': usage.get('usage_monthly', 0),
                            'billing_status': c.get('billing_status', ''),
                            'created': c.get('created', 0),
                            'admin_notes': c.get('admin_notes', ''),
                            'health': cust_health(c)[0],
                            'health_reasons': cust_health(c)[1],
                            'provisioned': bool(c.get('openrouter_key'))})
            return self._json(200, {'customers': out})
        if self.path == '/api/admin/referrals':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            _ref_status_sync(db)
            save_db(db)
            out = []
            for email, c in db['customers'].items():
                r = c.get('referral') or {}
                if not (r.get('code') or r.get('signups') or r.get('commissions')):
                    continue
                out.append({'owner': email, 'name': c.get('name', ''), 'business': c.get('business', ''),
                            'code': r.get('code', ''), 'payout_email': r.get('payout_email', ''),
                            'payout_method': r.get('payout_method', 'etransfer'),
                            'signups': sorted(r.get('signups') or [], key=lambda s: s.get('ts', 0), reverse=True),
                            'commissions': sorted(r.get('commissions') or [], key=lambda x: x.get('ts', 0), reverse=True),
                            'totals': _ref_totals(c)})
            out.sort(key=lambda x: x['totals']['balance'], reverse=True)
            return self._json(200, {'ok': True, 'referrers': out})

        if self.path == '/api/admin/summary':
            # Dashboard stats: counts, MRR, AI spend, support tickets
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            custs = list(db['customers'].values())
            mrr = sum(PLANS[c['plan']]['monthly'] for c in custs)
            spend = 0
            near = 0
            for c in custs:
                if c.get('openrouter_key'):
                    u = get_key_usage(c['openrouter_key'])
                    spend += u.get('usage_daily', 0)
                    pct = u.get('usage_daily', 0) / max(PLANS[c['plan']]['daily_limit'], 0.001)
                    if pct >= 0.75:
                        near += 1
                        today = time.strftime('%Y-%m-%d')
                        if pct >= 0.999 and c.get('cap_logged') != today:
                            c['cap_logged'] = today
                            log_activity(db, c['email'], 'cap',
                                         f"⚡ {c.get('name', c['email'])} hit their daily AI cap", save=False)
            open_tickets = sum(1 for c in custs for t in c.get('support_tickets', []) if t.get('status') == 'open')
            return self._json(200, {'customers': len(custs),
                                    'mrr': mrr,
                                    'ai_spend_today': round(spend, 2),
                                    'near_cap': near,
                                    'open_tickets': open_tickets,
                                    'live_keys': sum(1 for c in custs if c.get('openrouter_key'))})
        if self.path == '/api/admin/insights':
            # Signup/MRR trend (6 months) + credit-burn leaderboard + health badges
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            custs = list(db['customers'].values())
            # --- 6-month trend: signups + cumulative MRR by month ---
            months = []
            now = time.localtime()
            y, m = now.tm_year, now.tm_mon
            for i in range(5, -1, -1):
                mm = m - i
                yy = y + (mm - 1) // 12
                mm = (mm - 1) % 12 + 1
                start = time.mktime((yy, mm, 1, 0, 0, 0, 0, 0, 0))
                end = time.mktime((yy + (mm == 12), mm % 12 + 1, 1, 0, 0, 0, 0, 0, 0))
                label = time.strftime('%b', time.localtime(start))
                signups = sum(1 for c in custs if start <= c.get('created', 0) < end)
                mrr_m = sum(PLANS[c['plan']]['monthly'] for c in custs if c.get('created', 0) < end)
                months.append({'label': label, 'signups': signups, 'mrr': mrr_m})
            # --- credit-burn leaderboard (this month) ---
            board = []
            for c in custs:
                if not c.get('openrouter_key'):
                    continue
                u = get_key_usage(c['openrouter_key'])
                board.append({'email': c['email'], 'name': c.get('name', ''), 'business': c.get('business', ''),
                              'plan': c['plan'], 'month': round(u.get('usage_monthly', 0), 2),
                              'today': round(u.get('usage_daily', 0), 2),
                              'monthly_rev': PLANS[c['plan']]['monthly']})
            board.sort(key=lambda x: x['month'], reverse=True)
            # --- health badges ---
            health = {}
            for c in custs:
                h, reasons = cust_health(c)
                health[c['email']] = {'level': h, 'reasons': reasons}
            counts = {'green': 0, 'yellow': 0, 'red': 0}
            for v in health.values():
                counts[v['level']] += 1
            return self._json(200, {'months': months, 'leaderboard': board[:10],
                                    'health': health, 'health_counts': counts,
                                    'note': 'MRR uses plan list prices until Stripe is live.'})
        if self.path == '/api/admin/activity':
            # Global activity feed, newest first
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            feed = sorted(db.get('activity', []), key=lambda x: x.get('ts', 0), reverse=True)
            return self._json(200, {'activity': feed[:80]})
        if self.path == '/api/admin/revenue':
            # Real Stripe revenue when a live key is configured; otherwise list-price MRR
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            if not STRIPE_KEY:
                db = load_db()
                mrr = sum(PLANS[c['plan']]['monthly'] for c in db['customers'].values())
                return self._json(200, {'mode': 'list_price', 'mrr': mrr,
                                        'note': 'Add a live Stripe secret key to see real collected revenue.'})
            try:
                subs = []
                url = 'https://api.stripe.com/v1/subscriptions?status=active&limit=100'
                while url:
                    req = urllib.request.Request(url, headers={
                        'Authorization': 'Bearer ' + STRIPE_KEY})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        d = json.load(r)
                    subs.extend(d.get('data', []))
                    url = ('https://api.stripe.com/v1/subscriptions?status=active&limit=100&starting_after='
                           + subs[-1]['id']) if d.get('has_more') and subs else None
                mrr = 0
                renewals = []
                for s in subs:
                    for it in s.get('items', {}).get('data', []):
                        p = it.get('price', {})
                        amt = (p.get('unit_amount') or 0) / 100
                        if p.get('recurring', {}).get('interval') == 'year':
                            amt /= 12
                        mrr += amt
                    renewals.append({'customer': s.get('customer'),
                                     'renews': s.get('current_period_end'),
                                     'status': s.get('status')})
                renewals.sort(key=lambda x: x.get('renews') or 0)
                # failed payments
                failed = 0
                req = urllib.request.Request(
                    'https://api.stripe.com/v1/charges?limit=100',
                    headers={'Authorization': 'Bearer ' + STRIPE_KEY})
                with urllib.request.urlopen(req, timeout=20) as r:
                    ch = json.load(r)
                failed = sum(1 for c in ch.get('data', []) if not c.get('paid'))
                return self._json(200, {'mode': 'stripe', 'mrr': round(mrr, 2),
                                        'active_subs': len(subs), 'failed_charges': failed,
                                        'upcoming': renewals[:10]})
            except Exception as e:
                return self._json(200, {'mode': 'error', 'error': str(e)})
        if self.path == '/api/admin/support':
            # Support ticket inbox across all customers
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            db = load_db()
            tickets = []
            for email, c in db['customers'].items():
                for t in c.get('support_tickets', []):
                    tickets.append({**t, 'customer': email, 'business': c.get('business', '')})
            tickets.sort(key=lambda x: x.get('ts', 0), reverse=True)
            return self._json(200, {'tickets': tickets[:100]})
        if self.path == '/api/admin/impersonate':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            email = self.headers.get('X-Email', '').lower().strip()
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            token = secrets.token_hex(16)
            SESSIONS[token] = email
            _sessions_save()
            return self._json(200, {'token': token, 'email': email, 'name': c.get('name', '')})
        if self.path == '/api/admin/integrations':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            email = self.headers.get('X-Email', '').lower().strip()
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            integ = c.get('integrations', {})
            masked = {}
            for s, d in integ.items():
                m = {k: v for k, v in d.items() if k != 'api_key' and 'password' not in k and k != 'bot_token'
                     and k not in ('access_token', 'refresh_token', 'expires', 'webhook_keys')}
                m['connected_at'] = d.get('connected_at')
                m['kind'] = d.get('kind')
                masked[s] = m
            return self._json(200, {'integrations': masked, 'catalog': INTEGRATIONS_CATALOG + CHANNEL_CATALOG})
        if self.path == '/api/broadcasts':
            # Customer-facing: unread admin broadcasts for the notification banner
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            dismissed = set(c.get('broadcasts_dismissed', []))
            items = [n for n in c.get('notifications', [])
                     if n.get('kind') == 'broadcast' and n.get('ts') not in dismissed]
            return self._json(200, {'broadcasts': items[:5]})

        if self.path == '/api/team':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            members = [{'email': m['email'], 'name': m.get('name', ''), 'role': m['role'],
                        'role_label': TEAM_ROLES.get(m['role'], {}).get('label', m['role']),
                        'created': m.get('created'), 'last_login': m.get('last_login')}
                       for m in c.get('team_members', [])]
            invites = [] if member else [{'email': i['email'], 'role': i['role'], 'code': i['code'],
                                          'created': i.get('created')}
                                         for i in c.get('team_invites', [])]
            return self._json(200, {
                'owner': {'email': c['email'], 'name': c.get('name', ''), 'role': 'owner'},
                'members': members, 'invites': invites,
                'roles': {k: {'label': v['label'], 'desc': v['desc']} for k, v in TEAM_ROLES.items()},
                'my_role': 'owner' if not member else member['role']})

        if self.path == '/api/integrations/catalog':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, {'catalog': INTEGRATIONS_CATALOG + CHANNEL_CATALOG})

        if self.path == '/api/integrations':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            integ = c.get('integrations', {})
            # mask secrets in display; channels get live status
            masked = {}
            for s, d in integ.items():
                m = {k: v for k, v in d.items() if k != 'api_key' and 'password' not in k and k != 'bot_token'
                     and k not in ('access_token', 'refresh_token', 'expires', 'webhook_keys')}
                m['connected_at'] = d.get('connected_at')
                m['kind'] = d.get('kind')
                if d.get('kind') == 'channel':
                    m['status'] = channel_status(c['email'])
                masked[s] = m
            return self._json(200, {'integrations': masked})

        if self.path == '/api/auth/google-config':
            return self._json(200, {'client_id': ENV.get('GOOGLE_OAUTH_CLIENT_ID', '')})

        # Safari fallback: GIS button does a full-page GET redirect with ?credential=...
        # when popup mode is blocked (ITP). Handle it like the POST, then bounce to /.
        if self.path.startswith('/api/auth/google') and 'credential=' in getattr(self, '_raw_path', ''):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self._raw_path).query)
            id_token = (qs.get('credential', [''])[0]).strip()
            gclient_id = ENV.get('GOOGLE_OAUTH_CLIENT_ID', '')
            if id_token and gclient_id:
                try:
                    req = urllib.request.Request(
                        'https://oauth2.googleapis.com/tokeninfo?id_token=' + urllib.parse.quote(id_token))
                    with urllib.request.urlopen(req, timeout=20) as r:
                        info = json.load(r)
                    if info.get('aud') == gclient_id:
                        email = (info.get('email') or '').lower().strip()
                        if email and info.get('email_verified') in ('true', True):
                            db = load_db()
                            c = db['customers'].get(email)
                            if not c:
                                name = (info.get('name') or email.split('@')[0]).strip()[:100]
                                c = {'name': name, 'business': '', 'email': email, 'password': '',
                                     'google_sub': info.get('sub', ''), 'plan': 'lite', 'created': int(time.time()),
                                     'billing_status': 'free', 'card_last4': '—', 'company_type': 'tree'}
                                if MGMT_KEY:
                                    try:
                                        k = provision_key(f"customer:{email}", PLANS['lite']['daily_limit'])
                                        c['openrouter_key'] = k.get('key')
                                        c['openrouter_key_hash'] = k.get('data', {}).get('hash') or k.get('hash')
                                    except Exception as e:
                                        c['provision_error'] = str(e)
                                db['customers'][email] = c
                                log_activity(db, email, 'signup',
                                             f"New Google signup: {c['name']} ({email})", save=False)
                                save_db(db)
                            elif not c.get('google_sub'):
                                c['google_sub'] = info.get('sub', '')
                                save_db(db)
                            token = secrets.token_hex(16)
                            SESSIONS[token] = email
                            _sessions_save()
                            # hand the token to the frontend via URL hash (never logged server-side)
                            return self._html_redirect('/#gtoken=' + token)
                except Exception as e:
                    print('[OAUTH safari-get]', e)
            return self._html_redirect('/?login_error=google')

        if self.path == '/api/tts/voices':
            db, c, member = self._customer()
            voices = [{'id': k, 'label': v['label'], 'edge': v['edge'], 'provider': 'free'} for k, v in VOICES.items()]
            el_connected = bool(c and el_key(c))
            el_voices = []
            if el_connected:
                try:
                    el_voices = [{**v, 'provider': 'elevenlabs'} for v in el_list_voices(c)]
                except Exception as e:
                    print('[EL voices]', e)
            return self._json(200, {'voices': voices, 'elevenlabs_connected': el_connected,
                                    'elevenlabs_voices': el_voices})

        if self.path == '/api/reload/options':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, {'reloads': {k: {'charge': v['charge'], 'credit': v['credit'], 'label': v['label']} for k, v in RELOADS.items()}})

        if self.path == '/api/crm/lead-sources':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            srcs = c.get('lead_sources')
            if srcs is None:
                c['lead_sources'] = ['Google', 'Referral', 'Facebook', 'Instagram', 'Word of mouth', 'Drive-by / yard sign', 'Flyer', 'Other']
                save_db(db)
                srcs = c['lead_sources']
            return self._json(200, {'ok': True, 'sources': srcs})

        if self.path == '/api/crm/lead-fields':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            fields = c.setdefault('lead_fields', [])
            return self._json(200, {'ok': True, 'fields': fields})

        if self.path == '/api/crm/leads':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            leads = crm_leads(c)
            return self._json(200, {'leads': leads, 'stats': _crm_stats(leads, _crm_stages(c)), 'stages': _crm_stages(c)})

        if self.path == '/api/clients2':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if _migrate_clients(c):
                save_db(db)
            out = []
            for cl in _clients(c):
                props = _properties_of_client(c, cl['id'])
                out.append({
                    'id': cl['id'], 'name': cl.get('name', ''), 'email': cl.get('email', ''),
                    'phone': cl.get('phone', ''), 'created': cl.get('created', 0),
                    'property_count': len(props),
                    'properties': [{'id': l['id'], 'address': l.get('address', ''),
                                    'name': l.get('client') or l.get('title') or l.get('address') or 'Property',
                                    'stage': l.get('stage', ''), 'prop_status': l.get('prop_status', 'Active'),
                                    'amount': l.get('amount'), 'job_date': l.get('job_date', ''),
                                    'photo_count': len(l.get('photos') or []),
                                    'op_notes': bool((l.get('op_notes') or '').strip()),
                                    'route_stop': bool(l.get('route_stop'))} for l in props],
                })
            out.sort(key=lambda x: x.get('name', '').lower())
            return self._json(200, {'clients': out})

        if self.path == '/api/clients':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            leads = [l for l in crm_leads(c) if l.get('stage') in ('Booked/Approved', 'Job Completed')]
            projects = []
            for l in leads:
                ests = [e for e in c.get('estimates', []) if e.get('lead_id') == l.get('id')]
                notes = sum(1 for a in (l.get('activities') or []) if a.get('type') == 'note')
                projects.append({**l, 'estimates': len(ests), 'notes': notes})
            total = sum((l.get('amount') or 0) for l in leads if l.get('stage') == 'Booked/Approved')
            done = sum((l.get('amount') or 0) for l in leads if l.get('stage') == 'Job Completed')
            return self._json(200, {'projects': projects,
                'stats': {'active': sum(1 for l in leads if l.get('stage') == 'Booked/Approved'),
                          'completed': sum(1 for l in leads if l.get('stage') == 'Job Completed'),
                          'booked_value': total, 'completed_value': done}})

        if self.path == '/api/integrations/google/connect':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            cid = ENV.get('GOOGLE_OAUTH_CLIENT_ID', '')
            if not cid:
                return self._json(500, {'error': 'Google OAuth not configured on server'})
            state = secrets.token_urlsafe(16)
            OAUTH_STATES[state] = {'email': c['email'], 'ts': time.time()}
            base = ENV.get('PORTAL_BASE', 'https://app.anos.ca')
            scopes = 'openid email profile https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/calendar.events'
            url = ('https://accounts.google.com/o/oauth2/v2/auth?response_type=code'
                   '&client_id=' + urllib.parse.quote(cid) +
                   '&redirect_uri=' + urllib.parse.quote(base + '/api/integrations/google/callback') +
                   '&scope=' + urllib.parse.quote(scopes) +
                   '&access_type=offline&prompt=consent&state=' + state)
            return self._json(200, {'url': url})

        if self.path == '/api/integrations/google/callback':
            qs = self._qs()
            code = qs.get('code', [''])[0]
            state = qs.get('state', [''])[0]
            st = OAUTH_STATES.pop(state, None)
            if not st or time.time() - st['ts'] > 600:
                return self._html_redirect('/?view=integrations&google=error')
            cid = ENV.get('GOOGLE_OAUTH_CLIENT_ID', '')
            csecret = ENV.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
            base = ENV.get('PORTAL_BASE', 'https://app.anos.ca')
            try:
                data = urllib.parse.urlencode({
                    'code': code, 'client_id': cid, 'client_secret': csecret,
                    'redirect_uri': base + '/api/integrations/google/callback',
                    'grant_type': 'authorization_code'}).encode()
                req = urllib.request.Request('https://oauth2.googleapis.com/token', data=data,
                    method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded'})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    tok = json.load(resp)
                ureq = urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',
                    headers={'Authorization': 'Bearer ' + tok['access_token']})
                with urllib.request.urlopen(ureq, timeout=20) as resp:
                    uinfo = json.load(resp)
                db = load_db()
                c = db['customers'].get(st['email'])
                if not c:
                    return self._html_redirect('/?view=integrations&google=error')
                entry = {'email': (uinfo.get('email') or '').lower(),
                         'access_token': tok.get('access_token', ''),
                         'refresh_token': tok.get('refresh_token', ''),
                         'expires': int(time.time()) + int(tok.get('expires_in', 3600)),
                         'via': 'oauth'}
                c.setdefault('integrations', {})['gmail'] = {**(c.get('integrations', {}).get('gmail') or {}), **entry}
                c['integrations']['googlecal'] = {**(c.get('integrations', {}).get('googlecal') or {}), **entry}
                save_db(db)
                log_activity(db, c['email'], 'admin', f"Google connected via OAuth ({entry['email']})", save=True)
                return self._html_redirect('/?view=integrations&google=connected')
            except Exception as e:
                print('[OAUTH]', e)
                return self._html_redirect('/?view=integrations&google=error')

        if self.path == '/api/agent/run':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            rid = self._qs().get('id', [''])[0]
            steps = AGENT_RUNS.get(rid, [])
            done = AGENT_RUNS.pop(rid + ':done', False) is True
            if done:
                AGENT_RUNS.pop(rid, None)
            return self._json(200, {'steps': steps, 'done': done})

        if self.path == '/api/client/360':
            # per-client dossier: CRM record + invoices + estimates + activity + notes
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            lid = self._qs().get('id', [''])[0]
            leads = crm_leads(c)
            lead = next((l for l in leads if l.get('id') == lid), None)
            if not lead:
                return self._json(404, {'error': 'client not found'})
            invs = c.get('client_invoices', [])
            if invs and not invs[0].get('client_key'):
                # one-time backfill: attach orphan invoices to the portal client if emails match
                cp = (c.get('client_portal') or {})
                ce = (cp.get('client_email') or '').lower().strip()
                changed = False
                for iv in invs:
                    if not iv.get('client_key'):
                        hit = next((l for l in leads if (l.get('email') or '').lower().strip() and (l.get('email') or '').lower().strip() == ce), None)
                        iv['client_key'] = (hit or leads[0]).get('id') if leads else ''
                        changed = True
                if changed:
                    save_db(db)
            email_l = (lead.get('email') or '').lower().strip()
            phone_d = (lead.get('phone') or '').replace(' ', '')[-10:]
            cinvs = [iv for iv in invs
                     if iv.get('client_key') == lid
                     or (email_l and (iv.get('email') or '').lower().strip() == email_l)
                     or (phone_d and (iv.get('phone') or '').replace(' ', '')[-10:] == phone_d)]
            ests = [e for e in c.get('estimates', []) if e.get('lead_id') == lid]
            open_deals = [l for l in leads if l.get('stage') == 'Booked/Approved'
                          and ((l.get('email') or '').lower().strip() == email_l and email_l
                               or (phone_d and (l.get('phone') or '').replace(' ', '')[-10:] == phone_d))]
            rec = [iv for iv in cinvs if iv.get('recurring')]
            outstanding = sum((iv.get('total') or 0) for iv in cinvs if iv.get('status') == 'sent')
            paid_total = sum((iv.get('total') or 0) for iv in cinvs if iv.get('status') == 'paid')
            # properties = this lead + same-email/phone matches; aggregate photos + op notes
            related = [l for l in leads if l.get('id') == lid
                       or (email_l and (l.get('email') or '').lower().strip() == email_l)
                       or (phone_d and (l.get('phone') or '').replace(' ', '')[-10:] == phone_d)]
            props = []
            for l in related:
                props.append({
                    'id': l.get('id'),
                    'name': l.get('client') or l.get('title') or l.get('address') or 'Property',
                    'address': l.get('address', ''),
                    'stage': l.get('stage', ''),
                    'op_notes': l.get('op_notes', ''),
                    'photos': [{'id': p.get('id'), 'url': p.get('url'), 'caption': p.get('caption', ''),
                                'ts': p.get('ts', 0), 'by': p.get('by', 'owner')}
                               for p in (l.get('photos') or [])],
                })
            return self._json(200, {
                'client': lead,
                'invoices': sorted(cinvs, key=lambda x: x.get('created', 0), reverse=True),
                'estimates': sorted(ests, key=lambda x: x.get('created', 0), reverse=True),
                'open_deals': open_deals,
                'recurring': rec,
                'properties': props,
                'stats': {'outstanding': outstanding, 'paid_total': paid_total,
                          'open_deals_value': sum((l.get('amount') or 0) for l in open_deals),
                          'recurring_mrr': sum((iv.get('total') or 0) for iv in rec if iv.get('recurring', {}).get('interval') == 'month')}})

        if self.path == '/api/lead/invoices':
            # invoices + recurring plans for one lead (client data page hero banner)
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            lid = self._qs().get('id', [''])[0]
            leads = crm_leads(c)
            lead = next((l for l in leads if l.get('id') == lid), None)
            if not lead:
                return self._json(404, {'error': 'client not found'})
            email_l = (lead.get('email') or '').lower().strip()
            phone_d = (lead.get('phone') or '').replace(' ', '')[-10:]
            cinvs = [iv for iv in _client_invoices(c)
                     if iv.get('client_key') == lid
                     or (email_l and (iv.get('email') or '').lower().strip() == email_l)
                     or (phone_d and (iv.get('phone') or '').replace(' ', '')[-10:] == phone_d)]
            rec = [iv for iv in cinvs if iv.get('recurring')]
            return self._json(200, {'invoices': cinvs, 'recurring': rec})

        if self.path == '/api/sms/threads':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            threads = c.setdefault('sms_threads', {})
            out = []
            for phone, msgs in threads.items():
                if not msgs: continue
                last = msgs[-1]
                lead = next((l for l in crm_leads(c) if (l.get('phone') or '').replace(' ', '')[-10:] == phone[-10:]), None)
                out.append({'phone': phone, 'lead_id': lead.get('id') if lead else '',
                            'name': (lead.get('client') or '') if lead else '',
                            'last': last.get('body', '')[:80], 'ts': last.get('ts', 0),
                            'unread': sum(1 for m in msgs if m.get('dir') == 'in' and not m.get('read'))})
            out.sort(key=lambda t: -t['ts'])
            return self._json(200, {'threads': out})


        if self.path == '/api/sms/thread':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            phone = self._qs().get('phone', [''])[0]
            threads = c.setdefault('sms_threads', {})
            msgs = threads.get(phone, [])
            for m in msgs:
                if m.get('dir') == 'in': m['read'] = True
            save_db(db)
            return self._json(200, {'messages': msgs})


        if self.path == '/api/export/leads.csv':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['client', 'title', 'stage', 'amount', 'phone', 'email', 'address', 'job_date', 'created'])
            for l in crm_leads(c):
                w.writerow([l.get('client', ''), l.get('title', ''), l.get('stage', ''), l.get('amount', ''),
                            l.get('phone', ''), l.get('email', ''), l.get('address', ''), l.get('job_date', ''),
                            time.strftime('%Y-%m-%d', time.localtime(l.get('created', 0))) if l.get('created') else ''])
            body = buf.getvalue().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="leads.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == '/api/expenses':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            exps = sorted(_expenses(c), key=lambda x: x.get('date', ''), reverse=True)
            out = []
            for e in exps:
                d = dict(e)
                d['has_receipt'] = bool(e.get('receipt'))
                d.pop('receipt', None)
                out.append(d)
            return self._json(200, {'expenses': out})

        if self.path == '/api/expenses.csv':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            import csv as _csv, io as _io
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow(['Date', 'Vendor', 'Description', 'Category', 'Currency', 'Amount', 'Tax %', 'Tax Label', 'Total w/ Tax'])
            for e in sorted(_expenses(c), key=lambda x: x.get('date', '')):
                total = round(e.get('amount', 0) * (1 + e.get('tax_pct', 0) / 100), 2)
                w.writerow([e.get('date', ''), e.get('vendor', ''), e.get('desc', ''), e.get('category', ''),
                            e.get('currency', 'CAD'), '%.2f' % e.get('amount', 0), e.get('tax_pct', 0), e.get('tax_label', ''), '%.2f' % total])
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="expenses.csv"')
            self.end_headers()
            self.wfile.write(buf.getvalue().encode())
            return
        if self.path == '/api/export/invoices.csv':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['number', 'status', 'created', 'items', 'subtotal', 'tax', 'total'])
            for i2 in _client_invoices(c):
                w.writerow([i2.get('number', ''), i2.get('status', ''),
                            time.strftime('%Y-%m-%d', time.localtime(i2.get('created', 0))),
                            '; '.join(f"{x.get('desc','')} x{x.get('qty',1)}" for x in i2.get('items', [])),
                            i2.get('subtotal', ''), i2.get('tax', ''), i2.get('total', '')])
            body = buf.getvalue().encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Content-Disposition', 'attachment; filename="invoices.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == '/api/client/invoices':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            invs = sorted(_client_invoices(c), key=lambda x: x.get('created', 0), reverse=True)
            now = time.time()
            for iv in invs:
                iv['balance'] = round((iv.get('total') or 0) - sum(p.get('amount', 0) for p in iv.get('payments', [])), 2)
                iv['overdue'] = bool(iv.get('status') == 'sent' and iv.get('due_ts') and iv['due_ts'] < now)
            return self._json(200, {'invoices': invs, 'profile': _client_profile(c),
                                    'client_name': _client_portal(c).get('client_name', ''),
                                    'client_email': _client_portal(c).get('client_email', ''),
                                    'settings': c.get('invoice_settings', {}),
                                    'summary': {
                                        'outstanding': round(sum(i['balance'] for i in invs if i.get('status') == 'sent'), 2),
                                        'overdue': round(sum(i['balance'] for i in invs if i.get('overdue')), 2),
                                        'paid_total': round(sum(i.get('total') or 0 for i in invs if i.get('status') == 'paid'), 2),
                                        'count': len(invs)}})

        if self.path == '/api/estimates':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            ests = sorted(c.setdefault('estimates', []), key=lambda e: e.get('created', 0), reverse=True)
            return self._json(200, {'estimates': ests, 'templates': c.get('estimate_templates', [])})

        if self.path == '/api/activity':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            acts = sorted(c.get('activity', []), key=lambda a: a.get('ts', 0), reverse=True)[:50]
            return self._json(200, {'activity': acts})

        if self.path == '/api/usage/history':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            hist = c.get('usage_history', {})
            today = time.strftime('%Y-%m-%d', time.gmtime())
            # live today value from OpenRouter overrides the stored snapshot
            if c.get('openrouter_key'):
                u = get_key_usage(c['openrouter_key'])
                if 'usage_daily' in u:
                    hist = dict(hist)
                    hist[today] = u['usage_daily']
            days = []
            for i in range(29, -1, -1):
                d = time.strftime('%Y-%m-%d', time.gmtime(time.time() - i * 86400))
                days.append({'date': d, 'usage': hist.get(d, 0)})
            return self._json(200, {'days': days, 'daily_limit': PLANS[c['plan']]['daily_limit']})

        if self.path == '/api/routes/status':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, snow.run_status(c, crm_leads(c)))

        if self.path == '/api/referrals':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            r = _referral(c)
            code = _ref_code(c)
            save_db(db)  # persist a freshly minted code
            return self._json(200, {'ok': True, 'code': code,
                'link': 'https://app.anos.ca/?ref=' + code,
                'payout_email': r.get('payout_email', ''),
                'payout_method': r.get('payout_method', 'etransfer'),
                'signups': sorted(r.get('signups') or [], key=lambda s: s.get('ts', 0), reverse=True),
                'commissions': sorted(r.get('commissions') or [], key=lambda x: x.get('ts', 0), reverse=True),
                'totals': _ref_totals(c)})

        if self.path == '/api/routes/stops':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            raw = [l for l in crm_leads(c) if l.get('route_stop')]
            raw.sort(key=lambda l: (int(l.get('plow_priority') or 2),
                                    l.get('route_order') if l.get('route_order') is not None else 9999,
                                    l.get('client') or l.get('name', '')))
            stops = [{'id': l['id'], 'name': l.get('client') or l.get('name', ''), 'address': l.get('address', ''),
                      'lat': l.get('lat'), 'lon': l.get('lon'), 'geocoded': bool(l.get('lat')),
                      'plow_priority': l.get('plow_priority', 2), 'phone': l.get('phone', ''),
                      'route_order': l.get('route_order'), 'op_notes': l.get('op_notes', ''),
                      'service_min': l.get('service_min')}
                     for l in raw]
            return self._json(200, {'ok': True, 'stops': stops, 'company_type': snow.company_type(c),
                                    'driver_token': snow.driver_token(c),
                                    'depot_address': c.get('depot_address', ''),
                                    'service_min': int(c.get('route_service_min') or snow.DEFAULT_SERVICE_MIN),
                                    'history': snow.route_history(c)[:20]})

        if self.path == '/api/client/route':
            # client-token authed: what one client sees (stop #, ETA, plow pos)
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            db = load_db()
            _, c = _find_customer_by_client_token(auth)
            if not c:
                return self._json(401, {'error': 'not logged in'})
            # resolve which portal login this token belongs to (multi-portal aware)
            ce = ''
            linked = []
            for p in _client_portals(c):
                if p.get('token') and p['token'] == auth:
                    ce = (p.get('email') or '').lower().strip()
                    linked = p.get('property_ids') or []
                    break
            if not ce:
                ce = _client_portal(c).get('client_email', '')
                linked = _client_portal(c).get('property_ids') or []
            view = snow.client_route_view(c, crm_leads(c), ce)
            # fallback: match by linked property ids when email doesn't match a stop lead
            if not view.get('active') and linked:
                view = _route_view_by_property_ids(c, crm_leads(c), linked)
            return self._json(200, view)

        if self.path == '/api/client/notifications/feed':
            db, c = _find_customer_by_client_token(_get_bearer(self))
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            tok = _get_bearer(self)
            portal = _find_client_by_token_multi(c, tok) or _client_portal(c)
            ce = (portal.get('email') or portal.get('client_email') or '').lower().strip()
            feed = (c.get('client_notifications') or {}).get(ce, [])
            return self._json(200, {'ok': True, 'notifications': list(reversed(feed[-30:]))})

        if self.path == '/api/client/weather':
            db, c = _find_customer_by_client_token(_get_bearer(self))
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            lat = c.get('depot_lat')
            lon = c.get('depot_lon')
            if not lat or not lon:
                any_addr = next((l.get('address') for l in crm_leads(c) if l.get('address')), None)
                if any_addr:
                    try:
                        g = snow.geocode(any_addr)
                        if g:
                            lat, lon = g['lat'], g['lon']
                    except Exception:
                        pass
            if not lat or not lon:
                stops = [l for l in crm_leads(c) if l.get('route_stop') and l.get('lat')]
                if stops:
                    lat, lon = stops[0]['lat'], stops[0]['lon']
            if not lat or not lon:
                return self._json(200, {'ok': False})
            try:
                url = ('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s'
                       '&hourly=snow_depth&daily=snowfall_sum&timezone=auto&forecast_days=2' % (lat, lon))
                req = urllib.request.Request(url, headers={'User-Agent': 'anos-portal/1.0'})
                with urllib.request.urlopen(req, timeout=15) as rr:
                    d = json.load(rr)
                daily = (d.get('daily') or {})
                fall = (daily.get('snowfall_sum') or [0, 0])
                depth = (d.get('hourly') or {}).get('snow_depth') or [0]
                return self._json(200, {'ok': True, 'snowfall_today_cm': fall[0] if fall else 0,
                                        'snowfall_tomorrow_cm': fall[1] if len(fall) > 1 else 0,
                                        'snow_depth_m': depth[-1] if depth else 0})
            except Exception as e:
                return self._json(200, {'ok': False, 'error': str(e)[:100]})

        if self.path == '/api/client/notifications':
            db, c = _find_customer_by_client_token(_get_bearer(self))
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            tok = _get_bearer(self)
            portal = _find_client_by_token_multi(c, tok)
            if portal:
                props = _client_props_for(c, portal)
            else:
                portal = _client_portal(c)
                props = _client_properties(c)
            prop_list = [{'id': l['id'], 'name': l.get('client') or l.get('name', ''),
                          'address': l.get('address', '')} for l in props]
            return self._json(200, {'ok': True, 'properties': prop_list,
                                    'notify_phones': portal.get('notify_phones') or []})

        if self.path == '/api/auth/accounts':
            # every account the logged-in person's email can access (for the switcher)
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            person = member['email'] if member else c['email']
            current_owner = c['email']
            accounts = []
            own = db['customers'].get(person)
            if own:
                accounts.append({'owner': person, 'kind': 'owner', 'role': 'owner',
                                 'business': own.get('business', '') or own.get('name', '') or person,
                                 'name': own.get('name', ''), 'current': current_owner == person and not member})
            for owner_email, cust in db['customers'].items():
                m = next((x for x in cust.get('team_members', []) if x.get('email') == person), None)
                if m:
                    accounts.append({'owner': owner_email, 'kind': 'team', 'role': m.get('role', ''),
                                     'business': cust.get('business', '') or owner_email,
                                     'name': m.get('name', ''),
                                     'current': current_owner == owner_email and bool(member)})
            return self._json(200, {'ok': True, 'accounts': accounts})

        if self.path == '/api/me':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            usage = get_key_usage(c['openrouter_key']) if c.get('openrouter_key') else {}
            plan = PLANS[c['plan']]
            self._json(200, {
                'name': member.get('name', c['name']) if member else c['name'],
                'business': c.get('business', ''), 'email': c['email'],
                'phone': c.get('phone', ''),
                'role': 'owner' if not member else member['role'],
                'role_label': 'Owner' if not member else TEAM_ROLES.get(member['role'], {}).get('label', member['role']),
                'can_edit': True if not member else TEAM_ROLES.get(member['role'], {}).get('can_edit', False),
                'member_email': member['email'] if member else '',
                'plan': c['plan'], 'plan_name': plan['name'], 'monthly': plan['monthly'],
                'daily_limit': plan['daily_limit'],
                'usage': {
                    'today': usage.get('usage_daily', 0),
                    'week': usage.get('usage_weekly', 0),
                    'month': usage.get('usage_monthly', 0),
                    'limit_remaining': usage.get('limit_remaining'),
                },
                'billing': {'next_amount': plan['monthly'], 'status': c.get('billing_status', 'inactive'),
                            'card': c.get('card_last4', '—'),
                            'stripe_customer': bool(c.get('stripe_customer_id')),
                            'trial_end': c.get('trial_end'), 'trial_used': bool(c.get('trial_used'))},
                'plans': {k: {'name': v['name'], 'monthly': v['monthly']} for k, v in PLANS.items()},
                'trial_days': TRIAL_DAYS,
                'integrations_quo': bool((c.get('integrations') or {}).get('quo', {}).get('number')),
                'inference_note': 'Usage shown in USD of AI credits.',
                'voice': c.get('voice', 'sonia'), 'theme': c.get('theme', 'dark'),
                'company_type': snow.company_type(c),
                'dashboard_template': c.get('dashboard_template', 'owner'),
                'settings_profile': c.get('settings_profile', {}),
                'settings_defaults': c.get('settings_defaults', {}),
                'settings_notifications': c.get('settings_notifications', {}),
                'settings_agent': c.get('settings_agent', {}),
                'settings_branding': c.get('settings_branding', {}),
                'settings_tax': c.get('settings_tax', {}),
                'settings_service_area': c.get('settings_service_area', {}),
                'settings_booking': c.get('settings_booking', {}),
                'onboarding_dismissed': bool(c.get('onboarding_dismissed')),
                'setup': {
                    'profile': bool(c.get('name') and c.get('business')),
                    'ai_key': bool(c.get('openrouter_key')),
                    'billing': bool(c.get('stripe_customer_id')),
                    'chatted': bool(c.get('chat_log')),
                },
            })
            return

        if self.path == '/api/social/posts':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            posts = c.setdefault('social_posts', [])
            posts_sorted = sorted(posts, key=lambda p: p.get('scheduled_at') or '', reverse=False)
            return self._json(200, {'posts': posts_sorted})

        if self.path == '/api/social/calendar':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            qs = self._qs()
            try:
                y = int((qs.get('y') or [time.strftime('%Y')])[0])
                m = int((qs.get('m') or [time.strftime('%m')])[0])
            except Exception:
                y, m = int(time.strftime('%Y')), int(time.strftime('%m'))
            posts = c.setdefault('social_posts', [])
            by_date = {}
            for p in posts:
                d = (p.get('scheduled_at') or '')[:10]
                if not d:
                    continue
                try:
                    py, pm, _ = d.split('-')
                    if int(py) == y and int(pm) == m:
                        by_date.setdefault(d, []).append(p)
                except Exception:
                    pass
            return self._json(200, {'ok': True, 'year': y, 'month': m, 'by_date': by_date, 'posts': posts})

        if self.path == '/api/tasks':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            tasks = c.setdefault('tasks', [])
            order = {'todo': 0, 'inprogress': 1, 'done': 2}
            prio = {'high': 0, 'medium': 1, 'low': 2}
            tasks_sorted = sorted(tasks, key=lambda t: (order.get(t.get('status'), 9), prio.get(t.get('priority'), 9), t.get('due') or '9999', t.get('created', 0)))
            return self._json(200, {'ok': True, 'tasks': tasks_sorted})


        if self.path == '/api/subdomain':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            dom = c.get('portal_domain') or ''
            # check if cert exists
            ssl_ready = False
            if dom:
                try:
                    import subprocess
                    result = subprocess.run(['certbot', 'certificates', '--cert-name', dom],
                                          capture_output=True, text=True, timeout=10)
                    ssl_ready = 'Certificate Name' in result.stdout
                except Exception:
                    pass
            return self._json(200, {'ok': True, 'domain': dom,
                                    'url': f'https://{dom}/client' if dom else '',
                                    'cname_target': 'app.anos.ca',
                                    'ssl_ready': ssl_ready,
                                    'branding': c.get('portal_branding') or {},
                                    'available': None})

        if self.path == '/api/email/from':
            # effective sender identity for the invoice/estimate send dialogs
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            s = c.get('smtp') or {}
            prof = c.get('settings_profile') or {}
            integ = (c.get('integrations') or {}).get('gmail') or {}
            route, frm = None, ''
            if smtp_conf(c):
                route, frm = 'smtp', s.get('from_email') or s.get('user') or ''
            elif (s.get('provider') or '') in ('brevo', 'mailgun') and (s.get('from_email') or s.get('user')):
                route, frm = s['provider'], s.get('from_email') or s.get('user') or ''
            elif integ.get('email'):
                route, frm = 'gmail', integ.get('email')
            return self._json(200, {'ok': True, 'route': route, 'from_email': frm,
                                    'from_name': prof.get('from_name') or '',
                                    'reply_to': prof.get('reply_to') or ''})

        if self.path == '/api/contracts':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            cid = self._qs().get('client_id', [''])[0]
            out = [dict(ct, **_contract_rollup(c, ct)) for ct in _contracts(c)
                   if ct.get('status') != 'ended' and (not cid or ct.get('client_id') == cid)]
            return self._json(200, {'ok': True, 'contracts': out})

        if self.path == '/api/telephony/status':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            sub = c.get('twilio_sub') or {}
            w = _sms_wallet(c)
            t = twilio_conf(c)
            return self._json(200, {'ok': True,
                                    'platform_ready': bool(TWILIO_MASTER_SID and TWILIO_MASTER_TOKEN),
                                    'subaccount': bool(sub.get('sid')),
                                    'number': sub.get('number') or '',
                                    'byo_number': (c.get('twilio') or {}).get('from') or '',
                                    'active_from': (t or {}).get('from') or '',
                                    'balance': round(w.get('balance') or 0, 2),
                                    'sms_price': SMS_PRICE_CAD,
                                    'activation_fee': NUMBER_ACTIVATION_CAD,
                                    'ledger': (w.get('ledger') or [])[-10:][::-1]})

        if self.path == '/api/telephony/numbers/search':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            q = self._qs()
            area = re.sub(r'\D', '', q.get('area', [''])[0])[:3]
            country = (q.get('country', ['CA'])[0] or 'CA').upper()
            if country not in ('CA', 'US'):
                country = 'CA'
            sub = c.get('twilio_sub') or {}
            if not (sub.get('sid') and sub.get('token')):
                return self._json(400, {'error': 'Phone account not provisioned yet'})
            params = {'SmsEnabled': 'true', 'PageSize': 8}
            if area:
                params['AreaCode'] = area
            ok, r = twilio_api(sub['sid'], sub['token'], 'GET',
                               f"/Accounts/{sub['sid']}/AvailablePhoneNumbers/{country}/Local.json", params)
            if not ok:
                return self._json(400, {'error': r})
            out = [{'number': n.get('phone_number'), 'locality': n.get('locality') or '',
                    'region': n.get('region') or ''}
                   for n in (r.get('available_phone_numbers') or [])]
            return self._json(200, {'ok': True, 'numbers': out})

        if self.path == '/api/subdomain/check':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            dom = _domain_clean(self._qs().get('sub', [''])[0])
            if not dom:
                return self._json(200, {'available': False, 'reason': 'Invalid format'})
            if dom in ('anos.ca', 'app.anos.ca', 'api.anos.ca', 'www.anos.ca'):
                return self._json(200, {'available': False, 'reason': 'Reserved'})
            return self._json(200, {'available': _domain_available(db, dom, exclude_email=c['email']), 'domain': dom})

        if self.path == '/api/client/preview-status':
            # owner previews the client portal as one of their portal clients
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            pid = self._qs().get('id', [''])[0]
            p = next((x for x in _client_portals(c) if x.get('id') == pid), None)
            if not p:
                return self._json(404, {'error': 'portal client not found'})
            st = get_client_status(c, portal_override=p)
            st['ok'] = True
            st['preview'] = True
            return self._json(200, st)

        if self.path == '/api/subdomain/resolve':
            # public: given a domain, return the client portal config (business name, branding)
            dom = _domain_clean(self._qs().get('sub', [''])[0])
            db2, c2 = _find_customer_by_domain(dom)
            if not c2:
                return self._json(404, {'error': 'not found'})
            cp = _client_portal(c2)
            branding = c2.get('portal_branding') or {}
            return self._json(200, {'business': c2.get('business', ''), 'client_name': cp.get('client_name', ''),
                                    'client_email': cp.get('client_email', ''), 'enabled': bool(cp.get('enabled')),
                                    'branding': branding})

        if self.path == '/api/apikeys':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            keys = c.setdefault('api_keys', [])
            # mask the keys for display
            out = [{'name': k.get('name'), 'created': k.get('created'), 'revoked': k.get('revoked'),
                    'key_preview': k.get('key', '')[:12] + '...'} for k in keys]
            return self._json(200, {'keys': out})

        if self.path == '/api/smtp':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            s = dict(c.get('smtp') or {})
            if s.get('password'):
                s['password'] = '***'
            return self._json(200, {'smtp': s, 'configured': bool(smtp_conf(c))})

        if self.path == '/api/seo/audits':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            audits = sorted(c.setdefault('seo_audits', []), key=lambda a: a.get('ts', 0), reverse=True)
            return self._json(200, {'audits': audits[:20], 'keywords': c.setdefault('seo_keywords', []),
                                    'strategy': c.get('seo_strategy')})

        if self.path == '/api/documents':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            docs = sorted(_client_documents(c), key=lambda d: d.get('ts', 0), reverse=True)
            return self._json(200, {'documents': docs})

        if self.path == '/api/client/documents':
            tok = _get_bearer(self)
            db2, c2 = _find_customer_by_client_token(tok)
            if not c2:
                return self._json(401, {'error': 'invalid client token'})
            # resolve which portal login this is
            ce = ''
            linked = []
            for p in _client_portals(c2):
                if p.get('token') and p['token'] == tok:
                    ce = (p.get('email') or '').lower().strip()
                    linked = p.get('property_ids') or []
                    break
            if not ce:
                cp2 = _client_portal(c2)
                ce = (cp2.get('client_email') or '').lower().strip()
                linked = cp2.get('property_ids') or []
            out = []
            for d in _client_documents(c2):
                if (d.get('client_email') or '').lower() == ce or d.get('lead_id') in linked:
                    out.append(d)
            out.sort(key=lambda d: d.get('ts', 0), reverse=True)
            return self._json(200, {'documents': out, 'token': tok})

        if self.path == '/api/analytics':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            qs = self._qs()
            days = int((qs.get('days') or ['30'])[0])
            if days not in (7, 30, 90, 365):
                days = 30
            now = time.time()
            cutoff = now - days * 86400
            leads = crm_leads(c)
            ests = c.get('estimates', [])
            invs = c.get('client_invoices', [])
            portals = _client_portals(c)

            def _ts(x):
                v = x.get('created') or x.get('ts') or 0
                if isinstance(v, str):
                    try: return time.mktime(time.strptime(v[:10], '%Y-%m-%d'))
                    except Exception: return 0
                return v

            # headline stats
            total_clients = sum(1 for l in leads if l.get('stage') in ('Booked/Approved', 'Job Completed'))
            active_clients = sum(1 for l in leads if l.get('stage') == 'Booked/Approved')
            portal_users = len([p for p in portals if p.get('enabled', True)])
            new_leads = sum(1 for l in leads if _ts(l) >= cutoff)

            # revenue: paid invoices + won deals
            def _rev(inv):
                return inv.get('total') or sum(li.get('price', 0) for li in inv.get('line_items', [])) or 0
            paid_revenue = sum(_rev(i) for i in invs if i.get('status') == 'paid' and _ts(i) >= cutoff)
            outstanding = sum(_rev(i) for i in invs if i.get('status') == 'sent')
            won_value = sum((l.get('amount') or 0) for l in leads if l.get('stage') == 'Job Completed' and _ts(l) >= cutoff)
            pipeline_value = sum((l.get('amount') or 0) for l in leads if l.get('stage') in ('Lead', 'Quote Sent', 'In Progress', 'Booked/Approved'))

            # revenue by day (paid invoices + completed jobs)
            rev_by_day = {}
            for i in invs:
                if i.get('status') == 'paid':
                    d = time.strftime('%Y-%m-%d', time.localtime(_ts(i)))
                    rev_by_day[d] = rev_by_day.get(d, 0) + _rev(i)
            for l in leads:
                if l.get('stage') == 'Job Completed':
                    d = time.strftime('%Y-%m-%d', time.localtime(_ts(l)))
                    rev_by_day[d] = rev_by_day.get(d, 0) + (l.get('amount') or 0)
            rev_series = []
            for i in range(days - 1, -1, -1):
                d = time.strftime('%Y-%m-%d', time.localtime(now - i * 86400))
                rev_series.append({'date': d, 'revenue': round(rev_by_day.get(d, 0), 2)})

            # leads by day
            lead_by_day = {}
            for l in leads:
                d = time.strftime('%Y-%m-%d', time.localtime(_ts(l)))
                lead_by_day[d] = lead_by_day.get(d, 0) + 1
            lead_series = []
            for i in range(days - 1, -1, -1):
                d = time.strftime('%Y-%m-%d', time.localtime(now - i * 86400))
                lead_series.append({'date': d, 'count': lead_by_day.get(d, 0)})

            # funnel
            funnel = {}
            for l in leads:
                funnel[l.get('stage', 'Lead')] = funnel.get(l.get('stage', 'Lead'), 0) + 1

            # estimate conversion
            est_sent = [e for e in ests if e.get('status') in ('sent', 'approved', 'declined')]
            est_approved = [e for e in ests if e.get('status') == 'approved']
            win_rate = round(100 * len(est_approved) / len(est_sent)) if est_sent else 0

            # MRR from subscriptions
            subs = c.get('subscriptions', [])
            mrr = sum(s.get('amount', 0) for s in subs if s.get('status') == 'active')

            # cumulative revenue (running total)
            cum = 0
            cum_series = []
            for p in rev_series:
                cum += p['revenue']
                cum_series.append({'date': p['date'], 'total': round(cum, 2)})

            # lead source breakdown
            sources = {}
            for l in leads:
                if _ts(l) >= cutoff:
                    src_name = (l.get('source') or 'Unknown').strip() or 'Unknown'
                    sources[src_name] = sources.get(src_name, 0) + 1

            # expenses: totals + by category (within period by date field)
            exps = c.get('expenses', [])
            def _exp_total(e):
                amt = float(e.get('amount') or 0)
                return amt * (1 + float(e.get('tax_pct') or 0) / 100)
            def _exp_ts(e):
                d = e.get('date') or ''
                try:
                    return time.mktime(time.strptime(d[:10], '%Y-%m-%d'))
                except Exception:
                    return e.get('created') or 0
            exp_in_period = [e for e in exps if _exp_ts(e) >= cutoff]
            expenses_total = sum(_exp_total(e) for e in exp_in_period)
            exp_by_cat = {}
            exp_by_cur = {}
            for e in exp_in_period:
                cat = (e.get('category') or 'other').lower()
                exp_by_cat[cat] = exp_by_cat.get(cat, 0) + _exp_total(e)
                cur = e.get('currency') or 'CAD'
                exp_by_cur[cur] = exp_by_cur.get(cur, 0) + _exp_total(e)
            exp_by_day = {}
            for e in exp_in_period:
                d = (e.get('date') or '')[:10]
                if d:
                    exp_by_day[d] = exp_by_day.get(d, 0) + _exp_total(e)
            exp_series = []
            for i in range(days - 1, -1, -1):
                d = time.strftime('%Y-%m-%d', time.localtime(now - i * 86400))
                exp_series.append({'date': d, 'expenses': round(exp_by_day.get(d, 0), 2)})

            # profit = revenue (paid invoices + completed jobs) - expenses
            gross_revenue = paid_revenue + won_value
            profit = gross_revenue - expenses_total
            margin = round(100 * profit / gross_revenue) if gross_revenue > 0 else 0

            # pending contract value: active contracts minus what's been collected against them
            contracts_pending = 0.0
            contracts_active = 0
            for ct in _contracts(c):
                if ct.get('status') != 'active':
                    continue
                roll = _contract_rollup(c, ct)
                contracts_pending += roll['remaining']
                contracts_active += 1

            return self._json(200, {
                'days': days,
                'cum_series': cum_series,
                'exp_series': exp_series,
                'exp_by_cat': {k: round(v, 2) for k, v in exp_by_cat.items()},
                'sources': sources,
                'stats': {
                    'total_clients': total_clients,
                    'active_clients': active_clients,
                    'portal_users': portal_users,
                    'new_leads': new_leads,
                    'total_leads': len(leads),
                    'paid_revenue': round(paid_revenue, 2),
                    'outstanding': round(outstanding, 2),
                    'won_value': round(won_value, 2),
                    'expenses_total': round(expenses_total, 2),
                    'profit': round(profit, 2),
                    'margin': margin,
                    'exp_by_cur': {k: round(v, 2) for k, v in exp_by_cur.items()},
                    'pipeline_value': round(pipeline_value, 2),
                    'win_rate': win_rate,
                    'mrr': round(mrr, 2),
                    'contracts_pending': round(contracts_pending, 2),
                    'contracts_active': contracts_active,
                },
                'rev_series': rev_series,
                'lead_series': lead_series,
                'funnel': funnel,
            })

        if self.path.startswith('/api/sign-doc/'):
            token = self.path.split('/api/sign-doc/')[-1].split('?')[0][:64]
            db, c, doc = _find_doc_by_sign_token(token)
            if not doc:
                return self._json(404, {'error': 'invalid or expired signing link'})
            base = _public_base(c)
            return self._json(200, {
                'name': doc.get('name', 'Document'),
                'doc_type': doc.get('doc_type', 'other'),
                'note': doc.get('note', ''),
                'url': ((doc.get('url', '') if str(doc.get('url', '')).startswith('http') else base + doc.get('url', ''))
                        + ('?sigtok=' + doc.get('sign_token', '') if doc.get('sign_token') and not str(doc.get('url', '')).startswith('http') else '')),
                'business': (c.get('settings') or {}).get('business_name') or c.get('email', ''),
                'status': doc.get('sign_status', 'sent'),
                'signed_by': doc.get('signed_by', ''),
                'fields': doc.get('sign_fields') or [],
                'signed_pdf_url': doc.get('signed_pdf_url', ''),
            })

        if self.path.startswith('/invoice-pay/'):
            token = self.path.split('/invoice-pay/')[-1].split('?')[0][:80]
            q = self._qs()
            done = q.get('done', [''])[0] == '1'
            db = load_db()
            c, inv = _find_invoice_by_pay_token(db, token)
            if not inv:
                return self._json(404, {'error': 'payment link not found'})
            bal = _invoice_balance(inv)
            amount = bal
            if inv.get('deposit') and not (inv.get('payments') or []):
                try:
                    amount = min(bal, round(float(inv.get('deposit') or bal), 2))
                except (TypeError, ValueError):
                    amount = bal
            biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'Contractor')
            biz_esc = biz.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            num_esc = str(inv.get('number', 'Invoice')).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            paid = done or bal <= 0
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            body = ("<h1>" + ("Payment received ✓" if paid else "Pay " + num_esc) + "</h1>"
                    + "<p class='sub'>" + biz_esc + (" — thanks, your payment is being confirmed by Stripe." if paid else " — secure card payment") + "</p>"
                    + "<div class='card'><div class='amt'>$" + f"{amount:,.2f}" + "<span> CAD</span></div>"
                    + ("<div class='fine'>You can close this page. A Stripe receipt will arrive by email.</div>" if paid else
                       "<div class='fine'>" + ("Deposit due now" if amount < bal else "Amount due") + (" · balance after: $" + f"{bal - amount:,.2f}" if amount < bal else "") + "</div><button id='pay' onclick='go()'>Pay by card →</button><div class='err' id='err'></div>") + "</div>"
                    + "<div class='secure'>🔒 Secure payment by Stripe — " + biz_esc + " never sees your card number.</div>")
            js = "" if paid else ("<script>const TOKEN=" + json.dumps(token) + ";"
                "async function go(){const b=document.getElementById('pay');b.disabled=true;b.textContent='Opening Stripe…';"
                "const r=await fetch('/api/invoice-pay/'+TOKEN+'/checkout',{method:'POST'});const d=await r.json();"
                "if(d.url){location.href=d.url;}else{b.disabled=false;b.textContent='Pay by card →';document.getElementById('err').textContent=d.error||'Could not start checkout';}}</script>")
            page = ("<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<title>" + num_esc + " — " + biz_esc + "</title><style>"
                    "body{font-family:Inter,-apple-system,'Segoe UI',sans-serif;background:#080a10;color:#f0f2f7;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}"
                    ".wrap{max-width:420px;width:100%;text-align:center}h1{font-size:1.5rem;font-weight:800;letter-spacing:-.02em}.sub{color:#8b93a3;font-size:.88rem;margin:8px 0 22px;line-height:1.5}"
                    ".card{background:#131824;border:1px solid #232c3d;border-radius:16px;padding:26px 24px;box-shadow:0 10px 28px rgba(0,0,0,.3)}"
                    ".amt{font-size:2.2rem;font-weight:800}.amt span{font-size:.9rem;color:#5c6577;font-weight:600}"
                    "button{margin-top:18px;width:100%;background:linear-gradient(135deg,#4f7dff,#3b62d6);color:#fff;border:none;border-radius:12px;padding:15px;font-size:1rem;font-weight:800;cursor:pointer}"
                    "button:disabled{opacity:.55}.fine{color:#5c6577;font-size:.72rem;margin-top:12px}.secure{color:#3f4756;font-size:.68rem;margin-top:18px}.err{color:#f87171;font-size:.8rem;margin-top:10px;min-height:1em}"
                    "</style></head><body><div class='wrap'>" + body + js + "</div></body></html>")
            self.wfile.write(page.encode())
            return

        if self.path.startswith('/invoice-view/'):
            inv_id = self.path.split('/invoice-view/')[-1].split('?')[0][:20]
            q = self._qs()
            tok = q.get('tok', [''])[0]
            dom = (self.headers.get('Host') or 'app.anos.ca')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            pg = ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,user-scalable=yes">'
                  '<title>Invoice Preview</title>'
                  '<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>'
                  '<style>*{box-sizing:border-box;margin:0}body{background:#0b0d10;color:#e8ecf1;font-family:-apple-system,Segoe UI,Inter,sans-serif;min-height:100vh}'
                  '.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:12px 14px;background:#131720;border-bottom:1px solid #232a36}'
                  '.bar .t{font-weight:700;font-size:.9rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
                  '.bar button,.bar a{background:#1a2029;border:1px solid #2a3342;color:#7fb0ff;border-radius:9px;padding:9px 14px;font-size:.8rem;font-weight:700;text-decoration:none;cursor:pointer;white-space:nowrap}'
                  '.bar .back{color:#aab3c0}'
                  '#pages{padding:12px 10px 60px;max-width:760px;margin:0 auto}'
                  '.pg{margin:0 auto 16px;background:#fff;border-radius:8px;box-shadow:0 2px 14px rgba(0,0,0,.45);overflow:hidden}'
                  '.pg canvas{display:block;width:100%;height:auto}'
                  '.ld{text-align:center;color:#8b94a3;padding:40px;font-size:.9rem}</style></head><body>'
                  '<div class="bar"><button class="back" onclick="history.length>1?history.back():window.close()">← Back</button>'
                  '<div class="t">Invoice Preview</div>'
                  '<a href="/invoice/' + inv_id + '.pdf?tok=' + tok + '" target="_blank" rel="noopener">Download</a></div>'
                  '<div id="pages"><div class="ld">Loading invoice…</div></div>'
                  '<script>'
                  'const FILE_URL=' + json.dumps('/invoice/' + inv_id + '.pdf?tok=' + tok) + ';'
                  'pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";'
                  '(async()=>{const host=document.getElementById("pages");try{'
                  'const pdf=await pdfjsLib.getDocument(FILE_URL).promise;host.innerHTML="";'
                  'for(let p=1;p<=pdf.numPages;p++){const el=document.createElement("div");el.className="pg";host.appendChild(el);'
                  'const page=await pdf.getPage(p);const vp=page.getViewport({scale:2.2});'
                  'const cv=document.createElement("canvas");cv.width=vp.width;cv.height=vp.height;el.appendChild(cv);'
                  'await page.render({canvasContext:cv.getContext("2d"),viewport:vp}).promise;}}'
                  'catch(e){host.innerHTML=\'<div class="ld">Could not render here — <a style="color:#7fb0ff;font-weight:700" href="\'+FILE_URL+\'" target="_blank" rel="noopener">download the PDF</a>.</div>\';}})();'
                  '</script></body></html>')
            self.wfile.write(pg.encode())
            return

        if self.path.startswith('/invoice/'):
            inv_id = self.path.split("/invoice/")[-1].split("?")[0].replace(".pdf", "")[:20]
            q = self._qs()
            tok = _get_bearer(self) or q.get("tok", [""])[0]
            db = load_db()
            owner_c, inv = None, None
            for em, cc in db["customers"].items():
                for i2 in _client_invoices(cc):
                    if i2.get("id") == inv_id:
                        owner_c, inv = cc, i2
                        break
                if inv:
                    break
            if not inv:
                return self._json(404, {"error": "invoice not found"})
            ok = False
            if tok:
                email_sess, _member = _session_user(tok)
                if email_sess and email_sess in db["customers"] and db["customers"][email_sess] is owner_c:
                    ok = True
                if not ok:
                    cp = (owner_c.get("client_portal") or {})
                    ok = (cp.get("token") == tok) or any(p.get("token") == tok for p in _client_portals(owner_c))
            if not ok:
                return self._json(401, {"error": "unauthorized"})
            client = None
            if inv.get("client_key"):
                l = next((x for x in _crm_leads(owner_c) if x.get("id") == inv["client_key"]), None)
                if l:
                    client = {"name": l.get("client") or l.get("title") or "", "email": l.get("email", ""), "address": l.get("address", "")}
            try:
                pdf = invoice_pdf.render_invoice_pdf(owner_c, inv, client, str(BASE))
            except Exception as e:
                return self._json(500, {"error": "pdf render failed: " + str(e)})
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'inline; filename="' + inv.get('number', 'invoice') + '.pdf"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(pdf)
            return

        if self.path.startswith('/photo/'):
            ptok = self.path.split('/photo/')[-1].split('?')[0][:40]
            db = load_db()
            found = None
            for em, cc in db['customers'].items():
                for l in _crm_leads(cc):
                    for p in (l.get('photos') or []):
                        if p.get('ptok') == ptok:
                            found = (cc, p)
                            break
                    if found:
                        break
                if found:
                    break
            if not found:
                return self._json(404, {'error': 'not found'})
            cc, p = found
            owner_dir = re.sub(r'[^a-z0-9@._-]', '', cc.get('email', 'unknown'))
            fname = str(p.get('url', '')).rsplit('/', 1)[-1]
            f = BASE / 'uploads' / owner_dir / fname
            if not f.exists():
                return self._json(404, {'error': 'file missing'})
            ext = f.suffix.lower().lstrip('.')
            ctype = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                     'webp': 'image/webp', 'gif': 'image/gif'}.get(ext, 'application/octet-stream')
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Cache-Control', 'max-age=86400')
            self.end_headers()
            self.wfile.write(f.read_bytes())
            return

        if self.path.startswith('/doc-view/'):
            doc_id = self.path.split('/doc-view/')[-1].split('?')[0][:32]
            q = self._qs()
            tok = _get_bearer(self) or q.get('tok', [''])[0]
            sigtok = q.get('sigtok', [''])[0]
            db = load_db()
            found_doc, owner_c = None, None
            for em, cc in db['customers'].items():
                for d in _client_documents(cc):
                    if d.get('id') == doc_id:
                        found_doc, owner_c = d, cc
                        break
                if found_doc:
                    break
            if not found_doc:
                return self._json(404, {'error': 'document not found'})
            ok = False
            if tok:
                email_sess, _member = _session_user(tok)
                if email_sess and email_sess in db['customers'] and db['customers'][email_sess] is owner_c:
                    ok = True
                if not ok:
                    cp = (owner_c.get('client_portal') or {})
                    ok = (cp.get('token') == tok) or any(p.get('token') == tok for p in _client_portals(owner_c))
            if not ok and sigtok and found_doc.get('sign_token') == sigtok:
                ok = True
            if not ok:
                return self._json(401, {'error': 'unauthorized'})
            raw_url = str(found_doc.get('url', ''))
            file_url = raw_url if raw_url.startswith('http') else raw_url + ('?sigtok=' + sigtok if sigtok else '?tok=' + tok)
            mime = str(found_doc.get('mime') or '')
            ext = (str(found_doc.get('name') or raw_url).rsplit('.', 1)[-1] or '').lower()
            if mime.startswith('image/') or ext in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
                ftype = 'image'
            elif 'html' in mime or ext in ('html', 'htm'):
                ftype = 'html'
            elif 'wordprocessingml' in mime or ext == 'docx':
                ftype = 'docx'
            elif 'pdf' in mime or ext == 'pdf':
                ftype = 'pdf'
            else:
                ftype = 'other'
            nm = (found_doc.get('name') or 'Document').replace('<', '&lt;').replace('>', '&gt;').replace(chr(34), '&quot;')
            base = _public_base(owner_c)
            sign_url = (base + '/sign-doc/' + found_doc['sign_token']) if found_doc.get('sign_token') else ''
            signed_pdf = str(found_doc.get('signed_pdf_url') or '')
            if signed_pdf and not signed_pdf.startswith('http'):
                signed_pdf = base + signed_pdf + (('?sigtok=' + found_doc.get('sign_token', '')) if found_doc.get('sign_token') else '')
            sess_email, sess_member = _session_user(tok) if tok else (None, None)
            owner_ok = bool(sess_email and sess_member is None and sess_email in db['customers'] and db['customers'][sess_email] is owner_c)
            status = 'signed' if (found_doc.get('sign_status') == 'signed' or found_doc.get('signed_ts')) else ('pending' if found_doc.get('sign_token') else 'draft')
            status_label = {'signed': 'Signed', 'pending': 'Pending signature', 'draft': 'Draft'}[status]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            page = ('<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,user-scalable=yes">'
                    + '<title>' + nm + '</title>'
                    + '<script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>'
                    + '<script src="https://cdnjs.cloudflare.com/ajax/libs/mammoth/1.6.0/mammoth.browser.min.js"></script>'
                    + '<style>*{box-sizing:border-box;margin:0}body{background:#0b0d10;color:#e8ecf1;font-family:-apple-system,Segoe UI,Inter,sans-serif;min-height:100vh}'
                    + '.bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:10px;padding:12px 14px;background:#131720;border-bottom:1px solid #232a36;flex-wrap:wrap}'
                    + '.bar .t{font-weight:700;font-size:.9rem;flex:1;min-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
                    + '.bar a,.bar button{background:#1a2029;border:1px solid #2a3342;color:#7fb0ff;border-radius:9px;padding:9px 14px;font-size:.8rem;font-weight:700;text-decoration:none;cursor:pointer;white-space:nowrap}'
                    + '.bar .back{color:#e8ecf1;background:#232b38;border-color:#33405a}'
                    + '.bar .pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:6px 11px;font-size:.68rem;font-weight:800;letter-spacing:.03em;text-transform:uppercase;border:1px solid;white-space:nowrap}'
                    + '.bar .pill.draft{background:rgba(139,148,163,.1);color:#aab3c0;border-color:rgba(139,148,163,.28)}'
                    + '.bar .pill.pending{background:rgba(251,191,36,.1);color:#fbbf24;border-color:rgba(251,191,36,.32)}'
                    + '.bar .pill.signed{background:rgba(74,222,128,.1);color:#4ade80;border-color:rgba(74,222,128,.32)}'
                    + '.bar .acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}'
                    + '@media(max-width:640px){.bar{align-items:flex-start}.bar .t{flex-basis:100%;order:2}.bar .back{order:1}.bar .pill{order:3}.bar .acts{order:4;width:100%}.bar .acts a,.bar .acts button{flex:1;text-align:center}}'
                    + '#pages{padding:14px 10px 60px;max-width:860px;margin:0 auto}'
                    + '.pg{margin:0 auto 16px;background:#fff;border-radius:8px;box-shadow:0 2px 14px rgba(0,0,0,.45);overflow:hidden}'
                    + '.pg canvas{display:block;width:100%;height:auto}'
                    + '.docx-page{background:#fff;color:#111827;border-radius:8px;box-shadow:0 2px 14px rgba(0,0,0,.45);padding:34px 28px;margin:0 auto 16px;overflow-wrap:anywhere}'
                    + '.docx-page img{max-width:100%;height:auto}.docx-page table{width:100%;border-collapse:collapse}.docx-page td,.docx-page th{border:1px solid #d1d5db;padding:6px 8px}'
                    + '.ld{text-align:center;color:#8b94a3;padding:40px;font-size:.9rem}</style>'
                    + '</head><body>'
                    + '<div class="bar"><button class="back" onclick="history.length>1?history.back():window.close()">← Back</button><div class="t">' + nm + '</div><span class="pill ' + status + '">' + status_label + '</span><div class="acts">'
                    + (('<a href="' + signed_pdf + '" target="_blank" rel="noopener">Signed PDF</a>') if status == 'signed' and signed_pdf else '')
                    + (('<a href="/?docfields=' + doc_id + '" target="_blank" rel="noopener">Place Fields</a>') if owner_ok and ftype == 'pdf' and status != 'signed' else '')
                    + (('<button onclick="emailForSignature()">' + ('Resend Email' if status == 'pending' else 'Send to Email') + '</button><button onclick="copySignLink()">Copy Link</button>') if owner_ok and status != 'signed' else '')
                    + (('<button onclick="copySignLink()">Copy Sign Link</button>') if (not owner_ok and status == 'pending' and sign_url) else '')
                    + '<a href="' + file_url + '" target="_blank" rel="noopener">Download</a></div></div>'
                    + '<div id="pages"><div class="ld">Loading document…</div></div>'
                    + '<style>.pg iframe{width:100%;height:82vh;border:none;background:#fff;border-radius:8px;transform-origin:0 0}.pg img{display:block;width:100%;border-radius:8px}#zoombar{display:flex;gap:8px;padding:8px 12px;position:sticky;top:57px;z-index:4;background:rgba(19,23,32,.92);backdrop-filter:blur(6px)}#zoombar button{background:#1a2029;border:1px solid #2a3342;color:#aab3c0;border-radius:8px;padding:7px 14px;font-size:.85rem;font-weight:700;cursor:pointer}</style>'
                    + '<script>'
                    + 'const FILE_URL=' + repr(file_url) + ',FTYPE=' + repr(ftype) + ',DOC_ID=' + repr(doc_id) + ',OWNER_TOKEN=' + repr(tok if owner_ok else '') + ',SIGN_URL=' + repr(sign_url) + ';'
                    + 'function copyText(t){if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(()=>alert("Signing link copied"));return;}const ta=document.createElement("textarea");ta.value=t;ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();try{document.execCommand("copy");alert("Signing link copied");}catch(e){prompt("Copy the signing link:",t);}ta.remove();}'
                    + 'async function ensureSignLink(){if(SIGN_URL){return SIGN_URL;}if(!OWNER_TOKEN){alert("Open this document from the owner portal to share it.");return "";}const r=await fetch("/api/documents/sign-link",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+OWNER_TOKEN},body:JSON.stringify({id:DOC_ID})});const d=await r.json();if(d.ok){SIGN_URL=d.url||SIGN_URL;return SIGN_URL;}alert(d.error||"Could not create signing link");return "";}'
                    + 'async function copySignLink(){const u=await ensureSignLink();if(u){copyText(u);}}'
                    + 'async function emailForSignature(){if(!OWNER_TOKEN){alert("Open this document from the owner portal to email it.");return;}const to=prompt("Send the signing link to which email?");if(!to){return;}const message=prompt("Optional note to include:","")||"";const r=await fetch("/api/documents/email",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+OWNER_TOKEN},body:JSON.stringify({id:DOC_ID,to:to,message:message})});const d=await r.json();if(d.ok){alert("Signing link emailed to "+to);location.reload();}else{alert(d.error||"Email failed");}}'
                    + 'if(FTYPE==="image"){document.getElementById("pages").innerHTML=\'<div class="pg"><img src="\'+FILE_URL+\'"></div>\';}'
                    + 'else if(FTYPE==="html"){'
                    + 'document.getElementById("pages").innerHTML=\'<div id="zoombar"><button onclick="htmZoom(-0.15)">−</button><button onclick="htmZoom(0.15)">+</button><button onclick="htmFit()">Fit width</button></div><div class="pg" style="overflow:hidden"><iframe id="htmf" src="\'+FILE_URL+\'"></iframe></div>\';'
                    + 'window._hz=1;window._baseW=900;'
                    + 'window.htmZoom=function(d){window._hz=Math.min(2.5,Math.max(0.2,window._hz+d));htmApply();};'
                    + 'window.htmFit=function(){const pg=document.querySelector(".pg");window._hz=Math.min(1,(pg?pg.clientWidth:360)/window._baseW);htmApply();};'
                    + 'window.htmApply=function(){const f=document.getElementById("htmf");if(!f)return;f.style.width=window._baseW+"px";f.style.transform="scale("+window._hz+")";f.style.height=(window.innerHeight*0.82/window._hz)+"px";f.parentElement.style.height=(window.innerHeight*0.82)+"px";};'
                    + 'setTimeout(htmFit,300);}'
                    + 'else if(FTYPE==="docx"){(async()=>{const host=document.getElementById("pages");try{const buf=await (await fetch(FILE_URL)).arrayBuffer();const out=await mammoth.convertToHtml({arrayBuffer:buf});host.innerHTML=\'<div class="docx-page">\'+(out.value||"No readable content.")+\'</div>\';}catch(e){host.innerHTML=\'<div class="ld">Could not render this Word file here — <a style="color:#7fb0ff;font-weight:700" href="\'+FILE_URL+\'" target="_blank" rel="noopener">open the file directly</a>.</div>\';}})();}'
                    + 'else if(FTYPE==="other"){document.getElementById("pages").innerHTML=\'<div class="ld">Preview is not available for this file type — <a style="color:#7fb0ff;font-weight:700" href="\'+FILE_URL+\'" target="_blank" rel="noopener">download/open the file</a>.</div>\';}'
                    + 'else{pdfjsLib.GlobalWorkerOptions.workerSrc="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";'
                    + '(async()=>{const host=document.getElementById("pages");try{'
                    + 'const pdf=await pdfjsLib.getDocument(FILE_URL).promise;host.innerHTML="";'
                    + 'for(let p=1;p<=pdf.numPages;p++){const el=document.createElement("div");el.className="pg";host.appendChild(el);'
                    + 'const page=await pdf.getPage(p);const scale=2.2;const vp=page.getViewport({scale});'
                    + 'const cv=document.createElement("canvas");cv.width=vp.width;cv.height=vp.height;el.appendChild(cv);'
                    + 'await page.render({canvasContext:cv.getContext("2d"),viewport:vp}).promise;}}'
                    + 'catch(e){host.innerHTML=\'<div class="ld">Could not render here — <a style="color:#7fb0ff;font-weight:700" href="\'+FILE_URL+\'" target="_blank" rel="noopener">open the file directly</a>.</div>\';}})();}'
                    + 'window._pz=1;let _pinch=0;'
                    + 'document.addEventListener("touchmove",function(e){if(e.touches.length===2){const d=Math.hypot(e.touches[0].clientX-e.touches[1].clientX,e.touches[0].clientY-e.touches[1].clientY);if(_pinch){window._pz=Math.min(4,Math.max(0.5,window._pz*(d/_pinch)));document.querySelectorAll(".pg canvas").forEach(function(c){c.style.width=(100*window._pz)+"%";});}_pinch=d;}},{passive:true});'
                    + 'document.addEventListener("touchend",function(){_pinch=0;});'
                    + '</script></body></html>')
            self.wfile.write(page.encode())
            return
        if self.path.startswith('/sign-doc/'):
            token = self.path.split('/sign-doc/')[-1].split('?')[0][:64]
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(_sign_doc_page(token).encode())
            return

        self._json(404, {'error': 'not found'})

    def do_POST(self):
        # ---- Team role guard: moderators are view-only ----
        TEAM_OPEN = ('/api/signup', '/api/login', '/api/auth/google', '/api/team/accept',
                     '/api/routes/gps', '/api/routes/stop-driver', '/api/routes/driver-run',
                     '/api/chat', '/api/agent', '/api/voice/preview', '/api/tts',
                     '/api/quo/webhook', '/api/stripe/webhook', '/api/webhooks/twilio/sms',
                     '/api/subscribe', '/api/auth/switch', '/api/setup-lead')
        if not any(self.path.startswith(p) for p in TEAM_OPEN):
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            _e, _m = _session_user(auth)
            if _m and _e:
                # re-resolve live role (owner may have changed it since login)
                _c = load_db()['customers'].get(_e) or {}
                _live = next((x for x in _c.get('team_members', []) if x.get('email') == _m.get('email')), _m)
                if not TEAM_ROLES.get(_live.get('role'), {}).get('can_edit'):
                    return self._json(403, {'error': 'Your role is view-only — ask the account owner or an admin to make changes.'})
        # ---- Snow vertical: company type, geocode, route runs, GPS ----
        if self.path == '/api/company-type':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            try:
                snow.set_company_type(c, b.get('company_type', 'tree'))
            except ValueError:
                return self._json(400, {'error': 'bad company type'})
            save_db(db)
            return self._json(200, {'ok': True, 'company_type': snow.company_type(c)})

        if self.path == '/api/sms/send':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            to = str(b.get('to') or '').strip()
            body = str(b.get('body') or '').strip()[:1600]
            if not to or not body:
                return self._json(400, {'error': 'to and body required'})
            if not (twilio_conf(c) or {}).get('sid'):
                return self._json(400, {'error': 'Twilio not connected — add it on the Integrations page'})
            ok, err = twilio_send(c, to, body)
            if not ok:
                return self._json(400, {'error': err})
            norm = '+' + ''.join(ch for ch in to if ch.isdigit())
            threads = c.setdefault('sms_threads', {})
            threads.setdefault(norm, []).append({'dir': 'out', 'body': body, 'ts': time.time(), 'read': True})
            lead = next((l for l in crm_leads(c) if (l.get('phone') or '').replace(' ', '')[-10:] == norm[-10:]), None)
            if lead:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'sms',
                    'text': '📱 SMS to client: ' + body[:200], 'by': 'owner'})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/sms/inbound':
            # Twilio webhook — no auth (Twilio posts From/Body form fields)
            b = self._body()
            frm = str(b.get('From') or b.get('from') or '')
            body = str(b.get('Body') or b.get('body') or '')[:1600]
            to_num = str(b.get('To') or b.get('to') or '')
            if not frm or not body:
                return self._json(400, {'error': 'missing fields'})
            # find which customer owns this Twilio number
            norm_to = ''.join(ch for ch in to_num if ch.isdigit())[-10:]
            target = None
            for em, c2 in db['customers'].items():
                tw = (twilio_conf(c2) or {})
                if tw.get('from') and ''.join(ch for ch in tw['from'] if ch.isdigit())[-10:] == norm_to:
                    target = c2; break
            if not target:
                return self._json(404, {'error': 'no matching account'})
            norm = '+' + ''.join(ch for ch in frm if ch.isdigit())
            threads = target.setdefault('sms_threads', {})
            threads.setdefault(norm, []).append({'dir': 'in', 'body': body, 'ts': time.time(), 'read': False})
            lead = next((l for l in crm_leads(target) if (l.get('phone') or '').replace(' ', '')[-10:] == norm[-10:]), None)
            if lead:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'sms',
                    'text': '📩 SMS from client: ' + body[:200], 'by': 'client'})
            save_db(db)
            # Twilio expects TwiML
            self.send_response(200)
            self.send_header('Content-Type', 'text/xml')
            self.end_headers()
            self.wfile.write(b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>')
            return

        if self.path == '/api/settings/save':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            allowed = {
                'profile': ['business', 'phone', 'service_area', 'hours', 'logo', 'address', 'hst_number', 'from_name', 'reply_to'],
                'defaults': ['deposit_pct', 'valid_days', 'terms', 'tax_default', 'payment_terms', 'invoice_note'],
                'notifications': ['new_lead', 'quote_approved', 'invoice_paid', 'route_done', 'channels'],
                'agent': ['agent_name', 'tone', 'always_know', 'personality', 'instructions', 'skills'],
                'branding': ['accent'],
                'tax': ['rate', 'currency', 'prices_include_tax'],
                'service_area': ['base', 'radius_km', 'travel_fee', 'fee_note'],
                'booking': ['min_notice_h', 'buffer_min', 'blackouts', 'max_per_day'],
            }
            sec = b.get('section')
            if sec not in allowed:
                return self._json(400, {'error': 'bad section'})
            data = b.get('data') or {}
            cur = c.setdefault('settings_' + sec, {})
            for k in allowed[sec]:
                if k in data:
                    cur[k] = data[k]
            save_db(db)
            return self._json(200, {'ok': True, sec: cur})

        if self.path == '/api/dashboard-template':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            t = (self._body().get('template') or 'owner')
            if t not in ('owner', 'numbers', 'dispatch'):
                return self._json(400, {'error': 'bad template'})
            c['dashboard_template'] = t
            save_db(db)
            return self._json(200, {'ok': True, 'template': t})

        if self.path == '/api/crm/route-flag':
            # toggle route_stop / priority / geocode on a lead: {id, route_stop, plow_priority}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            if 'route_stop' in b:
                lead['route_stop'] = bool(b['route_stop'])
            if 'plow_priority' in b:
                try: lead['plow_priority'] = int(b['plow_priority'])
                except (TypeError, ValueError): pass
            if 'route_order' in b:
                if b['route_order'] in (None, ''):
                    lead.pop('route_order', None)
                else:
                    try: lead['route_order'] = int(b['route_order'])
                    except (TypeError, ValueError): pass
            if 'service_min' in b:
                if b['service_min'] in (None, ''):
                    lead.pop('service_min', None)
                else:
                    try: lead['service_min'] = max(0, min(240, int(b['service_min'])))
                    except (TypeError, ValueError): pass
            if lead.get('route_stop') and not lead.get('lat') and lead.get('address'):
                g = snow.geocode(lead['address'])
                if g:
                    lead['lat'], lead['lon'] = g['lat'], g['lon']
                else:
                    save_db(db)
                    return self._json(200, {'ok': True, 'lead': lead, 'geocode': 'failed'})
            save_db(db)
            return self._json(200, {'ok': True, 'lead': lead, 'geocode': 'ok' if lead.get('lat') else 'skipped'})

        if self.path == '/api/routes/reorder':
            # {order: [lead_id, ...]} — pins manual order within each priority tier.
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            order = b.get('order') or []
            leads = _crm_leads(c)
            pos = {lid: i + 1 for i, lid in enumerate(order)}
            for l in leads:
                if l.get('id') in pos:
                    l['route_order'] = pos[l['id']]
                elif l.get('route_stop'):
                    l.pop('route_order', None)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/routes/optimize':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            return self._json(200, snow.optimize(c, crm_leads(c)))

        if self.path == '/api/routes/start':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            r = snow.start_run(c, crm_leads(c))
            if r.get('ok'):
                try:
                    for l in crm_leads(c):
                        if l.get('route_stop'):
                            client_notify(c, (l.get('email') or '').lower().strip(), 'route',
                                          'Plow route started — live tracking is on')
                except Exception:
                    pass
                save_db(db)
            return self._json(200 if r.get('ok') else 400, r)

        if self.path == '/api/routes/stop':
            # {lead_id, status: done|skipped}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            r = snow.mark_stop(c, crm_leads(c), b.get('lead_id'), b.get('status', 'done'))
            fire_route_notifications(c, crm_leads(c))
            if r.get('ok'):
                save_db(db)
            return self._json(200 if r.get('ok') else 400, r)

        if self.path == '/api/routes/cancel':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            snow.archive_run(c, crm_leads(c), 'cancelled')
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/routes/setup':
            # {depot_address?, service_min?} — depot geocoded once, stored on the account
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            if 'service_min' in b:
                try:
                    c['route_service_min'] = max(1, min(240, int(b['service_min'])))
                except (TypeError, ValueError):
                    pass
            if 'depot_address' in b:
                addr = str(b.get('depot_address') or '').strip()
                if not addr:
                    c.pop('depot_address', None); c.pop('depot_lat', None); c.pop('depot_lon', None)
                else:
                    try:
                        g = snow.geocode(addr)
                    except Exception:
                        g = None
                    if not g:
                        save_db(db)
                        return self._json(200, {'ok': True, 'geocode': 'failed',
                                                'service_min': int(c.get('route_service_min') or snow.DEFAULT_SERVICE_MIN)})
                    c['depot_address'] = addr[:200]
                    c['depot_lat'], c['depot_lon'] = g['lat'], g['lon']
            save_db(db)
            return self._json(200, {'ok': True, 'geocode': 'ok',
                                    'depot_address': c.get('depot_address', ''),
                                    'service_min': int(c.get('route_service_min') or snow.DEFAULT_SERVICE_MIN)})

        # ---------- Referral program (owner) ----------
        if self.path == '/api/referrals/payout':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            r = _referral(c)
            r['payout_email'] = str(b.get('payout_email') or '').strip()[:120]
            if b.get('payout_method') in ('etransfer', 'paypal', 'stripe'):
                r['payout_method'] = b['payout_method']
            save_db(db)
            return self._json(200, {'ok': True})

        # ---------- Referral program (admin) ----------
        if self.path == '/api/admin/referrals/commission':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            db = load_db()
            c = db['customers'].get((b.get('owner') or '').lower().strip())
            if not c:
                return self._json(404, {'error': 'owner not found'})
            try:
                amount = round(float(b.get('amount') or 0), 2)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return self._json(400, {'error': 'amount must be positive'})
            entry = {'id': secrets.token_hex(5), 'ts': int(time.time()),
                     'period': str(b.get('period') or '')[:40], 'amount': amount,
                     'status': 'pending', 'note': str(b.get('note') or '')[:200], 'paid_ts': 0}
            _referral(c)['commissions'].append(entry)
            log_activity(db, c['email'], 'referral',
                         f"Commission logged for {c.get('name', c['email'])}: ${amount:,.2f} ({entry['period'] or 'manual'})", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'commission': entry})

        if self.path == '/api/admin/referrals/paid':
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            db = load_db()
            c = db['customers'].get((b.get('owner') or '').lower().strip())
            if not c:
                return self._json(404, {'error': 'owner not found'})
            entry = next((x for x in _referral(c)['commissions'] if x.get('id') == b.get('id')), None)
            if not entry:
                return self._json(404, {'error': 'commission not found'})
            entry['status'] = 'paid'
            entry['paid_ts'] = int(time.time())
            if b.get('note'):
                entry['note'] = str(b['note'])[:200]
            log_activity(db, c['email'], 'referral',
                         f"Commission paid to {c.get('name', c['email'])}: ${entry['amount']:,.2f}", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'commission': entry})

        if self.path == '/api/routes/stop-driver':
            # driver marks a stop done/skipped from the tracker page: {token, lead_id, status, photo?}
            b = self._body()
            db = load_db()
            c = snow.driver_auth(db, b.get('token', ''))
            if not c:
                return self._json(401, {'error': 'bad driver token'})
            leads = crm_leads(c)
            r = snow.mark_stop(c, leads, b.get('lead_id'), b.get('status', 'done'))
            photo_url = ''
            if b.get('photo') and r.get('ok') and b.get('status', 'done') == 'done':
                lead = next((l for l in leads if l.get('id') == b.get('lead_id')), None)
                if lead is not None:
                    try:
                        photo_url = _save_photo(c, lead, b.get('photo'),
                            caption='✅ Proof of service — ' + time.strftime('%b %-d, %Y'), by='driver')
                    except Exception as e:
                        print('[proof-photo]', e)
            fire_route_notifications(c, leads)
            save_db(db)
            if photo_url:
                r['photo_url'] = photo_url
            return self._json(200 if r.get('ok') else 400, r)

        if self.path == '/api/estimates/duplicate':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            e = next((x for x in c.get('estimates', []) if x.get('id') == b.get('id')), None)
            if not e:
                return self._json(404, {'error': 'estimate not found'})
            c['est_counter'] = c.get('est_counter', 0) + 1
            copy = {'id': secrets.token_hex(5), 'number': f"EST-{c['est_counter']:03d}", 'created': time.time(),
                    'status': 'draft', 'lead_id': e.get('lead_id', ''),
                    'client': e.get('client', ''), 'address': e.get('address', ''),
                    'notes': e.get('notes', ''), 'line_items': list(e.get('line_items') or [])}
            copy['total'] = round(sum((i.get('price') or 0) for i in copy['line_items']), 2)
            c.setdefault('estimates', []).insert(0, copy)
            save_db(db)
            return self._json(200, {'ok': True, 'estimate': copy})

        if self.path == '/api/estimate/templates':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            name = str(b.get('name') or '').strip()[:80]
            items = [{'desc': str(i.get('desc') or '')[:200], 'price': float(i.get('price') or 0)}
                     for i in (b.get('line_items') or []) if i.get('desc')][:20]
            if not name or not items:
                return self._json(400, {'error': 'name + line items required'})
            c.setdefault('estimate_templates', []).append({
                'id': secrets.token_hex(5), 'name': name, 'line_items': items,
                'notes': str(b.get('notes') or '')[:300], 'created': time.time()})
            save_db(db)
            return self._json(200, {'ok': True, 'templates': c['estimate_templates']})

        if self.path == '/api/estimate/templates/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            c['estimate_templates'] = [t for t in c.get('estimate_templates', []) if t.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/contracts':
            # seasonal contract CRUD + roll-up (invoicing itself stays manual)
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body() if self.command == 'POST' else {}
            action = b.get('action') or 'list'
            cts = _contracts(c)
            if action == 'list':
                cid = self._qs().get('client_id', [''])[0] or b.get('client_id') or ''
                out = [dict(ct, **_contract_rollup(c, ct)) for ct in cts
                       if ct.get('status') != 'ended' and (not cid or ct.get('client_id') == cid)]
                return self._json(200, {'ok': True, 'contracts': out})
            if action == 'create':
                client_id = str(b.get('client_id') or '')
                if not client_id:
                    return self._json(400, {'error': 'client_id required'})
                ctype = b.get('type') if b.get('type') in ('monthly', 'upfront', 'per_visit') else 'monthly'
                try:
                    total = round(float(b.get('seasonal_total') or 0), 2)
                except (TypeError, ValueError):
                    total = 0
                if total <= 0:
                    return self._json(400, {'error': 'Seasonal total must be more than $0'})
                import uuid as _uuid
                ct = {'id': _uuid.uuid4().hex[:12], 'client_id': client_id,
                      'property_id': str(b.get('property_id') or ''),
                      'season': str(b.get('season') or '')[:20], 'type': ctype,
                      'seasonal_total': total, 'hst': bool(b.get('hst')),
                      'monthly_rate': round(float(b.get('monthly_rate') or 0), 2) if b.get('monthly_rate') else 0,
                      'per_visit_rate': round(float(b.get('per_visit_rate') or 0), 2) if b.get('per_visit_rate') else 0,
                      'start': str(b.get('start') or '')[:10], 'end': str(b.get('end') or '')[:10],
                      'package': str(b.get('package') or '')[:40],
                      'trigger_cm': int(b.get('trigger_cm') or 0) if str(b.get('trigger_cm') or '') in ('3', '5') else 0,
                      'status': 'active', 'created': time.time()}
                cts.append(ct)
                save_db(db)
                return self._json(200, {'ok': True, 'contract': dict(ct, **_contract_rollup(c, ct))})
            if action == 'end':
                ct = next((x for x in cts if x.get('id') == b.get('id')), None)
                if not ct:
                    return self._json(404, {'error': 'contract not found'})
                ct['status'] = 'ended'
                save_db(db)
                return self._json(200, {'ok': True})
            if action == 'delete':
                before = len(cts)
                c['contracts'] = [x for x in cts if x.get('id') != b.get('id')]
                if len(c['contracts']) == before:
                    return self._json(404, {'error': 'contract not found'})
                save_db(db)
                return self._json(200, {'ok': True})
            return self._json(400, {'error': 'unknown action'})

        if self.path == '/api/client/invoices/payment':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((i for i in _client_invoices(c) if i.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            try:
                amt = round(float(b.get('amount') or 0), 2)
            except (TypeError, ValueError):
                return self._json(400, {'error': 'bad amount'})
            if amt <= 0:
                return self._json(400, {'error': 'amount must be positive'})
            inv.setdefault('payments', []).append({'ts': time.time(), 'amount': amt,
                'method': str(b.get('method') or 'e-transfer')[:40]})
            paid = sum(p['amount'] for p in inv['payments'])
            if paid >= (inv.get('total') or 0) - 0.005:
                inv['status'] = 'paid'
            c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
                'desc': f"Payment ${amt:,.2f} on {inv['number']}"})
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': inv, 'balance': round((inv.get('total') or 0) - paid, 2)})

        if self.path == '/api/invoice/logo':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            data_url = str(b.get('logo') or '')
            m = re.match(r'^data:image/(png|jpeg|jpg|webp);base64,(.+)$', data_url, re.S)
            if not m:
                return self._json(400, {'error': 'logo must be png/jpg/webp'})
            import base64 as _b, uuid as _u, os as _os
            ext = {'jpeg': 'jpg'}.get(m.group(1), m.group(1))
            raw = _b.b64decode(m.group(2))
            if len(raw) > 4 * 1024 * 1024:
                return self._json(400, {'error': 'logo too large (4MB max)'})
            owner = re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
            d = BASE / 'uploads' / owner
            _os.makedirs(d, exist_ok=True)
            fname = 'logo_invoice.' + ext
            (d / fname).write_bytes(raw)
            s = c.setdefault('invoice_settings', {})
            s['logo_url'] = '/uploads/' + owner + '/' + fname
            save_db(db)
            return self._json(200, {'ok': True, 'logo_url': s['logo_url']})

        if self.path == '/api/invoice/settings':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            s = c.setdefault('invoice_settings', {})
            if b.get('template') in ('classic', 'modern', 'bold', 'kw'):
                s['template'] = b['template']
            if 'accent' in b:
                ac = str(b.get('accent') or '')
                if re.fullmatch(r'#[0-9a-fA-F]{6}', ac):
                    s['accent'] = ac
            if 'logo_url' in b:
                s['logo_url'] = str(b.get('logo_url') or '')[:300]
            if 'footer' in b:
                s['footer'] = str(b.get('footer') or '')[:300]
            if 'payment_methods' in b:
                # one method per line, e.g. "E-transfer: pay@kwsnowco.ca" — shown in the client portal
                lines = [ln.strip()[:120] for ln in str(b.get('payment_methods') or '').splitlines() if ln.strip()]
                s['payment_methods'] = lines[:6]
            if 'auto_reminders' in b:
                s['auto_reminders'] = bool(b.get('auto_reminders'))
            save_db(db)
            return self._json(200, {'ok': True, 'settings': s})

        if self.path == '/api/integrations/google/disconnect':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            integ = c.setdefault('integrations', {})
            for svc in ('gmail', 'googlecal'):
                if integ.get(svc, {}).get('via') == 'oauth':
                    keep = {k: v for k, v in integ[svc].items() if k in ('email', 'app_password')}
                    if keep:
                        integ[svc] = keep
                    else:
                        integ.pop(svc, None)
            save_db(db)
            log_activity(db, c['email'], 'admin', 'Google disconnected', save=True)
            return self._json(200, {'ok': True})

        if self.path == '/api/integrations/twilio':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            sid, tok, frm = (b.get('sid') or '').strip(), (b.get('token') or '').strip(), (b.get('from') or '').strip()
            if not sid or not frm:
                return self._json(400, {'error': 'Account SID and phone number are required'})
            tw = c.setdefault('twilio', {})
            tw['sid'] = sid
            if tok:  # blank token = keep existing
                tw['token'] = tok
            if not tw.get('token'):
                return self._json(400, {'error': 'Auth token required on first save'})
            tw['from'] = frm
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/integrations/twilio/test':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            to = (b.get('to') or '').strip()
            if not to:
                return self._json(400, {'error': 'missing destination number'})
            ok, err = twilio_send(c, to, f"ANOSagent test SMS - Twilio is connected for {c.get('business', 'your company')}.")
            return self._json(200 if ok else 400, {'ok': ok, 'error': err})

        if self.path == '/api/telephony/provision':
            # create (or return) this customer's Twilio subaccount under the master account
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not (TWILIO_MASTER_SID and TWILIO_MASTER_TOKEN):
                return self._json(400, {'error': 'Platform phone numbers are not enabled yet'})
            sub = _twilio_sub(c)
            if sub.get('sid') and sub.get('token'):
                return self._json(200, {'ok': True, 'existing': True})
            ok, r = twilio_api(TWILIO_MASTER_SID, TWILIO_MASTER_TOKEN, 'POST', '/Accounts.json',
                               {'FriendlyName': 'ANOS-' + (c.get('email') or '')[:60]})
            if not ok:
                return self._json(400, {'error': r})
            sub['sid'] = r.get('sid')
            sub['token'] = r.get('auth_token')
            sub['created'] = time.time()
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/telephony/numbers/buy':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            num = str(b.get('phone_number') or '')
            num = '+' + re.sub(r'\D', '', num) if re.sub(r'\D', '', num) else ''
            if len(num) < 10:
                return self._json(400, {'error': 'Invalid phone number'})
            sub = _twilio_sub(c)
            if not (sub.get('sid') and sub.get('token')):
                return self._json(400, {'error': 'Set up your phone account first'})
            w = _sms_wallet(c)
            if (w.get('balance') or 0) < NUMBER_ACTIVATION_CAD:
                return self._json(400, {'error': f'Number activation costs ${NUMBER_ACTIVATION_CAD:.2f} — top up your SMS balance first',
                                        'need_topup': True})
            ok, r = twilio_api(sub['sid'], sub['token'], 'POST',
                               f"/Accounts/{sub['sid']}/IncomingPhoneNumbers.json",
                               {'PhoneNumber': num,
                                'SmsUrl': PORTAL_BASE + '/api/webhooks/twilio/sms'})
            if not ok:
                return self._json(400, {'error': r})
            sub['number'] = r.get('phone_number') or num
            _wallet_charge(c, NUMBER_ACTIVATION_CAD, 'Number activation ' + sub['number'])
            save_db(db)
            return self._json(200, {'ok': True, 'number': sub['number'],
                                    'balance': round(_sms_wallet(c).get('balance') or 0, 2)})

        if self.path == '/api/telephony/topup':
            # Stripe Checkout session for SMS wallet credit
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not STRIPE_KEY:
                return self._json(400, {'error': 'Payments not configured'})
            b = self._body()
            try:
                amt = float(b.get('amount') or 0)
            except (TypeError, ValueError):
                amt = 0
            if amt not in (10, 25, 50):
                return self._json(400, {'error': 'Top-up amounts are $10, $25 or $50'})
            try:
                sess = stripe_request('POST', '/checkout/sessions', {
                    'mode': 'payment',
                    'success_url': PORTAL_BASE + '/?topup=done',
                    'cancel_url': PORTAL_BASE + '/?topup=cancel',
                    'customer_email': c.get('email'),
                    'client_reference_id': c.get('email'),
                    'metadata[type]': 'sms_topup',
                    'metadata[anos_owner]': c.get('email'),
                    'line_items[0][price_data][currency]': 'cad',
                    'line_items[0][price_data][unit_amount]': int(amt * 100),
                    'line_items[0][price_data][product_data][name]': f'SMS credit top-up (${amt:.0f} CAD)',
                    'line_items[0][quantity]': 1,
                })
                return self._json(200, {'ok': True, 'url': sess['url']})
            except Exception as e:
                return self._json(500, {'ok': False, 'error': str(e)})

        if self.path == '/api/webhooks/twilio/sms':
            # Twilio inbound SMS (form POST). Public — matched to a customer by their number.
            b = self._body()
            to_num = re.sub(r'\D', '', b.get('To', ''))[-10:]
            from_num = re.sub(r'\D', '', b.get('From', ''))[-10:]
            text = (b.get('Body') or '')[:500]
            db = load_db()
            for em, c in (db.get('customers') or {}).items():
                nums = [(c.get('twilio') or {}).get('from') or '', (c.get('twilio_sub') or {}).get('number') or '']
                if to_num and any(re.sub(r'\D', '', n)[-10:] == to_num for n in nums if n):
                    lead = next((l for l in _crm_leads(c)
                                 if from_num and re.sub(r'\D', '', l.get('phone') or '')[-10:] == from_num), None)
                    if lead:
                        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'sms',
                            'by': 'client', 'text': f'📥 Text received: {text}'})
                        lead['updated'] = time.time()
                    c.setdefault('activity', []).append({'type': 'sms', 'ts': time.time(),
                        'desc': f"Text from {b.get('From')}: {text[:80]}"})
                    save_db(db)
                    break
            self.send_response(200)
            self.send_header('Content-Type', 'text/xml')
            self.end_headers()
            self.wfile.write(b'<Response></Response>')
            return

        # ---- Quo (OpenPhone) ----
        if self.path == '/api/integrations/elevenlabs':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'owner only'})
            b = self._body()
            key = (b.get('api_key') or '').strip()
            if not key:
                return self._json(400, {'error': 'Paste your ElevenLabs API key'})
            # verify + count voices
            c.setdefault('integrations', {})['elevenlabs'] = {'api_key': key, 'connected_at': int(time.time())}
            try:
                vs = el_list_voices(c)
            except Exception:
                c['integrations'].pop('elevenlabs', None)
                return self._json(400, {'error': 'Key rejected by ElevenLabs — check it and try again'})
            save_db(db)
            return self._json(200, {'ok': True, 'voice_count': len(vs)})

        if self.path == '/api/integrations/elevenlabs/disconnect':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'owner only'})
            c.get('integrations', {}).pop('elevenlabs', None)
            # fall back to a free voice if an EL voice was selected
            if (c.get('voice') or '').startswith('el:'):
                c['voice'] = 'sonia'
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/integrations/quo':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            key = (b.get('api_key') or '').strip()
            if not key:
                return self._json(400, {'error': 'Paste your Quo API key first'})
            ok, info = quo.connect(c, key, PORTAL_BASE)
            if not ok:
                return self._json(400, {'ok': False, 'error': info})
            save_db(db)
            return self._json(200, {'ok': True, 'number': info.get('number')})

        if self.path == '/api/integrations/quo/disconnect':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            quo.disconnect(c)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/integrations/quo/test':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            to = (b.get('to') or '').strip()
            if not to:
                return self._json(400, {'error': 'missing destination number'})
            ok, info = quo.send_sms(c, to, f"ANOSagent test — Quo is connected for {c.get('business', 'your company')}.")
            return self._json(200 if ok else 400, {'ok': ok, 'error': None if ok else info})

        if self.path == '/api/quo/webhook':
            # Quo inbound events (messages + calls). Public endpoint — matched to a
            # customer by their registered phoneNumberId; unclaimed events are ignored.
            try:
                event = self._body()
            except Exception:
                return self._json(400, {'error': 'bad payload'})
            db = load_db()
            raw = getattr(self, '_raw_body', b'')
            claimed = quo.handle_webhook(db, save_db, raw, event, self.headers)
            return self._json(200, {'received': True, 'claimed': claimed})

        if self.path == '/api/quo/send':
            # Send an SMS from the customer's Quo line to a lead; log it on the timeline
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            to, text = (b.get('to') or '').strip(), (b.get('text') or '').strip()
            if not to or not text:
                return self._json(400, {'error': 'to and text required'})
            ok, info = quo.send_sms(c, to, text)
            if not ok:
                return self._json(400, {'ok': False, 'error': info})
            lead = quo.log_to_lead(c, to, {'ts': int(time.time()), 'type': 'sms', 'by': 'owner',
                                           'text': f'📤 Text sent: {text[:400]}'})
            save_db(db)
            return self._json(200, {'ok': True, 'lead_matched': bool(lead)})

        if self.path == '/api/quo/thread':
            # Fetch live SMS thread with a phone number (lead detail view)
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            msgs = quo.fetch_history(c, b.get('phone') or '', 50)
            out = [{'dir': m.get('direction'), 'text': m.get('text') or m.get('content') or '',
                    'at': m.get('createdAt'), 'status': m.get('status'),
                    'media': [mm.get('url') for mm in (m.get('media') or []) if isinstance(mm, dict) and mm.get('url')]}
                   for m in msgs]
            return self._json(200, {'ok': True, 'messages': out})

        if self.path == '/api/routes/gps':
            # driver token auth: {token, lat, lon}
            b = self._body()
            db = load_db()
            c = snow.driver_auth(db, b.get('token', ''))
            if not c:
                return self._json(401, {'error': 'bad driver token'})
            try:
                lat, lon = float(b['lat']), float(b['lon'])
            except (KeyError, TypeError, ValueError):
                return self._json(400, {'error': 'bad lat/lon'})
            r = snow.gps_ping(c, crm_leads(c), lat, lon)
            save_db(db)
            return self._json(200, r)

        if self.path == '/api/crm/stage':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lid, stage = b.get('id', ''), b.get('stage', '')
            if stage not in _crm_stages(c):
                return self._json(400, {'error': 'invalid stage'})
            lead = next((l for l in _crm_leads(c) if l.get('id') == lid), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            old_stage = lead.get('stage')
            lead['stage'] = stage
            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'stage',
                'text': f"Stage changed: {old_stage or '—'} → {stage}", 'by': 'owner'})
            c.setdefault('activity', []).append({'type': 'crm', 'ts': time.time(),
                'desc': f"Moved {lead.get('title') or lead.get('client') or 'a deal'} to {stage}"})
            save_db(db)
            return self._json(200, {'ok': True, 'stats': _crm_stats(_crm_leads(c), _crm_stages(c))})

        if self.path == '/api/crm/stages':
            # owner edits the pipeline itself: add / rename / delete / reorder stages
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            stages = _crm_stages(c)
            act = b.get('action')
            name = str(b.get('name') or '').strip()[:40]
            old = str(b.get('old') or '')
            if act in ('add', 'rename'):
                if not name:
                    return self._json(400, {'error': 'name required'})
                if any(s.lower() == name.lower() for s in stages):
                    return self._json(400, {'error': 'a stage with that name already exists'})
            if act == 'add':
                # land new columns in the active part of the board, before the terminal stages
                idx = next((i for i, s in enumerate(stages) if s in ('Job Completed', 'Lost Lead')), len(stages))
                stages.insert(idx, name)
            elif act == 'rename':
                if old not in stages:
                    return self._json(404, {'error': 'stage not found'})
                stages[stages.index(old)] = name
                for l in _crm_leads(c):
                    if l.get('stage') == old:
                        l['stage'] = name
            elif act == 'delete':
                if old not in stages:
                    return self._json(404, {'error': 'stage not found'})
                if len(stages) <= 2:
                    return self._json(400, {'error': 'pipeline needs at least 2 stages'})
                in_use = [l for l in _crm_leads(c) if l.get('stage') == old]
                if in_use:
                    move_to = str(b.get('move_to') or '')
                    if move_to not in stages or move_to == old:
                        return self._json(400, {'error': f'{len(in_use)} lead(s) sit in this stage — pick where to move them',
                                                'in_use': len(in_use)})
                    for l in in_use:
                        l['stage'] = move_to
                stages.remove(old)
            elif act == 'reorder':
                order = [str(s)[:40] for s in (b.get('order') or [])]
                if len(order) != len(stages) or sorted(order) != sorted(stages):
                    return self._json(400, {'error': 'order must list every stage exactly once'})
                c['crm_stages'] = order
                stages = c['crm_stages']
            else:
                return self._json(400, {'error': 'unknown action'})
            save_db(db)
            return self._json(200, {'ok': True, 'stages': stages,
                                    'stats': _crm_stats(_crm_leads(c), stages)})

        if self.path == '/api/crm/note':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            text = (b.get('text') or '').strip()
            if not text:
                return self._json(400, {'error': 'empty note'})
            lead.setdefault('activities', []).append({'ts': time.time(), 'type': b.get('type', 'note'),
                'text': text, 'by': 'owner'})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/client/notifications':
            db, c = _find_customer_by_client_token(_get_bearer(self))
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            tok = _get_bearer(self)
            portal = _find_client_by_token_multi(c, tok) or _client_portal(c)
            b = self._body()
            entries = b.get('notify_phones') or []
            clean = []
            for e in entries[:20]:
                ph = ''.join(ch for ch in str(e.get('phone', '')) if ch.isdigit() or ch == '+')
                if len(ph) < 10:
                    continue
                pids = [str(x) for x in (e.get('property_ids') or [])][:50]
                if pids:
                    clean.append({'phone': ph, 'property_ids': pids,
                                  'name': str(e.get('name') or '')[:80]})
            portal['notify_phones'] = clean
            save_db(db)
            return self._json(200, {'ok': True, 'notify_phones': clean})

        if self.path == '/api/client/profile':
            db, c = _find_customer_by_client_token(_get_bearer(self))
            if not c:
                return self._json(401, {'error': 'invalid client token'})
            b = self._body()
            p = _client_profile(c)
            for f in ('phone', 'address', 'notes'):
                if f in b:
                    p[f] = str(b[f])[:500]
            if 'name' in b and b['name'].strip():
                _client_portal(c)['client_name'] = str(b['name']).strip()[:120]
            save_db(db)
            return self._json(200, {'ok': True, 'profile': p})

        if self.path == '/api/client/invoices':
            # owner adds a billed product/item to their client's account
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            import uuid
            items = []
            for it in (b.get('items') or [])[:30]:
                try:
                    price = float(it.get('price') or 0)
                    qty = int(it.get('qty') or 1)
                except (TypeError, ValueError):
                    continue
                items.append({'desc': str(it.get('desc') or '')[:200], 'price': price, 'qty': max(1, qty)})
            if not items:
                return self._json(400, {'error': 'no line items'})
            subtotal = round(sum(i['price'] * i['qty'] for i in items), 2)
            tax_pct = float(b.get('tax_pct') or 0)
            tax = round(subtotal * tax_pct / 100, 2)
            due_days = int(b.get('due_days') or 14)
            inv = {'id': uuid.uuid4().hex[:10],
                   'number': f"INV-{c.get('inv_counter', 0) + 1:03d}",
                   'created': time.time(), 'status': 'sent',
                   'note': str(b.get('note') or '')[:500],
                   'estimate_id': str(b.get('estimate_id') or ''),
                   'client_key': str(b.get('client_key') or ''),
                   'contract_id': str(b.get('contract_id') or '')[:24],
                   'due_ts': time.time() + due_days * 86400,
                   'payments': [],
                   'items': items, 'subtotal': subtotal,
                   'tax_pct': tax_pct, 'tax': tax,
                   'total': round(subtotal + tax, 2),
                   'reminders': 0, 'last_reminded': 0}
            if b.get('deposit'):
                try:
                    dep = round(float(b.get('deposit') or 0), 2)
                    if 0 < dep < inv['total']:
                        inv['deposit'] = dep
                except (TypeError, ValueError):
                    pass
            rec_in = b.get('recurring') or {}
            if rec_in.get('interval') in ('week', 'month'):
                interval = rec_in.get('interval')
                try:
                    months = int(rec_in.get('months') or 1)
                except (TypeError, ValueError):
                    months = 1
                months = max(1, min(36, months))
                if interval == 'week':
                    try:
                        day = int(rec_in.get('day') or 0)
                    except (TypeError, ValueError):
                        day = 0
                    day = max(0, min(6, day))
                    first_ts = _next_weekly_ts(day, time.time() - 1)
                else:
                    try:
                        day = int(rec_in.get('day') or 1)
                    except (TypeError, ValueError):
                        day = 1
                    day = max(1, min(31, day))
                    first_ts = _next_monthly_ts(day, time.time() - 1)
                inv['recurring'] = {'interval': interval, 'day': day, 'months': months,
                                    'due_days': due_days, 'next_ts': first_ts,
                                    'until_ts': _add_months_ts(time.time(), months),
                                    'sent_count': 0}
            # scheduled send: invoice hides from the client until X days before its due date
            try:
                sbd = int(b.get('send_before_due_days') or 0)
            except (TypeError, ValueError):
                sbd = 0
            if 0 < sbd <= 60:
                inv['status'] = 'scheduled'
                inv['send_ts'] = inv['due_ts'] - sbd * 86400
            c['inv_counter'] = c.get('inv_counter', 0) + 1
            _client_invoices(c).append(inv)
            c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
                'desc': f"Billed client {inv['number']} — ${inv['total']:,.0f}"})
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': inv})

        if self.path == '/api/client/invoices/update':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((i for i in _client_invoices(c) if i.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            if b.get('status') in ('sent', 'paid', 'void'):
                inv['status'] = b['status']
            if 'note' in b:
                inv['note'] = str(b.get('note') or '')[:500]
            if b.get('due_days'):
                inv['due_ts'] = inv.get('created', time.time()) + int(b['due_days']) * 86400
            if b.get("due_ts"):
                try:
                    inv["due_ts"] = float(b["due_ts"])
                except (TypeError, ValueError):
                    pass
            if "items" in b:
                items = []
                for it in (b.get("items") or [])[:30]:
                    try:
                        price = float(it.get("price") or 0)
                        qty = int(it.get("qty") or 1)
                    except (TypeError, ValueError):
                        continue
                    items.append({"desc": str(it.get("desc") or "")[:200], "price": price, "qty": max(1, qty)})
                if not items:
                    return self._json(400, {"error": "no line items"})
                inv["items"] = items
                inv["subtotal"] = round(sum(i2["price"] * i2["qty"] for i2 in items), 2)
            if "tax_pct" in b:
                try:
                    inv["tax_pct"] = float(b.get("tax_pct") or 0)
                except (TypeError, ValueError):
                    pass
            if "deposit" in b:
                try:
                    dep = round(float(b.get("deposit") or 0), 2)
                    if 0 < dep:
                        inv["deposit"] = dep
                    else:
                        inv.pop("deposit", None)
                except (TypeError, ValueError):
                    pass
            if "items" in b or "tax_pct" in b:
                inv["tax"] = round(inv.get("subtotal", 0) * inv.get("tax_pct", 0) / 100, 2)
                inv["total"] = round(inv.get("subtotal", 0) + inv.get("tax", 0), 2)
                if inv.get("payments") and invoice_pdf.inv_balance(inv)[0] <= 0 and inv.get("status") == "sent":
                    inv["status"] = "paid"
                elif inv.get("status") == "paid" and invoice_pdf.inv_balance(inv)[0] > 0:
                    inv["status"] = "sent"
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': inv})

        if self.path == '/api/client/invoices/paylink':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((i for i in _client_invoices(c) if i.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            if inv.get('status') == 'void':
                return self._json(400, {'error': 'invoice is void'})
            if _invoice_balance(inv) <= 0:
                return self._json(400, {'error': 'invoice is already paid'})
            st = connect.acct_status(stripe_request, c) if STRIPE_KEY else {'connected': False}
            if not (st.get('connected') and st.get('charges_enabled')):
                return self._json(400, {'error': 'Connect Stripe first — Billing → Subscriptions → Connect Stripe'})
            if not inv.get('pay_token'):
                inv['pay_token'] = secrets.token_urlsafe(12)
                save_db(db)
            return self._json(200, {'ok': True, 'url': _public_base(c) + '/invoice-pay/' + inv['pay_token']})

        if self.path == '/api/client/invoices/send':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((i for i in _client_invoices(c) if i.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            cp = _client_portal(c)
            lead = next((l for l in _crm_leads(c) if l.get('id') == inv.get('client_key')), None)
            ce = (((lead or {}).get('email')) or (cp.get('client_email') or '')).strip()
            if not ce:
                return self._json(400, {'error': 'client has no email on file'})
            cname = ((lead or {}).get('client') or cp.get('client_name') or 'there')
            biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'Your contractor')
            base = _public_base(c)
            due = ''
            try:
                due = time.strftime('%b %-d, %Y', time.localtime(inv.get('due_ts', 0)))
            except Exception:
                due = ''
            body_txt = (f"Hi {cname},\n\n"
                        f"{biz} sent you invoice {inv.get('number', '')} for ${inv.get('total', 0):,.2f}"
                        + (f" — due {due}." if due else ".")
                        + f"\n\nYou can view and pay it in your client portal:\n{base}/client\n\n"
                        + "Reply to this email or call us with any questions.\n\n"
                        + f"— {biz}")
            ok, err = send_client_email(c, ce, f"Invoice {inv.get('number', '')} from {biz}", body_txt)
            if not ok:
                return self._json(502, {'error': err or 'send failed'})
            inv['status'] = 'sent'
            inv['sent_ts'] = time.time()
            inv['sent_to'] = ce
            c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
                'desc': f"Sent invoice {inv.get('number', '')} to {ce}"})
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': inv})

        if self.path == '/api/client/invoices/remind':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((i for i in _client_invoices(c) if i.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            cp = _client_portal(c)
            ce = cp.get('client_email', '')
            if not ce:
                return self._json(400, {'error': 'client has no portal email'})
            days = int((time.time() - inv.get('created', time.time())) / 86400)
            body_txt = (f"Hi {cp.get('client_name', 'there')},\n\n"
                        f"A friendly reminder that invoice {inv['number']} for ${inv['total']:,.2f} "
                        f"({', '.join(i['desc'] for i in inv.get('items', [])[:3])}) is outstanding"
                        + (f" ({days} days)." if days > 0 else ".")
                        + "\n\nYou can view it anytime in your client portal. "
                          "Reply to this email or call us with any questions.\n\n"
                        f"— {c.get('business', 'Your contractor')}")
            sent_via = []
            errs = []
            try:
                send_client_email(c, ce, f"Reminder: invoice {inv['number']} — balance due", body_txt)
                sent_via.append('email')
            except Exception as e:
                errs.append('email: ' + str(e)[:80])
            ph = ''
            for l in crm_leads(c):
                if (l.get('email') or '').lower().strip() == ce.lower():
                    ph = l.get('phone', ''); break
            if ph and (twilio_conf(c) or {}).get('sid'):
                ok, err = twilio_send(c, ph, f"{c.get('business','')}: reminder — invoice {inv['number']} (${inv['total']:,.2f}) is outstanding. View it in your client portal. Questions? Reply here.")
                if ok: sent_via.append('sms')
                else: errs.append('sms: ' + str(err)[:60])
            if not sent_via:
                return self._json(400, {'error': 'could not send — ' + '; '.join(errs)})
            inv['reminders'] = inv.get('reminders', 0) + 1
            inv['last_reminded'] = time.time()
            save_db(db)
            return self._json(200, {'ok': True, 'via': sent_via, 'errors': errs})

        if self.path == '/api/expenses':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if self.command == 'GET':
                exps = sorted(_expenses(c), key=lambda x: x.get('date', ''), reverse=True)
                out = []
                for e in exps:
                    d = dict(e)
                    d['has_receipt'] = bool(e.get('receipt'))
                    d.pop('receipt', None)
                    out.append(d)
                return self._json(200, {'expenses': out})
            b = self._body()
            import uuid as _ue
            try:
                amount = round(float(b.get('amount') or 0), 2)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return self._json(400, {'error': 'amount must be positive'})
            e = {'id': _ue.uuid4().hex[:10],
                 'date': str(b.get('date') or time.strftime('%Y-%m-%d'))[:10],
                 'vendor': str(b.get('vendor') or '')[:150],
                 'desc': str(b.get('desc') or '')[:300],
                 'category': str(b.get('category') or 'other')[:40],
                 'currency': b.get('currency') if b.get('currency') in ('CAD', 'USD') else 'CAD',
                 'amount': amount,
                 'tax_pct': float(b.get('tax_pct') or 0),
                 'tax_label': str(b.get('tax_label') or '')[:40],
                 'created': time.time()}
            _expenses(c).append(e)
            save_db(db)
            return self._json(200, {'ok': True, 'expense': e})

        if self.path == '/api/expenses/update':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            e = _expense_by_id(c, str(b.get('id') or ''))
            if not e:
                return self._json(404, {'error': 'expense not found'})
            for f in ('date', 'vendor', 'desc', 'category', 'tax_label'):
                if f in b:
                    e[f] = str(b[f])[:300]
            if 'amount' in b:
                try: e['amount'] = round(float(b['amount']), 2)
                except (TypeError, ValueError): pass
            if 'tax_pct' in b:
                try: e['tax_pct'] = float(b['tax_pct'])
                except (TypeError, ValueError): pass
            if b.get('currency') in ('CAD', 'USD'):
                e['currency'] = b['currency']
            save_db(db)
            return self._json(200, {'ok': True, 'expense': e})

        if self.path == '/api/expenses/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            e = _expense_by_id(c, str(b.get('id') or ''))
            if e and e.get('receipt'):
                import os as _os
                try: _os.remove(str(BASE) + e['receipt'])
                except Exception: pass
            c['expenses'] = [x for x in _expenses(c) if x.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/expenses/receipt':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            e = _expense_by_id(c, str(b.get('id') or ''))
            if not e:
                return self._json(404, {'error': 'expense not found'})
            m = re.match(r'^data:image/(png|jpeg|jpg|webp);base64,(.+)$', str(b.get('photo') or ''), re.S)
            if not m:
                return self._json(400, {'error': 'receipt must be an image'})
            import base64 as _be, os as _oe
            ext = {'jpeg': 'jpg'}.get(m.group(1), m.group(1))
            raw = _be.b64decode(m.group(2))
            if len(raw) > 8 * 1024 * 1024:
                return self._json(400, {'error': 'receipt too large (8MB max)'})
            owner = re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
            d2 = BASE / 'uploads' / owner
            _oe.makedirs(d2, exist_ok=True)
            fname = 'receipt_' + e['id'] + '.' + ext
            (d2 / fname).write_bytes(raw)
            e['receipt'] = '/uploads/' + owner + '/' + fname
            save_db(db)
            return self._json(200, {'ok': True, 'url': e['receipt']})

        if self.path == '/api/client/invoices/duplicate':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((x for x in _client_invoices(c) if x.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            import uuid as _u
            dup = dict(inv)
            dup['id'] = _u.uuid4().hex[:10]
            dup['number'] = 'INV-%03d' % (c.get('inv_counter', 0) + 1)
            c['inv_counter'] = c.get('inv_counter', 0) + 1
            dup['created'] = time.time()
            dup['status'] = 'sent'
            dup['payments'] = []
            dup['reminders'] = 0
            dup['last_reminded'] = 0
            dup.pop('auto_marks', None)
            dup['due_ts'] = time.time() + (b.get('due_days') or 14) * 86400
            _client_invoices(c).append(dup)
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': dup})

        if self.path == '/api/client/invoices/pay':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((x for x in _client_invoices(c) if x.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            try:
                amount = round(float(b.get('amount') or 0), 2)
            except (TypeError, ValueError):
                amount = 0
            if amount <= 0:
                return self._json(400, {'error': 'amount must be positive'})
            method = str(b.get('method') or 'payment')[:60]
            inv.setdefault('payments', []).append({'amount': amount, 'method': method, 'ts': time.time()})
            bal, paid_amt = invoice_pdf.inv_balance(inv)
            if bal <= 0 and inv.get('status') == 'sent':
                inv['status'] = 'paid'
            c.setdefault('activity', []).append({'type': 'payment', 'ts': time.time(),
                'desc': 'Payment $%.2f on %s (%s)%s' % (amount, inv['number'], method, ' - fully paid' if bal <= 0 else '')})
            save_db(db)
            return self._json(200, {'ok': True, 'invoice': inv, 'balance': bal, 'paid_total': paid_amt})

        if self.path == '/api/client/invoices/test-send':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            inv = next((x for x in _client_invoices(c) if x.get('id') == b.get('id')), None)
            if not inv:
                return self._json(404, {'error': 'invoice not found'})
            owner_email = c.get('email', '')
            if not owner_email:
                return self._json(400, {'error': 'no owner email on account'})
            base = _public_base(c)
            biz = (c.get('settings') or {}).get('business_name') or c.get('business', '')
            body = ('TEST PREVIEW - this is what your invoice emails look like.\n\n'
                    + biz + ' sent invoice ' + inv.get('number', '') + ' for $%.2f.\n\n' % inv.get('total', 0)
                    + 'View the PDF: ' + base + '/invoice/' + inv['id'] + '.pdf\n\n'
                    + '- Sent via ' + biz)
            ok, err = send_client_email(c, owner_email, 'TEST: invoice ' + inv.get('number', '') + ' preview', body)
            if not ok:
                return self._json(502, {'error': err or 'send failed'})
            return self._json(200, {'ok': True})

        if self.path == '/api/client/invoices/statement':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cl = _client_by_id(c, str(b.get('client_id') or ''))
            if not cl:
                return self._json(404, {'error': 'client not found'})
            props = _properties_of_client(c, cl['id'])
            prop_ids = {p['id'] for p in props}
            invs = [x for x in _client_invoices(c) if x.get('client_key') in prop_ids]
            invs.sort(key=lambda x: x.get('created', 0))
            label = time.strftime('%B %Y')
            try:
                pdf = invoice_pdf.render_statement_pdf(c, cl.get('name', 'Client'), invs, label, str(BASE))
            except Exception as e:
                return self._json(500, {'error': 'statement render failed: ' + str(e)})
            import base64 as _b64s
            return self._json(200, {'ok': True, 'pdf_b64': _b64s.b64encode(pdf).decode(), 'label': label, 'count': len(invs)})

        if self.path == '/api/client/invoices/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            before = len(_client_invoices(c))
            c['client_invoices'] = [i for i in _client_invoices(c) if i.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['client_invoices']) < before})

        if self.path == '/api/crm/photo':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            try:
                url = _save_photo(c, lead, b.get('photo', ''), b.get('caption', ''))
            except ValueError as e:
                return self._json(400, {'error': str(e)})
            c.setdefault('activity', []).append({'type': 'crm', 'ts': time.time(),
                'desc': f"Photo added to {lead.get('title') or lead.get('client') or 'a project'}"})
            save_db(db)
            return self._json(200, {'ok': True, 'url': url, 'photos': lead.get('photos', [])})

        if self.path == '/api/crm/lead/docs':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not member:
                member = {'name': 'API (key)'}
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            import os as _os, re as _re, uuid as _uuid, base64 as _b64
            data_url = b.get('file', '')
            m2 = _re.match(r'^data:([^;]+);base64,(.+)$', data_url, re.S)
            if not m2:
                return self._json(400, {'error': 'bad file'})
            raw = _b64.b64decode(m2.group(2))
            if len(raw) > 12 * 1024 * 1024:
                return self._json(400, {'error': 'file too large (12MB max)'})
            fname = str(b.get('name') or 'document')[:120]
            ext = _os.path.splitext(fname)[1].lower()[:10] or '.bin'
            
            doc_id = _uuid.uuid4().hex[:10]
            owner = _re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
            out_dir = BASE / 'uploads' / owner / 'leaddocs'
            _os.makedirs(out_dir, exist_ok=True)
            (out_dir / (doc_id + ext)).write_bytes(raw)
            url = '/uploads/' + owner + '/leaddocs/' + doc_id + ext
            att = {'id': doc_id, 'name': fname, 'url': url, 'mime': m2.group(1),
                   'size': len(raw), 'ts': time.time(), 'by': (member or {}).get('name') or (member or {}).get('email') or 'owner'}
            lead.setdefault('attachments', []).append(att)
            save_db(db)
            return self._json(200, {'ok': True, 'doc': att})

        if self.path == '/api/crm/lead/docs/delete':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            doc_id = b.get('doc_id')
            before = len(lead.get('attachments', []))
            att = next((a for a in (lead.get('attachments') or []) if a.get('id') == doc_id), None)
            if att:
                f = BASE / att['url'].lstrip('/')
                if f.exists():
                    try:
                        f.unlink()
                    except Exception:
                        pass
            lead['attachments'] = [a for a in lead.get('attachments', []) if a.get('id') != doc_id]
            save_db(db)
            return self._json(200, {'ok': len(lead.get('attachments', [])) < before})

        if self.path == '/api/crm/photo/caption':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            ph = next((p for p in (lead.get('photos') or []) if p.get('id') == b.get('photo_id')), None)
            if not ph:
                return self._json(404, {'error': 'photo not found'})
            cap = str(b.get('caption') or '')[:500]
            ph['caption'] = cap
            # keep the timeline entry in sync
            for a in (lead.get('activities') or []):
                if a.get('type') == 'photo' and a.get('url') == ph.get('url'):
                    a['text'] = cap or 'Photo added'
            save_db(db)
            return self._json(200, {'ok': True, 'photos': lead.get('photos')})

        if self.path == '/api/crm/photo/send':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            ph = next((p for p in (lead.get('photos') or []) if p.get('id') == b.get('photo_id')), None)
            if not ph:
                return self._json(404, {'error': 'photo not found'})
            ok, detail = send_photo_to_client(c, lead, ph)
            if ok:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'sms',
                    'text': 'Photo sent to client: ' + (ph.get('caption') or 'photo'), 'by': 'owner'})
                save_db(db)
            return self._json(200 if ok else 400, {'ok': ok, 'detail': detail, 'error': None if ok else detail})

        if self.path == '/api/crm/photo/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            pid = b.get('photo_id')
            before = len(lead.get('photos', []))
            lead['photos'] = [p for p in lead.get('photos', []) if p.get('id') != pid]
            lead['activities'] = [a for a in lead.get('activities', [])
                                  if not (a.get('type') == 'photo' and a.get('url') == b.get('url'))]
            save_db(db)
            return self._json(200, {'ok': len(lead.get('photos', [])) < before})

        if self.path == '/api/crm/lead':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            leads = _crm_leads(c)
            if b.get('id'):  # update existing
                lead = next((l for l in leads if l.get('id') == b['id']), None)
                if not lead:
                    return self._json(404, {'error': 'lead not found'})
            else:  # create new
                import uuid
                lead = {'id': uuid.uuid4().hex[:12], 'created': time.time(), 'stage': 'Lead',
                        'activities': [{'ts': time.time(), 'type': 'created', 'text': 'Lead created', 'by': 'owner'}]}
                leads.append(lead)
            for f in ('title', 'client', 'phone', 'email', 'address', 'notes', 'job_date', 'source'):
                if f in b:
                    lead[f] = str(b[f])[:500]
            if 'amount' in b:
                try:
                    lead['amount'] = float(b['amount']) if b['amount'] not in (None, '') else None
                except (TypeError, ValueError):
                    pass
            if b.get('stage') in _crm_stages(c):
                lead['stage'] = b['stage']
            save_db(db)
            return self._json(200, {'ok': True, 'lead': lead})

        if self.path == '/api/crm/lead/opnotes':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            lead['op_notes'] = str(b.get('op_notes') or '')[:1000]
            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
                'text': 'Operator notes updated', 'by': 'owner'})
            save_db(db)
            return self._json(200, {'ok': True, 'op_notes': lead['op_notes']})

        if self.path == '/api/crm/lead-sources':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            srcs = c.setdefault('lead_sources', [])
            name = str(b.get('name') or '').strip()[:60]
            if b.get('remove'):
                if name in srcs:
                    srcs.remove(name)
            elif name and name not in srcs and len(srcs) < 40:
                srcs.append(name)
            save_db(db)
            return self._json(200, {'ok': True, 'sources': srcs})

        if self.path == '/api/crm/lead-fields':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            fields = c.setdefault('lead_fields', [])
            fid = str(b.get('id') or '').strip()[:40]
            if b.get('remove'):
                c['lead_fields'] = [f for f in fields if f.get('id') != fid]
                save_db(db)
                return self._json(200, {'ok': True, 'fields': c['lead_fields']})
            label = str(b.get('label') or '').strip()[:60]
            ftype = str(b.get('type') or 'text').strip().lower()
            if ftype not in ('text', 'number', 'date', 'select'):
                ftype = 'text'
            options = b.get('options')
            if isinstance(options, str):
                options = [o.strip()[:60] for o in options.split(',') if o.strip()]
            if not isinstance(options, list):
                options = []
            options = [str(o)[:60] for o in options[:20]]
            if fid:
                f = next((x for x in fields if x.get('id') == fid), None)
                if not f:
                    return self._json(404, {'error': 'field not found'})
                if label:
                    f['label'] = label
                f['type'] = ftype
                f['options'] = options
            else:
                if not label:
                    return self._json(400, {'error': 'label required'})
                fid = 'fld_' + str(int(time.time() * 1000))[-10:]
                fields.append({'id': fid, 'label': label, 'type': ftype, 'options': options})
            save_db(db)
            return self._json(200, {'ok': True, 'fields': fields})

        if self.path == '/api/crm/lead/field':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            fid = str(b.get('field_id') or '').strip()[:40]
            if not fid:
                return self._json(400, {'error': 'field_id required'})
            vals = lead.setdefault('custom_fields', {})
            val = b.get('value')
            if val is None or val == '':
                vals.pop(fid, None)
            else:
                vals[fid] = str(val)[:500]
            save_db(db)
            return self._json(200, {'ok': True, 'lead': lead})

        if self.path == '/api/crm/lead/assign':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((x for x in _crm_leads(c) if x.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            assignee = str(b.get('assignee') or '').strip()[:120]
            if assignee:
                valid = [c.get('email', '')] + [m.get('email', '') for m in c.get('team_members', [])]
                if assignee not in valid:
                    return self._json(400, {'error': 'unknown team member'})
                lead['assignee'] = assignee
            else:
                lead.pop('assignee', None)
            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
                'text': 'Assigned to ' + (assignee or 'unassigned'), 'by': ((member or {}).get('name') or (member or {}).get('email') or 'owner')})
            save_db(db)
            return self._json(200, {'ok': True, 'assignee': assignee})

        if self.path == '/api/crm/lead/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            leads = _crm_leads(c)
            before = len(leads)
            c['crm_leads'] = [l for l in leads if l.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['crm_leads']) < before})

        if self.path == '/api/estimates/create':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            ests = c.setdefault('estimates', [])
            est_num = c.get('est_counter', 0) + 1
            c['est_counter'] = est_num
            import uuid
            est = {
                'id': uuid.uuid4().hex[:10],
                'number': f"EST-{est_num:03d}",
                'created': time.time(),
                'status': 'draft',
                'lead_id': b.get('lead_id') or None,
                'client': str(b.get('client') or '')[:200],
                'address': str(b.get('address') or '')[:300],
                'line_items': [],
                'notes': str(b.get('notes') or '')[:2000],
            }
            for item in (b.get('line_items') or [])[:50]:
                try:
                    price = float(item.get('price') or 0)
                except (TypeError, ValueError):
                    price = 0
                est['line_items'].append({
                    'desc': str(item.get('desc') or '')[:300],
                    'price': price,
                })
            try:
                _cl = next((x for x in _crm_leads(c) if x.get('id') == inv.get('client_key')), None)
                if _cl:
                    client_notify(c, (_cl.get('email') or '').lower().strip(), 'invoice',
                                  'New invoice ' + inv['number'] + ' for $%.2f' % inv.get('total', 0))
            except Exception:
                pass
            est['total'] = round(sum(i['price'] for i in est['line_items']), 2)
            ests.append(est)
            # link to lead timeline + stage
            if est['lead_id']:
                lead = next((l for l in _crm_leads(c) if l.get('id') == est['lead_id']), None)
                if lead:
                    lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
                        'text': f"Estimate {est['number']} drafted (${est['total']:,.0f})", 'by': 'owner'})
            c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
                'desc': f"Estimate {est['number']} drafted for {est['client'] or 'a client'}"})
            save_db(db)
            return self._json(200, {'ok': True, 'estimate': est})

        if self.path == '/api/estimates/update':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            est = next((e for e in c.setdefault('estimates', []) if e.get('id') == b.get('id')), None)
            if not est:
                return self._json(404, {'error': 'estimate not found'})
            if 'status' in b and b['status'] in ('draft', 'sent', 'approved', 'declined'):
                old = est['status']
                est['status'] = b['status']
                if old != b['status'] and est.get('lead_id'):
                    lead = next((l for l in _crm_leads(c) if l.get('id') == est['lead_id']), None)
                    if lead:
                        lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note',
                            'text': f"Estimate {est['number']}: {old} → {b['status']}", 'by': 'owner'})
                        if b['status'] == 'sent' and lead.get('stage') == 'Lead':
                            lead['stage'] = 'Quote Sent'
                            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'stage',
                                'text': 'Stage changed: Lead → Quote Sent', 'by': 'owner'})
                        if b['status'] == 'approved' and lead.get('stage') in ('Lead', 'Quote Sent', 'In Progress'):
                            old_stage = lead.get('stage')
                            lead['stage'] = 'Booked/Approved'
                            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'stage',
                                'text': f'Stage changed: {old_stage} → Booked/Approved', 'by': 'owner'})
                        if b['status'] == 'declined' and lead.get('stage') in ('Lead', 'Quote Sent'):
                            old_stage = lead.get('stage')
                            lead['stage'] = 'Lost Lead'
                            lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'stage',
                                'text': f'Stage changed: {old_stage} → Lost Lead', 'by': 'owner'})
                if old != b['status']:
                    c.setdefault('activity', []).append({'type': 'quote', 'ts': time.time(),
                        'desc': f"Estimate {est['number']} {b['status']}"})
            for f in ('client', 'address', 'notes'):
                if f in b:
                    est[f] = str(b[f])[:2000]
            if 'line_items' in b:
                est['line_items'] = [{'desc': str(i.get('desc') or '')[:300],
                                      'price': float(i.get('price') or 0)} for i in (b['line_items'] or [])[:50]]
                est['total'] = round(sum(i['price'] for i in est['line_items']), 2)
            save_db(db)
            return self._json(200, {'ok': True, 'estimate': est})

        if self.path == '/api/estimates/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            before = len(c.setdefault('estimates', []))
            c['estimates'] = [e for e in c['estimates'] if e.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['estimates']) < before})

        if self.path == '/api/estimate-templates/save':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            name = (b.get('name') or '').strip()[:100]
            items = b.get('items') or []
            if not name or not items:
                return self._json(400, {'error': 'Template needs a name and at least one line item'})
            tpls = c.setdefault('estimate_templates', [])
            tid = b.get('id')
            if tid:
                t = next((x for x in tpls if x.get('id') == tid), None)
                if not t:
                    return self._json(404, {'error': 'template not found'})
                t.update({'name': name, 'items': items[:40], 'notes': (b.get('notes') or '')[:500]})
            else:
                tpls.append({'id': secrets.token_hex(8), 'name': name,
                             'items': items[:40], 'notes': (b.get('notes') or '')[:500],
                             'created': int(time.time())})
            save_db(db)
            return self._json(200, {'ok': True, 'templates': sorted(tpls, key=lambda t: t.get('created', 0), reverse=True)})

        if self.path == '/api/estimate-templates/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            before = len(c.setdefault('estimate_templates', []))
            c['estimate_templates'] = [t for t in c['estimate_templates'] if t.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['estimate_templates']) < before})

        if self.path == '/api/tts':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            body = self._body()
            text = (body.get('text') or '')[:300]
            voice = (body.get('voice') or c.get('voice') or 'sonia')
            try:
                if voice.startswith('el:'):
                    key = el_key(c)
                    if not key:
                        return self._json(400, {'error': 'ElevenLabs not connected — add your key in Integrations'})
                    fn = tts_elevenlabs(text, voice[3:], key)
                else:
                    edge_voice = VOICES.get(voice, {}).get('edge', VOICES['sonia']['edge'])
                    fn = tts_edge(text, edge_voice)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.end_headers()
                with open(fn, 'rb') as f:
                    self.wfile.write(f.read())
                import os as _os
                _os.unlink(fn)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if self.path == '/api/theme':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            if b.get('theme') not in ('dark', 'light'):
                return self._json(400, {'error': 'invalid theme'})
            c['theme'] = b['theme']
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/voice':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            v = (b.get('voice') or '').strip()
            if v.startswith('el:'):
                if not el_key(c):
                    return self._json(400, {'error': 'ElevenLabs not connected'})
                c['voice'] = v
            elif v in VOICES:
                c['voice'] = v
            else:
                return self._json(400, {'error': 'unknown voice'})
            save_db(db)
            return self._json(200, {'ok': True, 'voice': v})

        if self.path == '/api/internal/session':
            b = self._body()
            if b.get('admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            email = b.get('email', '').lower().strip()
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            # reuse existing telegram token or create
            tok = SESSIONS.get(c.get('telegram_session_token'))
            if tok and SESSIONS.get(c.get('telegram_session_token')) == email:
                return self._json(200, {'token': c['telegram_session_token']})
            token = secrets.token_hex(16)
            SESSIONS[token] = email
            _sessions_save()
            c['telegram_session_token'] = token
            save_db(db)
            return self._json(200, {'token': token})

        if self.path == '/api/demo-login':
            return self._json(404, {'error': 'demo no longer available'})

        if self.path == '/api/broadcasts/dismiss':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            ts = b.get('ts')
            c.setdefault('broadcasts_dismissed', [])
            if ts not in c['broadcasts_dismissed']:
                c['broadcasts_dismissed'].append(ts)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/team/invite':
            # owner only: create an invite {email, role, name?}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'Only the account owner can invite team members'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            role = (b.get('role') or '').strip()
            name = (b.get('name') or '').strip()[:100]
            if not email or '@' not in email or '.' not in email.split('@')[-1]:
                return self._json(400, {'error': 'Enter a valid email address'})
            if role not in TEAM_ROLES:
                return self._json(400, {'error': 'Pick a role: admin or moderator'})
            if email == c['email']:
                return self._json(400, {'error': "That's your own login — you're already the owner"})
            if any(m['email'] == email for m in c.get('team_members', [])):
                return self._json(400, {'error': 'That person is already on your team'})
            invites = c.setdefault('team_invites', [])
            invites[:] = [i for i in invites if i['email'] != email]  # replace stale invite
            code = secrets.token_urlsafe(5).replace('-', '').replace('_', '')[:7].upper()
            inv = {'email': email, 'role': role, 'name': name, 'code': code,
                   'created': int(time.time())}
            invites.append(inv)
            save_db(db)
            return self._json(200, {'ok': True, 'invite': inv,
                                    'role_label': TEAM_ROLES[role]['label']})

        if self.path == '/api/team/accept':
            # invitee accepts: {email, code, name, password} -> session token
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            code = (b.get('code') or '').upper().strip()
            name = (b.get('name') or '').strip()[:100]
            pw = (b.get('password') or '').strip()
            db = load_db()
            for owner_email, c in db['customers'].items():
                inv = next((i for i in c.get('team_invites', [])
                            if i['email'] == email and i['code'] == code), None)
                if inv:
                    if len(pw) < 6:
                        return self._json(400, {'error': 'Password needs at least 6 characters'})
                    if not name:
                        return self._json(400, {'error': 'Your name is required'})
                    c['team_invites'].remove(inv)
                    m = {'email': email, 'name': name, 'role': inv['role'], 'password': pw,
                         'created': int(time.time()), 'last_login': int(time.time())}
                    c.setdefault('team_members', [])
                    c['team_members'] = [x for x in c['team_members'] if x['email'] != email]
                    c['team_members'].append(m)
                    save_db(db)
                    token = secrets.token_hex(16)
                    SESSIONS[token] = {'email': owner_email, 'member': m}
                    _sessions_save()
                    return self._json(200, {'ok': True, 'token': token, 'name': name,
                                            'role': m['role'],
                                            'business': c.get('business', '')})
            return self._json(401, {'error': 'Invalid invite email or code'})

        if self.path == '/api/team/role':
            # owner only: change a member's role {email, role}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'Only the account owner can change roles'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            role = (b.get('role') or '').strip()
            if role not in TEAM_ROLES:
                return self._json(400, {'error': 'Pick a role: admin or moderator'})
            m = next((x for x in c.get('team_members', []) if x['email'] == email), None)
            if not m:
                return self._json(404, {'error': 'No team member with that email'})
            m['role'] = role
            save_db(db)
            return self._json(200, {'ok': True, 'email': email, 'role': role,
                                    'role_label': TEAM_ROLES[role]['label']})

        if self.path == '/api/team/password':
            # owner only: reset a team member's password {email, password}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'Only the account owner can change team passwords'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            pw = (b.get('password') or '').strip()
            if len(pw) < 6:
                return self._json(400, {'error': 'Password needs at least 6 characters'})
            m = next((x for x in c.get('team_members', []) if x['email'] == email), None)
            if not m:
                return self._json(404, {'error': 'No team member with that email'})
            m['password'] = pw
            m['password_changed_at'] = int(time.time())
            # force re-login on any live sessions for that member
            for tok, s in list(SESSIONS.items()):
                if isinstance(s, dict) and s.get('email') == c['email'] and \
                   (s.get('member') or {}).get('email') == email:
                    SESSIONS.pop(tok, None)
            save_db(db)
            _sessions_save()
            return self._json(200, {'ok': True, 'email': email})

        if self.path == '/api/team/remove':
            # owner only: remove member or revoke invite {email}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if member:
                return self._json(403, {'error': 'Only the account owner can remove team members'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            before = len(c.get('team_members', [])) + len(c.get('team_invites', []))
            c['team_members'] = [m for m in c.get('team_members', []) if m['email'] != email]
            c['team_invites'] = [i for i in c.get('team_invites', []) if i['email'] != email]
            after = len(c['team_members']) + len(c['team_invites'])
            if after == before:
                return self._json(404, {'error': 'No member or invite with that email'})
            # kill any live sessions for the removed member
            for tok, s in list(SESSIONS.items()):
                if isinstance(s, dict) and s.get('email') == c['email'] and \
                   (s.get('member') or {}).get('email') == email:
                    SESSIONS.pop(tok, None)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/team/login':
            # returning team member login: {email, password} -> scoped session
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            pw = b.get('password') or ''
            db = load_db()
            for owner_email, c in db['customers'].items():
                m = next((x for x in c.get('team_members', [])
                          if x['email'] == email and x.get('password') == pw), None)
                if m:
                    m['last_login'] = int(time.time())
                    save_db(db)
                    token = secrets.token_hex(16)
                    SESSIONS[token] = {'email': owner_email, 'member': m}
                    _sessions_save()
                    return self._json(200, {'ok': True, 'token': token, 'name': m.get('name', ''),
                                            'role': m['role'], 'team': True,
                                            'business': c.get('business', '')})
            return self._json(401, {'error': 'Invalid team email or password'})

        if self.path == '/api/setup-lead':
            # public: setup-fee quote form submission (no auth)
            b = self._body()
            name = str(b.get('name') or '').strip()[:100]
            email = str(b.get('email') or '').lower().strip()
            if not name:
                return self._json(400, {'error': 'Your name is required'})
            if not email or '@' not in email:
                return self._json(400, {'error': 'A valid email is required'})
            db = load_db()
            entry = {'ts': int(time.time()),
                     'name': name,
                     'business': str(b.get('business') or '').strip()[:120],
                     'email': email,
                     'phone': str(b.get('phone') or '').strip()[:40],
                     'items': [str(x)[:120] for x in (b.get('items') or [])][:30],
                     'total': round(float(b.get('total') or 0), 2),
                     'monthly': str(b.get('monthly') or '')[:20],
                     'source': 'setup-form'}
            db.setdefault('setup_leads', []).append(entry)
            db['setup_leads'] = db['setup_leads'][-200:]
            log_activity(db, email, 'setup-lead',
                         f"🧾 Setup quote: {name} ({entry['business'] or 'no business'}) — ${entry['total']:,.0f} build · {len(entry['items'])} items", save=False)
            save_db(db)
            # notify Jacob by email, best-effort
            try:
                lines = '\n'.join(' - ' + x for x in entry['items']) or ' - (base setup only)'
                body_txt = (f"New setup-fee quote request from anos.ca/setup\n\n"
                            f"Name: {entry['name']}\nBusiness: {entry['business']}\n"
                            f"Email: {entry['email']}\nPhone: {entry['phone']}\n\n"
                            f"Build total: ${entry['total']:,.0f} one-time · plan: {entry['monthly']}\n\nItems:\n{lines}")
                gmail_send({'integrations': {}}, 'jacobbonofiglio14@gmail.com',
                           f"Setup quote: {entry['name']} — ${entry['total']:,.0f}", body_txt)
            except Exception as e:
                print('[setup-lead email]', e)
            return self._json(200, {'ok': True})

        if self.path == '/api/signup':
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            name = (b.get('name') or '').strip()[:100]
            business = (b.get('business') or '').strip()[:120]
            pw = (b.get('password') or '').strip()
            if not email or '@' not in email or '.' not in email.split('@')[-1]:
                return self._json(400, {'error': 'Enter a valid email address'})
            if len(pw) < 6:
                return self._json(400, {'error': 'Password needs at least 6 characters'})
            if not name:
                return self._json(400, {'error': 'Your name is required'})
            db = load_db()
            if email in db['customers']:
                return self._json(400, {'error': 'That email already has an account — log in instead'})
            cust = {'name': name, 'business': business, 'email': email, 'password': pw,
                    'plan': 'lite', 'created': int(time.time()), 'billing_status': 'free',
                    'card_last4': '—', 'company_type': (b.get('company_type') or 'tree')[:20]}
            if MGMT_KEY:
                try:
                    k = provision_key(f"customer:{email}", PLANS['lite']['daily_limit'])
                    cust['openrouter_key'] = k.get('key')
                    cust['openrouter_key_hash'] = k.get('data', {}).get('hash') or k.get('hash')
                except Exception as e:
                    cust['provision_error'] = str(e)
            else:
                cust['provision_error'] = 'no management key configured'
            db['customers'][email] = cust
            ref = _ref_attach_signup(db, cust, b.get('ref'))
            log_activity(db, email, 'signup',
                         f"New signup: {name} ({email}) — {business or 'no business'}" +
                         (f" — referred by {ref.get('name', ref['email'])}" if ref else ''), save=False)
            save_db(db)
            token = secrets.token_hex(16)
            SESSIONS[token] = email
            _sessions_save()
            return self._json(200, {'ok': True, 'token': token, 'name': name})

        if self.path == '/api/auth/google':
            # Google OAuth: frontend sends the ID token from Google Identity Services
            b = self._body()
            id_token = (b.get('credential') or '').strip()
            if not id_token:
                return self._json(400, {'error': 'missing Google credential'})
            gclient_id = ENV.get('GOOGLE_OAUTH_CLIENT_ID', '')
            if not gclient_id:
                return self._json(500, {'error': 'Google sign-in not configured yet'})
            try:
                req = urllib.request.Request(
                    'https://oauth2.googleapis.com/tokeninfo?id_token=' + urllib.parse.quote(id_token))
                with urllib.request.urlopen(req, timeout=20) as r:
                    info = json.load(r)
            except Exception:
                return self._json(401, {'error': 'Google token rejected — try again'})
            if info.get('aud') != gclient_id:
                return self._json(401, {'error': 'token not issued for this app'})
            email = (info.get('email') or '').lower().strip()
            if not email or info.get('email_verified') not in ('true', True):
                return self._json(401, {'error': 'Google account email not verified'})
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                # auto-signup: create account from Google profile
                name = (info.get('name') or email.split('@')[0]).strip()[:100]
                c = {'name': name, 'business': '', 'email': email, 'password': '',
                     'google_sub': info.get('sub', ''), 'plan': 'lite', 'created': int(time.time()),
                     'billing_status': 'free', 'card_last4': '—', 'company_type': 'tree'}
                if MGMT_KEY:
                    try:
                        k = provision_key(f"customer:{email}", PLANS['lite']['daily_limit'])
                        c['openrouter_key'] = k.get('key')
                        c['openrouter_key_hash'] = k.get('data', {}).get('hash') or k.get('hash')
                    except Exception as e:
                        c['provision_error'] = str(e)
                db['customers'][email] = c
                ref = _ref_attach_signup(db, c, b.get('ref'))
                log_activity(db, email, 'signup',
                             f"New Google signup: {c['name']} ({email})" +
                             (f" — referred by {ref.get('name', ref['email'])}" if ref else ''), save=False)
                save_db(db)
            elif not c.get('google_sub'):
                c['google_sub'] = info.get('sub', '')
                save_db(db)
            token = secrets.token_hex(16)
            SESSIONS[token] = email
            _sessions_save()
            return self._json(200, {'ok': True, 'token': token, 'name': c.get('name', '')})

        if self.path == '/api/login':
            b = self._body()
            db = load_db()
            email = b.get('email', '').lower().strip()
            pw = b.get('password')
            matches = _account_matches(db, email, pw)
            # client portal logins (legacy single + multi-portals) join the same match list
            for ce, cust in db['customers'].items():
                cp = cust.get('client_portal') or {}
                if cp.get('enabled') and cp.get('client_email', '').lower().strip() == email:
                    if cp.get('temp_password') and cp['temp_password'] == pw:
                        if not cp.get('token'):
                            cp['token'] = secrets.token_hex(20)
                            save_db(db)
                        matches.append({'kind': 'client', 'token': cp['token'],
                                        'business': cust.get('business', ''),
                                        'name': cp.get('client_name', ''), 'role': 'client'})
                for p in _client_portals(cust):
                    if (p.get('email') or '').lower().strip() == email and p.get('enabled', True):
                        if p.get('password') and p['password'] == pw:
                            if not p.get('token'):
                                p['token'] = secrets.token_hex(20)
                                save_db(db)
                            matches.append({'kind': 'client', 'token': p['token'],
                                            'business': cust.get('business', ''),
                                            'name': p.get('name', ''), 'role': 'client'})
            if not matches:
                return self._json(401, {'error': 'Invalid email or password'})
            if len(matches) > 1:
                # client portal page asked directly for the client session — the account
                # picker is an owner-app concern; a client page has no picker UI
                if b.get('client_login'):
                    cm = next((m for m in matches if m['kind'] == 'client'), None)
                    if cm:
                        return self._json(200, {'token': cm['token'], 'client': True,
                                                'business': cm['business']})
                # multi-account: hand back a picker instead of silently choosing the first match
                pick = secrets.token_hex(16)
                PENDING_LOGINS[pick] = {'ts': int(time.time()), 'matches': matches}
                return self._json(200, {'multi': True, 'pick': pick, 'accounts': [
                    {'i': i, 'kind': m['kind'], 'role': m.get('role', ''),
                     'business': m.get('business', ''), 'name': m.get('name', '')}
                    for i, m in enumerate(matches)]})
            m0 = matches[0]
            if m0['kind'] == 'client':
                return self._json(200, {'token': m0['token'], 'client': True,
                                        'business': m0['business']})
            if m0['kind'] == 'team':
                m0['member']['last_login'] = int(time.time())
                save_db(db)
                return self._json(200, {'token': _issue_session(m0), 'name': m0.get('name', ''),
                                        'role': m0['role'], 'team': True})
            return self._json(200, {'token': _issue_session(m0), 'name': m0.get('name', '')})

        if self.path == '/api/auth/switch':
            # re-scope the current session to another account the person's email can access
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            person = member['email'] if member else c['email']
            target = (self._body().get('owner') or '').lower().strip()
            if not target or target not in db['customers']:
                return self._json(404, {'error': 'No such account'})
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            if target == person:
                SESSIONS[auth] = person
                _sessions_save()
                tc = db['customers'][target]
                return self._json(200, {'ok': True, 'business': tc.get('business', '')})
            tm = next((x for x in db['customers'][target].get('team_members', [])
                       if x.get('email') == person), None)
            if not tm:
                return self._json(403, {'error': 'Your email is not on that account'})
            SESSIONS[auth] = {'email': target, 'member': tm}
            _sessions_save()
            return self._json(200, {'ok': True, 'business': db['customers'][target].get('business', '')})

        if self.path == '/api/login/select':
            # second step of multi-account login: {pick, i} -> session for the chosen account
            b = self._body()
            pend = PENDING_LOGINS.pop(b.get('pick') or '', None)
            if not pend or int(time.time()) - pend['ts'] > 600:
                return self._json(401, {'error': 'That picker expired — log in again'})
            try:
                m = pend['matches'][int(b.get('i'))]
            except Exception:
                return self._json(400, {'error': 'Bad account selection'})
            if m['kind'] == 'client':
                return self._json(200, {'token': m['token'], 'client': True,
                                        'business': m['business']})
            if m['kind'] == 'team':
                db2 = load_db()
                live = next((x for x in db2['customers'].get(m['owner'], {}).get('team_members', [])
                             if x['email'] == m['member']['email']), m['member'])
                live['last_login'] = int(time.time())
                m['member'] = live
                save_db(db2)
                return self._json(200, {'token': _issue_session(m), 'name': m.get('name', ''),
                                        'role': m['role'], 'team': True})
            return self._json(200, {'token': _issue_session(m), 'name': m.get('name', '')})

        if self.path == '/api/crm/convert-to-client':
            # turn a CRM lead into a client-portal account: enables their login, returns creds + link
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            email = (lead.get('email') or '').lower().strip()
            if not email:
                return self._json(400, {'error': 'Lead has no email — add one first so they can log in'})
            cp = _client_portal(c)
            cp['enabled'] = True
            cp['client_email'] = email
            cp['client_name'] = lead.get('client') or lead.get('title') or ''
            cp['temp_password'] = secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:8]
            cp['token'] = secrets.token_hex(20)
            lead['converted_to_client'] = True
            save_db(db)
            return self._json(200, {'ok': True,
                'email': email, 'password': cp['temp_password'],
                'link': PORTAL_BASE + '/client'})

        if self.path == '/api/client/portal':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cp = _client_portal(c)
            if 'enabled' in b:
                cp['enabled'] = bool(b['enabled'])
            if 'client_email' in b:
                cp['client_email'] = str(b['client_email']).lower().strip()[:200]
            if 'client_name' in b:
                cp['client_name'] = str(b['client_name'])[:200]
            if 'temp_password' in b:
                pw = str(b['temp_password']).strip()
                if pw:
                    cp['temp_password'] = pw[:40]
                    cp['token'] = secrets.token_hex(20)
            if b.get('regen'):
                cp['temp_password'] = secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:8]
                cp['token'] = secrets.token_hex(20)
            save_db(db)
            return self._json(200, {'ok': True, 'portal': {k: v for k, v in cp.items() if k != 'token'},
                                    'link': 'https://app.anos.ca/client'})

        if self.path == '/api/client/portal/get':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            cp = _client_portal(c)
            return self._json(200, {'portal': {k: v for k, v in cp.items() if k != 'token'},
                                    'link': 'https://app.anos.ca/client'})

        if self.path == '/api/admin/create':  # simple admin: create customer + provision key
            b = self._body()
            if b.get('admin_secret') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            email = b['email'].lower().strip()
            db = load_db()
            if email in db['customers']:
                return self._json(400, {'error': 'exists'})
            plan = b.get('plan', 'starter')
            cust = {'name': b['name'], 'business': b.get('business', ''), 'email': email,
                    'password': b.get('password') or secrets.token_urlsafe(8),
                    'plan': plan, 'created': int(time.time()), 'billing_status': 'active',
                    'card_last4': b.get('card_last4', '—')}
            if MGMT_KEY:
                try:
                    k = provision_key(f"customer:{email}", PLANS[plan]['daily_limit'])
                    cust['openrouter_key'] = k.get('key')
                    cust['openrouter_key_hash'] = k.get('data', {}).get('hash') or k.get('hash')
                except Exception as e:
                    cust['provision_error'] = str(e)
            else:
                cust['provision_error'] = 'no management key configured'
            db['customers'][email] = cust
            log_activity(db, email, 'signup',
                         f"New customer created by admin: {cust['name']} ({email}) on {plan}", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'email': email, 'password': cust['password'],
                                    'provisioned': 'openrouter_key' in cust,
                                    'provision_error': cust.get('provision_error')})

        if self.path == '/api/admin/customer/plan':
            # Admin: change a customer's plan and sync the OpenRouter key limit
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            plan = b.get('plan')
            if plan not in PLANS:
                return self._json(400, {'error': 'bad plan'})
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            c['plan'] = plan
            err = None
            if MGMT_KEY and c.get('openrouter_key_hash'):
                try:
                    update_key_limit(c['openrouter_key_hash'], PLANS[plan]['daily_limit'])
                except Exception as e:
                    err = str(e)
            log_activity(db, email, 'plan',
                         f"{c.get('name', email)} moved to {PLANS[plan]['name']} (${PLANS[plan]['monthly']}/mo)", save=False)
            save_db(db)
            return self._json(200, {'ok': err is None, 'email': email, 'plan': plan, 'error': err})

        if self.path == '/api/admin/customer/delete':
            # Admin: remove a customer (disabled key is left to expire on OpenRouter)
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            db = load_db()
            if email not in db['customers']:
                return self._json(404, {'error': 'no customer'})
            try:
                channel_stop(email)
            except Exception:
                pass
            del db['customers'][email]
            log_activity(db, email, 'admin', f"Customer deleted: {email}", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'deleted': email})

        if self.path == '/api/admin/customer/status':
            # Admin one-click actions: pause / resume a customer's account
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            action = (b.get('action') or '').strip()
            if action not in ('pause', 'resume'):
                return self._json(400, {'error': 'action must be pause or resume'})
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            if action == 'pause':
                c['billing_status'] = 'paused'
                # zero out their AI cap so the agent stops spending
                if MGMT_KEY and c.get('openrouter_key_hash'):
                    try:
                        update_key_limit(c['openrouter_key_hash'], 0.0001)
                    except Exception:
                        pass
            else:
                c['billing_status'] = 'active'
                if MGMT_KEY and c.get('openrouter_key_hash'):
                    try:
                        update_key_limit(c['openrouter_key_hash'], PLANS[c['plan']]['daily_limit'])
                    except Exception:
                        pass
            log_activity(db, email, 'admin',
                         f"Account {action}d: {c.get('name', email)}", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'email': email, 'status': c['billing_status']})

        if self.path == '/api/admin/customer/notes':
            # Admin scratchpad per customer: {email, notes}
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            c['admin_notes'] = (b.get('notes') or '')[:2000]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/admin/broadcast':
            # Push an announcement into every customer's portal notifications
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            msg = (b.get('message') or '').strip()[:500]
            title = (b.get('title') or 'Announcement').strip()[:80]
            if not msg:
                return self._json(400, {'error': 'message required'})
            db = load_db()
            n = 0
            for email, c in db['customers'].items():
                c.setdefault('notifications', []).insert(0, {
                    'ts': int(time.time()), 'kind': 'broadcast',
                    'title': title, 'text': msg, 'read': False})
                c['notifications'] = c['notifications'][:50]
                n += 1
            log_activity(db, '', 'admin', f"Broadcast sent to {n} customers: {title}", save=False)
            save_db(db)
            return self._json(200, {'ok': True, 'sent': n})

        if self.path == '/api/admin/support/close':
            # Admin: mark a support ticket resolved
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            b = self._body()
            email = (b.get('email') or '').lower().strip()
            ts = b.get('ts')
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            found = False
            for t in c.get('support_tickets', []):
                if t.get('ts') == ts:
                    t['status'] = 'closed'
                    found = True
            if not found:
                return self._json(404, {'error': 'no ticket'})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/onboarding/dismiss':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            c['onboarding_dismissed'] = True
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/profile':
            b = self._body()
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            c['name'] = b.get('name', c['name'])
            c['business'] = b.get('business', c.get('business', ''))
            c['phone'] = b.get('phone', c.get('phone', ''))
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/password':
            b = self._body()
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not b.get('password'):
                return self._json(400, {'error': 'empty password'})
            c['password'] = b['password']
            save_db(db)
            return self._json(200, {'ok': True})

        # ---- Integrations: connect/disconnect third-party keys per customer ----
        if self.path == '/api/integrations':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            svc = b.get('service')
            ALL = {s['service']: s for s in INTEGRATIONS_CATALOG + CHANNEL_CATALOG}
            if svc not in ALL:
                return self._json(400, {'error': 'unknown service'})
            integ = c.setdefault('integrations', {})
            if b.get('action') == 'disconnect':
                if svc == 'telegram':
                    channel_stop(c['email'])
                integ.pop(svc, None)
                save_db(db)
                return self._json(200, {'ok': True})
            vals = {f: b.get(f, '').strip() for f in ALL[svc]['fields']}
            if not all(vals.values()):
                return self._json(400, {'error': 'missing fields: ' + ', '.join(f for f in ALL[svc]['fields'] if not vals.get(f))})
            integ[svc] = {'kind': ALL[svc]['kind'], 'connected_at': int(time.time()), **vals}
            save_db(db)
            if ALL[svc]['kind'] == 'channel' and svc == 'telegram':
                try:
                    pid = channel_start(c['email'], vals['bot_token'])
                    integ[svc]['pid'] = pid
                    save_db(db)
                except Exception as e:
                    return self._json(500, {'error': 'bridge failed: ' + str(e)})
            return self._json(200, {'ok': True, 'service': svc})

        if self.path == '/api/admin/integrations':
            # Admin-managed integrations: connect/disconnect on behalf of a customer
            if self.headers.get('X-Admin') != ENV.get('PORTAL_ADMIN_SECRET', 'mammoth-admin'):
                return self._json(403, {'error': 'forbidden'})
            email = self.headers.get('X-Email', '').lower().strip()
            db = load_db()
            c = db['customers'].get(email)
            if not c:
                return self._json(404, {'error': 'no customer'})
            b = self._body()
            svc = b.get('service')
            ALL = {s['service']: s for s in INTEGRATIONS_CATALOG + CHANNEL_CATALOG}
            if svc not in ALL:
                return self._json(400, {'error': 'unknown service'})
            integ = c.setdefault('integrations', {})
            if b.get('action') == 'disconnect':
                if svc == 'telegram':
                    channel_stop(c['email'])
                integ.pop(svc, None)
                save_db(db)
                return self._json(200, {'ok': True})
            vals = {f: b.get(f, '').strip() for f in ALL[svc]['fields']}
            if not all(vals.values()):
                return self._json(400, {'error': 'missing fields: ' + ', '.join(f for f in ALL[svc]['fields'] if not vals.get(f))})
            integ[svc] = {'kind': ALL[svc]['kind'], 'connected_at': int(time.time()), **vals}
            save_db(db)
            if ALL[svc]['kind'] == 'channel' and svc == 'telegram':
                try:
                    pid = channel_start(c['email'], vals['bot_token'])
                    integ[svc]['pid'] = pid
                    save_db(db)
                except Exception as e:
                    return self._json(500, {'error': 'bridge failed: ' + str(e)})
            return self._json(200, {'ok': True, 'service': svc, 'managed_by': 'admin'})

        if self.path == '/api/billing/checkout':
            # Create a Stripe Checkout session — subscribe, or start a free trial
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not STRIPE_KEY:
                return self._json(500, {'error': 'billing not configured'})
            b = self._body()
            plan_key = b.get('plan') if b.get('plan') in PLANS else c['plan']
            if plan_key != c['plan']:
                c['plan'] = plan_key
                save_db(db)
            plan = PLANS[plan_key]
            want_trial = bool(b.get('trial')) and not c.get('stripe_customer_id') and not c.get('trial_used')
            params = {
                'mode': 'subscription',
                'line_items[0][price]': plan['price_id'],
                'line_items[0][quantity]': 1,
                'customer_email': c['email'],
                'client_reference_id': c['email'],
                'success_url': PORTAL_BASE + '/?billing=success',
                'cancel_url': PORTAL_BASE + '/?billing=cancel',
                'metadata[email]': c['email'],
                'metadata[plan]': plan_key,
            }
            if want_trial:
                # Trial comes from the Price itself (recurring[trial_period_days]=7 baked into
                # the 3 live plan prices) — Checkout silently drops subscription_data on this
                # account, so the price carries the trial. Card still collected upfront:
                params['payment_method_collection'] = 'always'
            try:
                sess = stripe_request('POST', '/checkout/sessions', params)
                return self._json(200, {'ok': True, 'url': sess['url'], 'trial': want_trial})
            except Exception as e:
                return self._json(500, {'ok': False, 'error': str(e)})

        if self.path == '/api/billing/portal':
            # Stripe-hosted customer portal (manage card, cancel, invoices)
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not c.get('stripe_customer_id'):
                return self._json(400, {'ok': False, 'error': 'no billing account yet — subscribe first'})
            try:
                sess = stripe_request('POST', '/billing_portal/sessions', {
                    'customer': c['stripe_customer_id'],
                    'return_url': PORTAL_BASE + '/',
                })
                return self._json(200, {'ok': True, 'url': sess['url']})
            except Exception as e:
                return self._json(500, {'ok': False, 'error': str(e)})

        if self.path == '/api/reload/checkout':
            # One-time Stripe payment for a credit top-up
            b = self._body()
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            rk = b.get('reload')
            if rk not in RELOADS:
                return self._json(400, {'error': 'bad reload option'})
            if not STRIPE_KEY:
                return self._json(500, {'error': 'billing not configured'})
            r = RELOADS[rk]
            try:
                sess = stripe_request('POST', '/checkout/sessions', {
                    'mode': 'payment',
                    'line_items[0][price]': r['price_id'],
                    'line_items[0][quantity]': 1,
                    'customer_email': c['email'],
                    'client_reference_id': c['email'],
                    'success_url': PORTAL_BASE + '/?reload=success',
                    'cancel_url': PORTAL_BASE + '/?reload=cancel',
                    'metadata[type]': 'reload',
                    'metadata[reload]': rk,
                    'metadata[email]': c['email'],
                })
                return self._json(200, {'ok': True, 'url': sess['url']})
            except Exception as e:
                return self._json(500, {'ok': False, 'error': str(e)})

        if self.path == '/api/brain/save':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            n, err = brain.save_note(c, self._body())
            if err:
                return self._json(400, {'error': err})
            save_db(db)
            return self._json(200, {'ok': True, 'note': n})

        if self.path == '/api/brain/test':
            # Ask the brain: answer as the agent would, using only brain notes
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            q = (self._body().get('q') or '').strip()
            if not q:
                return self._json(400, {'error': 'ask something'})
            block = brain.prompt_block(c) or '(brain is empty)'
            system = ("You answer ONLY from the KNOWLEDGE BASE below. If the answer isn't there, say "
                      "\"I don't know that yet — add it to the Brain.\" Keep it to 1-3 sentences. " + block)
            try:
                ans = _llm_simple(c, system, q, 200)
                return self._json(200, {'ok': True, 'answer': ans})
            except Exception as e:
                return self._json(200, {'ok': False, 'answer': 'Brain test failed: ' + str(e)})

        if self.path == '/api/brain/suggest':
            # Auto-learn: scan recent chat for durable facts worth saving
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            log = c.get('chat_log', [])[-12:]
            if not log:
                return self._json(200, {'suggestions': []})
            convo = '\n'.join(f"{m['role']}: {m['text']}" for m in log)
            known = '\n'.join(n['title'] for n in brain.list_notes(c)) or 'none'
            system = ("You extract durable business facts the owner stated, that are worth remembering forever "
                      "(prices, policies, client details, preferences). NOT small talk, NOT task requests. "
                      f"Already-saved note titles: {known}. "
                      "Return a JSON array (max 3): [{\"title\":\"...\",\"body\":\"...\"}]. Return [] if nothing new worth saving.")
            try:
                raw = _llm_simple(c, system, convo, 250)
                m = re.search(r'\[.*\]', raw, re.S)
                sugg = json.loads(m.group(0)) if m else []
                sugg = [x for x in sugg if isinstance(x, dict) and x.get('title') and x.get('body')][:3]
                return self._json(200, {'suggestions': sugg})
            except Exception:
                return self._json(200, {'suggestions': []})

        if self.path == '/api/brain/quiz':
            # Quiz-me: next interview question based on what's missing
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            h = brain.health(c)
            missing = [i['label'] for i in h['items'] if not i['done']]
            if not missing:
                return self._json(200, {'done': True, 'question': "Your brain's fully loaded — nothing left to quiz you on. 🧠💪"})
            topic = missing[0]
            system = (f"You're onboarding a busy contractor, interview-style. Ask ONE short, friendly question to learn about: {topic}. "
                      "One sentence, plain words, no jargon. Examples of tone: \"What's your hourly rate for a typical removal?\"")
            try:
                q = _llm_simple(c, system, f"Ask about: {topic}", 80)
                return self._json(200, {'done': False, 'question': q, 'topic': topic})
            except Exception as e:
                return self._json(200, {'done': False, 'question': f"Tell me about your {topic.lower()} — what should I know?", 'topic': topic})

        if self.path == '/api/brain/quiz-answer':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            topic, answer = (b.get('topic') or '').strip(), (b.get('answer') or '').strip()
            if not answer:
                return self._json(400, {'error': 'empty answer'})
            # write the answer into a note for that topic
            title = topic.split(' — ')[0].split('(')[0].strip()
            existing = next((n for n in brain.list_notes(c)
                             if title.lower() in (n.get('title') or '').lower()), None)
            body_line = f"- {answer}"
            if existing:
                existing['body'] = (existing['body'].rstrip() + '\n' + body_line)[:brain.MAX_BODY]
                existing['updated'] = int(time.time())
            else:
                brain._notes(c).append({'id': secrets.token_hex(8), 'title': title,
                    'body': f"# {title}\n\n{body_line}", 'enabled': True, 'share': False,
                    'tags': ['quiz'], 'updated': int(time.time()), 'revisions': []})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/brain/restore':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            n, err = brain.restore_revision(c, b.get('id'), int(b.get('idx', -1)))
            if err:
                return self._json(404, {'error': err})
            save_db(db)
            return self._json(200, {'ok': True, 'note': n})

        if self.path == '/api/brain/import':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            n, err = brain.digest_text(c, self._body().get('text'), None)
            if err:
                return self._json(400, {'error': err})
            c.setdefault('activity', []).append({'type': 'note', 'ts': time.time(),
                'desc': f'Brain import: "{n["title"]}"'})
            save_db(db)
            return self._json(200, {'ok': True, 'note': n})

        if self.path == '/api/brain/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            ok = brain.delete_note(c, self._body().get('id'))
            save_db(db)
            return self._json(200, {'ok': ok})

        if self.path == '/api/connect/onboard':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not STRIPE_KEY:
                return self._json(500, {'error': 'billing not configured'})
            try:
                r = connect.acct_onboard(stripe_request, c, PORTAL_BASE)
                save_db(db)
                return self._json(200, r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode())
                    msg = body.get('error', {}).get('message', str(e))
                except Exception:
                    msg = str(e)
                if 'signed up for Connect' in msg:
                    return self._json(400, {'error': 'Stripe Connect is not enabled on the platform account yet. The account owner needs to activate it at https://dashboard.stripe.com/connect before clients can connect their billing.'})
                return self._json(400, {'error': msg})
            except Exception as e:
                return self._json(500, {'error': str(e)})

        if self.path == '/api/subs/plans/save':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            plan, err = connect.plan_save(c, self._body())
            if err:
                return self._json(400, {'error': err})
            save_db(db)
            return self._json(200, {'ok': True, 'plan': plan})

        if self.path == '/api/subs/plans/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            ok = connect.plan_delete(c, self._body().get('id'))
            save_db(db)
            return self._json(200, {'ok': ok})

        if self.path == '/api/subs/cancel':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            sub, err = connect.sub_cancel(stripe_request, c, self._body().get('id'))
            if err:
                return self._json(400, {'error': err})
            save_db(db)
            return self._json(200, {'ok': True, 'sub': sub})

        if self.path.startswith('/api/subscribe/'):
            # public: create checkout session for a plan token
            parts = self.path.split('/')
            token = parts[3] if len(parts) > 3 else ''
            db = load_db()
            c, plan = connect.public_plan(db, token)
            if not plan:
                return self._json(404, {'error': 'plan not found'})
            try:
                url, err = connect.checkout_plan(stripe_request, c, plan, PORTAL_BASE)
                if err:
                    return self._json(400, {'error': err})
                save_db(db)  # price id cache
                return self._json(200, {'ok': True, 'url': url})
            except Exception as e:
                return self._json(500, {'error': str(e)})

        if self.path.startswith('/api/invoice-pay/') and self.path.endswith('/checkout'):
            # Public: client clicks Pay on an invoice-pay link -> Stripe Checkout on the contractor's Connect account.
            token = self.path.split('/api/invoice-pay/')[-1].split('/checkout')[0].strip('/')[:80]
            db = load_db()
            c, inv = _find_invoice_by_pay_token(db, token)
            if not inv:
                return self._json(404, {'error': 'payment link not found'})
            bal = _invoice_balance(inv)
            if inv.get('status') == 'void':
                return self._json(400, {'error': 'invoice is void'})
            if bal <= 0:
                return self._json(400, {'error': 'invoice already paid'})
            amount = bal
            if inv.get('deposit') and not (inv.get('payments') or []):
                try:
                    amount = min(bal, round(float(inv.get('deposit') or bal), 2))
                except (TypeError, ValueError):
                    amount = bal
            st = connect._connect(c)
            if not (STRIPE_KEY and st.get('account_id')):
                return self._json(400, {'error': 'Stripe is not connected for this business yet'})
            base = _public_base(c)
            biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'Contractor')
            params = {
                'mode': 'payment',
                'line_items[0][price_data][currency]': 'cad',
                'line_items[0][price_data][unit_amount]': int(round(amount * 100)),
                'line_items[0][price_data][product_data][name]': f'Invoice {inv.get("number", "")} — {biz}',
                'line_items[0][quantity]': 1,
                'success_url': base + '/invoice-pay/' + token + '?done=1',
                'cancel_url': base + '/invoice-pay/' + token,
                'metadata[type]': 'invoice_pay',
                'metadata[anos_owner]': c.get('email', ''),
                'metadata[anos_invoice]': inv.get('id', ''),
                'metadata[anos_token]': token,
                'payment_intent_data[metadata][type]': 'invoice_pay',
                'payment_intent_data[metadata][anos_owner]': c.get('email', ''),
                'payment_intent_data[metadata][anos_invoice]': inv.get('id', ''),
            }
            try:
                sess = stripe_request('POST', '/checkout/sessions', params, on_account=st['account_id'])
            except Exception as e:
                return self._json(502, {'error': str(e)[:200]})
            return self._json(200, {'ok': True, 'url': sess.get('url'), 'amount': amount})

        if self.path == '/api/stripe/webhook':
            # Stripe -> us. NOTE: signature verification skipped (add STRIPE_WEBHOOK_SECRET for prod).
            try:
                event = self._body()
            except Exception:
                return self._json(400, {'error': 'bad payload'})
            etype = event.get('type')
            obj = event.get('data', {}).get('object', {})
            db = load_db()

            def find_by_email(email):
                return db['customers'].get((email or '').lower().strip())

            # SMS wallet top-ups (platform telephony)
            if etype == 'checkout.session.completed' and (obj.get('metadata') or {}).get('type') == 'sms_topup':
                meta = obj.get('metadata') or {}
                c = find_by_email(meta.get('anos_owner'))
                if c:
                    w = _sms_wallet(c)
                    if not any(e.get('stripe_session') == obj.get('id') for e in (w.get('ledger') or [])):
                        try:
                            amount = round(float(obj.get('amount_total') or 0) / 100, 2)
                        except (TypeError, ValueError):
                            amount = 0
                        if amount > 0:
                            w['balance'] = round((w.get('balance') or 0) + amount, 2)
                            w.setdefault('ledger', []).append({'ts': time.time(), 'amount': amount,
                                'desc': 'Wallet top-up (card)', 'stripe_session': obj.get('id')})
                            save_db(db)
                return self._json(200, {'received': True})

            # Contractor invoice card payments (Stripe Connect destination charges)
            if etype == 'checkout.session.completed' and (obj.get('metadata') or {}).get('type') == 'invoice_pay':
                meta = obj.get('metadata') or {}
                c = find_by_email(meta.get('anos_owner'))
                inv = next((i for i in _client_invoices(c) if i.get('id') == meta.get('anos_invoice')), None) if c else None
                if inv:
                    try:
                        amount = round(float(obj.get('amount_total') or 0) / 100, 2)
                    except (TypeError, ValueError):
                        amount = 0
                    pays = inv.setdefault('payments', [])
                    if amount > 0 and not any(p.get('stripe_session') == obj.get('id') for p in pays):
                        pays.append({'ts': time.time(), 'amount': amount, 'method': 'stripe',
                                     'stripe_session': obj.get('id')})
                        if _invoice_balance(inv) <= 0:
                            inv['status'] = 'paid'
                        c.setdefault('activity', []).append({'type': 'payment', 'ts': time.time(),
                            'desc': f"Stripe payment ${amount:,.2f} on {inv.get('number', 'invoice')}"})
                        save_db(db)
                return self._json(200, {'received': True})

            # Stripe Connect (contractor client subscriptions) gets first crack
            if connect.handle_webhook(db, save_db, etype, obj):
                return self._json(200, {'received': True})

            if etype == 'checkout.session.completed':
                email = obj.get('customer_email') or obj.get('client_reference_id') or obj.get('metadata', {}).get('email')
                meta = obj.get('metadata', {})
                c = find_by_email(email)
                if c:
                    if meta.get('type') == 'reload':
                        # Credit reload: raise today's daily limit by the boost amount
                        rk = meta.get('reload')
                        if rk in RELOADS and MGMT_KEY and c.get('openrouter_key_hash'):
                            boost = RELOADS[rk]['credit']
                            plan_cap = PLANS[c['plan']]['daily_limit']
                            today = time.strftime('%Y-%m-%d', time.gmtime())
                            # reset accumulated reloads if a new UTC day
                            if c.get('reload_date') != today:
                                c['reload_date'] = today
                                c['reload_credit_today'] = 0
                            new_limit = plan_cap + c.get('reload_credit_today', 0) + boost
                            try:
                                update_key_limit(c['openrouter_key_hash'], new_limit)
                                c['reload_credit_today'] = c.get('reload_credit_today', 0) + boost
                                c.setdefault('reload_history', []).append(
                                    {'ts': int(time.time()), 'reload': rk, 'credit': boost})
                            except Exception:
                                pass
                    else:
                        c['stripe_customer_id'] = obj.get('customer')
                        c['stripe_subscription_id'] = obj.get('subscription')
                        # Was this checkout a trial start? Checkout session doesn't say directly —
                        # the subscription.updated event that follows carries status=trialing,
                        # so just mark subscribed here; the subscription event refines it.
                        c['billing_status'] = 'active'
                    save_db(db)
            elif etype in ('customer.subscription.updated', 'customer.subscription.deleted'):
                cust_id = obj.get('customer')
                status = obj.get('status')  # trialing, active, past_due, canceled, unpaid
                for c in db['customers'].values():
                    if c.get('stripe_customer_id') == cust_id:
                        if status == 'trialing':
                            c['billing_status'] = 'trialing'
                            c['trial_used'] = True
                            c['trial_end'] = obj.get('trial_end')
                        else:
                            c['billing_status'] = 'active' if status == 'active' else status
                        # keep the referrer's signup list in sync
                        if c.get('referred_by'):
                            ref = _ref_find_by_code(db, c['referred_by'])
                            if ref:
                                for s in _referral(ref)['signups']:
                                    if (s.get('email') or '').lower() == c['email']:
                                        s['status'] = c['billing_status']
                        # If they changed plan in Stripe portal, sync our plan + OpenRouter limit
                        try:
                            price = obj['items']['data'][0]['price']['id']
                            for pk, pv in PLANS.items():
                                if pv['price_id'] == price and c['plan'] != pk:
                                    c['plan'] = pk
                                    if MGMT_KEY and c.get('openrouter_key_hash'):
                                        update_key_limit(c['openrouter_key_hash'], pv['daily_limit'])
                        except Exception:
                            pass
                        save_db(db)
                        break
            return self._json(200, {'received': True})

        if self.path == '/api/chat':
            b = self._body()
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            msg = (b.get('message') or '').strip()
            if not msg:
                return self._json(400, {'error': 'empty message'})
            or_key = c.get('openrouter_key') or ENV.get('OPENROUTER_API_KEY', '') or MGMT_KEY
            usage = get_key_usage(c['openrouter_key']) if c.get('openrouter_key') else {}
            plan = PLANS[c['plan']]
            integ = c.get('integrations', {})
            connected = ', '.join(f"{s} (connected)" if integ.get(s) else f"{s} (NOT connected — refer to Integrations page if user needs it)"
                                  for s in ['gmail', 'outlook', 'notion', 'googlecal'])
            tpl_names = ', '.join(t.get('name', '?') for t in c.get('estimate_templates', [])) or 'none saved'
            ag = c.get('settings_agent', {})
            ag_name = (ag.get('agent_name') or '').strip() or 'Anno'
            ag_pers = {'brief': 'Be very brief — 1-2 short sentences, no filler.',
                       'friendly': 'Be warm and conversational — a bit of personality is good.',
                       'pro': 'Be professional and precise.'}.get(ag.get('personality'), "You're conversational, concrete, brief (2-4 sentences).")
            ag_custom = (ag.get('instructions') or '').strip()
            brain_block = brain.prompt_block(c)
            tax = c.get('settings_tax', {})
            area = c.get('settings_service_area', {})
            book = c.get('settings_booking', {})
            biz_facts = ''
            if tax.get('rate'):
                biz_facts += f" Tax: {tax['rate']}% {tax.get('currency', 'CAD')}{' (prices include tax)' if tax.get('prices_include_tax') else ' (added on top)'}."
            if area.get('base'):
                biz_facts += f" Service area: within {area.get('radius_km', '?')} km of {area['base']}" + (f"; travel fee {area['travel_fee']}" if area.get('travel_fee') else '') + '.' + (f" {area.get('fee_note','')}" if area.get('fee_note') else '')
            if book.get('min_notice_h') or book.get('max_per_day'):
                biz_facts += f" Booking rules: minimum {book.get('min_notice_h', 0)}h notice" + (f", max {book['max_per_day']} jobs/day" if book.get('max_per_day') else '') + (f", {book.get('buffer_min', 0)}min buffer between jobs" if book.get('buffer_min') else '') + '.'
            sys_prompt = (f"You are {ag_name}, {(c.get('business') or c['name']) + "'s"} AI business agent (ANOSagent portal, owner-facing). "
                          f"Saved estimate templates: {tpl_names}. "
                          f"Plan: {plan['name']} (${plan['monthly']}/mo). AI usage today: ${usage.get('usage_daily', 0):.2f} / ${plan['daily_limit']:.2f}. "
                          f"Integrations: {connected}. "
                          f"Style: {ag_pers} " + biz_facts + 
                          (f"Standing instructions from the owner (always follow): {ag_custom} " if ag_custom else '') +
                          "You manage the owner's CRM pipeline (built in — always available, no integration needed): capture leads they mention, move deals between stages, answer questions about open quotes and jobs. "
                          "When an action tool runs, report the real result. Never claim you sent something unless the tool ran. "
                          "Refuse to do things outside your tools; suggest the Integrations page or buying features." +
                          brain_block)
            if not or_key:
                return self._json(200, {'reply': "I'm online but my brain isn't wired in yet — the team just needs to plug an AI key in."})
            c.setdefault('chat_log', []).append({'ts': int(time.time()), 'role': 'user', 'text': msg})
            run_id = secrets.token_hex(6)

            # ---- Anno bridge: route this customer's chat through Hermes (real Anno) ----
            BRIDGE_TOKEN_FILE = os.environ.get('PORTAL_BRIDGE_TOKEN_FILE', '/opt/anos/.portal_bridge_token')
            def _bridge_reply():
                try:
                    if not os.path.exists(BRIDGE_TOKEN_FILE):
                        return None
                    tok = open(BRIDGE_TOKEN_FILE).read().strip()
                    payload = json.dumps({'customer': c['email'], 'message': msg,
                                          'system_prompt': sys_prompt,
                                          'history': c['chat_log'][-10:]}).encode()
                    req = urllib.request.Request('http://127.0.0.1:8786/chat', data=payload,
                        method='POST', headers={'Content-Type': 'application/json', 'X-Bridge-Token': tok})
                    with urllib.request.urlopen(req, timeout=330) as r:
                        d = json.load(r)
                    if d.get('reply') and d.get('source') == 'hermes':
                        return d['reply']
                    return None  # fallback replies → let portal's own agent handle instead
                except Exception as e:
                    print('[bridge] unreachable, using portal agent:', e)
                    return None
            if ag.get('use_anno_bridge', True) and c['email'] == 'info@mammothtreeco.ca':
                reply = _bridge_reply()
                if reply is not None:
                    c['chat_log'].append({'ts': int(time.time()), 'role': 'assistant', 'text': reply})
                    c['chat_log'] = c['chat_log'][-40:]
                    save_db(db)
                    steps = [{'icon': '🧠', 'text': 'Answered by Anno (Hermes)', 'ts': int(time.time())}]
                    return self._json(200, {'reply': reply, 'steps': steps, 'run_id': run_id})

            try:
                steps = []
                def _run():
                    try:
                        return agent_loop(sys_prompt, c['chat_log'], c, or_key, db, steps, run_id=run_id)
                    finally:
                        AGENT_RUNS[run_id + ':done'] = True
                with futures.ThreadPoolExecutor(max_workers=1) as ex:
                    reply = ex.submit(_run).result(timeout=150)
                c['chat_log'].append({'ts': int(time.time()), 'role': 'assistant', 'text': reply})
                c['chat_log'] = c['chat_log'][-40:]
                save_db(db)
                return self._json(200, {'reply': reply, 'steps': steps, 'run_id': run_id})
            except Exception as e:
                return self._json(200, {'reply': 'Sorry — I glitched: ' + str(e)})

        if self.path == '/api/support':
            b = self._body()
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            subject = (b.get('subject') or '').strip()
            message = (b.get('message') or '').strip()
            priority = b.get('priority', 'normal')
            if not subject or not message:
                return self._json(400, {'error': 'subject and message required'})
            # Store locally + email you (placeholder — logs to console for now)
            ticket = {
                'ts': int(time.time()),
                'email': c['email'],
                'name': c['name'],
                'business': c.get('business', ''),
                'subject': subject,
                'message': message,
                'priority': priority,
                'status': 'open'
            }
            c.setdefault('support_tickets', []).append(ticket)
            save_db(db)
            print(f'[SUPPORT] {priority.upper()} from {c["email"]}: {subject}')
            return self._json(200, {'ok': True, 'ticket_id': ticket['ts']})

        if self.path == '/api/upgrade':
            b = self._body()
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            plan = b.get('plan')
            if plan not in PLANS:
                return self._json(400, {'error': 'bad plan'})
            c['plan'] = plan
            err = None
            if MGMT_KEY and c.get('openrouter_key_hash'):
                try:
                    update_key_limit(c['openrouter_key_hash'], PLANS[plan]['daily_limit'])
                except Exception as e:
                    err = str(e)
            save_db(db)
            return self._json(200, {'ok': err is None, 'plan': plan,
                                    'daily_limit': PLANS[plan]['daily_limit'],
                                    'monthly': PLANS[plan]['monthly'], 'error': err})


        if self.path == '/api/social/posts':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            if b.get('id'):
                post = next((p for p in posts if p.get('id') == b['id']), None)
                if not post:
                    return self._json(404, {'error': 'post not found'})
            else:
                import uuid as _uuid
                post = {'id': _uuid.uuid4().hex[:12], 'created': time.time(), 'status': 'draft'}
                posts.append(post)
            if 'content' in b:
                post['content'] = str(b['content'])[:5000]
            if 'platforms' in b:
                plats = b['platforms']
                if isinstance(plats, list):
                    post['platforms'] = [str(x)[:30] for x in plats if str(x).strip()]
            if 'scheduled_at' in b:
                post['scheduled_at'] = str(b['scheduled_at'])[:19]
            if b.get('status') in ('draft', 'scheduled', 'posted', 'failed', 'revision'):
                post['status'] = b['status']
            if 'revision_notes' in b:
                post['revision_notes'] = str(b['revision_notes'])[:1000]
            if post.get('scheduled_at') and post.get('status') == 'draft':
                post['status'] = 'scheduled'
            save_db(db)
            return self._json(200, {'ok': True, 'post': post})

        if self.path == '/api/social/posts/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            before = len(posts)
            c['social_posts'] = [p for p in posts if p.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['social_posts']) < before})

        if self.path == '/api/social/posts/publish':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            post = next((p for p in posts if p.get('id') == b.get('id')), None)
            if not post:
                return self._json(404, {'error': 'post not found'})
            # Manual workflow (Nov 4): no auto-posting — 'Post Now' just marks the post done.
            post['status'] = 'posted'
            post['posted_at'] = time.time()
            save_db(db)
            return self._json(200, {'ok': True, 'post': post})

        if self.path == '/api/social/posts/review':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            post = next((p for p in posts if p.get('id') == b.get('id')), None)
            if not post:
                return self._json(404, {'error': 'post not found'})
            action = b.get('action')
            if action == 'approve':
                post['status'] = 'scheduled'
                if not post.get('scheduled_at'):
                    import datetime as _dt
                    tomorrow = _dt.datetime.now() + _dt.timedelta(days=1)
                    post['scheduled_at'] = tomorrow.strftime('%Y-%m-%dT09:00')
                post.pop('revision_notes', None)
            elif action == 'revision':
                post['status'] = 'revision'
                post['revision_notes'] = str(b.get('revision_notes') or '')[:1000]
            elif action == 'decline':
                c['social_posts'] = [p for p in posts if p.get('id') != b.get('id')]
                save_db(db)
                return self._json(200, {'ok': True, 'deleted': True})
            else:
                return self._json(400, {'error': 'action must be approve/revision/decline'})
            save_db(db)
            return self._json(200, {'ok': True, 'post': post})

        if self.path == '/api/social/posts/photo':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            post = next((p for p in posts if p.get('id') == b.get('id')), None)
            if not post:
                return self._json(404, {'error': 'post not found'})
            try:
                url = _save_social_photo(c, b.get('data_url') or '')
            except ValueError as e:
                return self._json(400, {'error': str(e)})
            post['photo_url'] = url
            save_db(db)
            return self._json(200, {'ok': True, 'url': url})

        if self.path == '/api/social/posts/photo/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            posts = c.setdefault('social_posts', [])
            post = next((p for p in posts if p.get('id') == b.get('id')), None)
            if not post:
                return self._json(404, {'error': 'post not found'})
            post.pop('photo_url', None)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/tasks':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            tasks = c.setdefault('tasks', [])
            if b.get('id'):
                task = next((t for t in tasks if t.get('id') == b['id']), None)
                if not task:
                    return self._json(404, {'error': 'task not found'})
            else:
                import uuid as _uuid
                task = {'id': _uuid.uuid4().hex[:12], 'created': time.time(), 'status': 'todo', 'priority': 'medium'}
                tasks.append(task)
            if 'title' in b:
                task['title'] = str(b['title'])[:300]
            if 'notes' in b:
                task['notes'] = str(b['notes'])[:2000]
            if 'due' in b:
                task['due'] = str(b['due'])[:10]
            if 'priority' in b and b['priority'] in ('low', 'medium', 'high'):
                task['priority'] = b['priority']
            if 'status' in b and b['status'] in ('todo', 'inprogress', 'done'):
                task['status'] = b['status']
                if b['status'] == 'done':
                    task['done_at'] = time.time()
            if 'assignee' in b:
                val = str(b['assignee'] or '').strip()[:120]
                if val:
                    task['assignee'] = val
                else:
                    task.pop('assignee', None)
            if 'lead_id' in b:
                val = str(b['lead_id'] or '').strip()[:24]
                if val:
                    task['lead_id'] = val
                else:
                    task.pop('lead_id', None)
            if 'subtasks' in b and isinstance(b['subtasks'], list):
                import uuid as _uuid2
                task['subtasks'] = [{'id': str(x.get('id') or _uuid2.uuid4().hex[:8])[:16],
                                     'text': str(x.get('text') or '')[:300],
                                     'done': bool(x.get('done'))}
                                    for x in b['subtasks'][:50] if isinstance(x, dict) and str(x.get('text') or '').strip()]
            if 'order' in b:
                try:
                    task['order'] = float(b['order'])
                except (TypeError, ValueError):
                    pass
            save_db(db)
            return self._json(200, {'ok': True, 'task': task})

        if self.path == '/api/tasks/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            tasks = c.setdefault('tasks', [])
            before = len(tasks)
            c['tasks'] = [t for t in tasks if t.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': len(c['tasks']) < before})


        if self.path == '/api/client-error':
            # client-side JS error reports (no auth — best effort, size-capped)
            b = self._body() or {}
            try:
                rec = {'ts': time.time(), 'ua': self.headers.get('User-Agent', '')[:100]}
                for k in ('kind', 'msg', 'src', 'line', 'view'):
                    if k in b:
                        rec[k] = str(b[k])[:300]
                with open(BASE / 'client_errors.log', 'a') as f:
                    f.write(json.dumps(rec) + '\n')
            except Exception:
                pass
            return self._json(200, {'ok': True})

        if self.path == '/api/estimates/send':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            est_id = b.get('estimate_id') or b.get('id')
            est = next((e for e in c.get('estimates', []) if e.get('id') == est_id), None)
            if not est:
                return self._json(404, {'error': 'estimate not found'})
            to = str(b.get('to') or '').strip()
            if not to:
                # try to pull from linked lead
                lead = next((l for l in _crm_leads(c) if l.get('id') == est.get('lead_id')), None)
                to = lead.get('email', '') if lead else ''
            if not to:
                return self._json(400, {'error': 'No recipient email — add one to the lead or enter it manually'})
            subject = str(b.get('subject') or f"Estimate {est.get('number', '')} — {c.get('business', 'Your Contractor')}")[:300]
            body_txt = str(b.get('body') or f"""Hi {est.get('client', 'there')},

Please find your estimate attached. You can view and approve it online here:
{b.get('sign_url', '')}

Total: ${est.get('total', 0):,.2f}

Thanks,
{c.get('business', 'Your Contractor')}""")[:5000]
            try:
                ok, err = send_client_email(c, to, subject, body_txt)
            except Exception as e:
                ok, err = False, str(e)
            if not ok:
                return self._json(200, {'ok': False, 'error': err})
            # mark as sent + log activity
            est['status'] = 'sent'
            est.setdefault('activities', []).append({'ts': time.time(), 'type': 'email',
                'text': f'Estimate sent to {to}', 'by': 'owner'})
            lead = next((l for l in _crm_leads(c) if l.get('id') == est.get('lead_id')), None)
            if lead:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'email',
                    'text': f'Estimate {est.get("number")} sent to {to}', 'by': 'owner'})
                # auto-move lead to Quote Sent if still at Lead
                if lead.get('stage') == 'Lead':
                    lead['stage'] = 'Quote Sent'
            save_db(db)
            return self._json(200, {'ok': True, 'sent_to': to, 'estimate': est})


        if self.path == '/api/seo/crawl':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            url = str(b.get('url') or '').strip()
            if not url:
                return self._json(400, {'error': 'url required'})
            try:
                result = seo_crawl_site(url, max_pages=int(b.get('max_pages') or 20))
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Crawl failed: {e}'})
            import uuid as _uuid
            result['id'] = _uuid.uuid4().hex[:10]
            crawls = c.setdefault('seo_crawls', [])
            crawls.append(result)
            c['seo_crawls'] = crawls[-10:]
            save_db(db)
            return self._json(200, {'ok': True, 'crawl': result})

        if self.path == '/api/seo/crawl/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            c['seo_crawls'] = [x for x in c.get('seo_crawls', []) if x.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/seo/fixes':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            url = str(b.get('url') or '').strip()
            if not url:
                return self._json(400, {'error': 'url required'})
            try:
                audit = seo_audit_url(url)
                fixes = seo_fix_snippets(audit, business_name=c.get('business', ''), url=audit['url'])
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Could not generate fixes: {e}'})
            return self._json(200, {'ok': True, 'audit': audit, 'fixes': fixes})

        if self.path == '/api/seo/competitor':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            your_url = str(b.get('your_url') or '').strip()
            comp_url = str(b.get('competitor_url') or '').strip()
            if not your_url or not comp_url:
                return self._json(400, {'error': 'your_url and competitor_url required'})
            try:
                you = seo_audit_url(your_url)
                comp = seo_audit_url(comp_url)
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Could not audit: {e}'})
            gaps = []
            comp_checks = {ch['name']: ch for ch in comp['checks']}
            you_checks = {ch['name']: ch for ch in you['checks']}
            for name, ch in comp_checks.items():
                if ch['ok'] and not you_checks.get(name, {}).get('ok'):
                    gaps.append({'check': name, 'competitor': True, 'you': False, 'hint': you_checks.get(name, {}).get('hint', '')})
            content_gaps = []
            for h2 in comp.get('h2', []):
                if h2.lower() not in [x.lower() for x in you.get('h2', [])]:
                    content_gaps.append(h2)
            return self._json(200, {'ok': True, 'you': you, 'competitor': comp,
                                    'gaps': gaps, 'content_gaps': content_gaps[:15]})

        if self.path == '/api/seo/local':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            url = str(b.get('url') or '').strip()
            if not url:
                return self._json(400, {'error': 'url required'})
            try:
                audit = seo_audit_url(url)
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Could not fetch: {e}'})
            # NAP extraction (Name, Address, Phone)
            import re as _re
            phone = _re.findall(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', audit.get('description', '') + ' '.join(audit.get('h2', [])) + ' '.join(audit.get('h1', [])))
            address_hints = _re.findall(r'\d+\s+[A-Z][a-z]+\s+(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Way|Court|Ct)', ' '.join(audit.get('h2', [])) + ' '.join(audit.get('h1', [])))
            local_checks = []
            local_checks.append({'name': 'Business name in title', 'ok': c.get('business', '').lower() in audit.get('title', '').lower() if c.get('business') else False, 'hint': 'Include your business name in the page title.'})
            local_checks.append({'name': 'Phone number visible', 'ok': bool(phone), 'hint': 'Add a click-to-call phone number in the header and footer.'})
            local_checks.append({'name': 'Address on page', 'ok': bool(address_hints), 'hint': 'Display your full address on the homepage and contact page.'})
            local_checks.append({'name': 'LocalBusiness schema', 'ok': audit.get('schema', False), 'hint': 'Add LocalBusiness JSON-LD schema with your address, phone, hours.'})
            local_checks.append({'name': 'Location in title', 'ok': any(x in audit.get('title', '').lower() for x in ['kitchener', 'waterloo', 'cambridge', 'guelph', 'on', 'ontario']), 'hint': 'Include your city/region in the title tag.'})
            local_checks.append({'name': 'Location in H1', 'ok': any(x in ' '.join(audit.get('h1', [])).lower() for x in ['kitchener', 'waterloo', 'cambridge', 'guelph', 'on', 'ontario']), 'hint': 'Mention your service area in the main heading.'})
            nap_score = round(100 * sum(1 for x in local_checks if x['ok']) / len(local_checks))
            citations = [
                {'name': 'Google Business Profile', 'url': 'https://business.google.com', 'status': 'unknown'},
                {'name': 'Yelp', 'url': f'https://www.yelp.ca/search?find_desc={c.get("business", "").replace(" ", "+")}', 'status': 'unknown'},
                {'name': 'YellowPages', 'url': 'https://www.yellowpages.ca', 'status': 'unknown'},
                {'name': 'Houzz', 'url': 'https://www.houzz.com', 'status': 'unknown'},
                {'name': 'Angi', 'url': 'https://www.angi.com', 'status': 'unknown'},
            ]
            local_kws = [f'{kw} {city}' for kw in (b.get('keywords') or ['tree removal', 'tree trimming', 'stump grinding']) for city in ['kitchener', 'waterloo', 'cambridge', 'guelph']]
            return self._json(200, {'ok': True, 'nap_score': nap_score, 'local_checks': local_checks,
                                    'citations': citations, 'local_keywords': local_kws[:20],
                                    'phone_found': phone[:3], 'address_hints': address_hints[:3]})

        if self.path == '/api/seo/content':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            topic = str(b.get('topic') or '').strip()
            keyword = str(b.get('keyword') or '').strip()
            if not topic:
                return self._json(400, {'error': 'topic required'})
            key = c.get('openrouter_key') or ENV.get('OPENROUTER_API_KEY', '')
            if not key:
                return self._json(400, {'error': 'No AI key available'})
            prompt = (
                "Write a complete, SEO-optimized blog post for a local business (" + c.get('business', 'the business') + "). "
                + "Topic: " + topic + ". Target keyword: " + (keyword or topic) + ". "
                + "Write 800–1200 words. Structure: H1 title, 3–5 H2 sections, FAQ section with 3 questions, conclusion with CTA. "
                + "Use the target keyword in the H1, first paragraph, one H2, and naturally throughout. "
                + "Write in a helpful, authoritative tone for homeowners. Include practical tips and local relevance. "
                + "Return STRICT JSON: {\"title\": \"...\", \"h1\": \"...\", \"sections\": [{\"h2\": \"...\", \"body\": \"...\"}], "
                + "\"faq\": [{\"q\": \"...\", \"a\": \"...\"}], \"meta_description\": \"...\", \"word_count\": 0}"
            )
            try:
                req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                    data=json.dumps({'model': 'openai/gpt-4o-mini', 'temperature': 0.5, 'max_tokens': 3000,
                        'messages': [{'role': 'system', 'content': 'You output strict JSON only.'},
                                     {'role': 'user', 'content': prompt}]}).encode(),
                    method='POST', headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=90) as r:
                    resp = json.load(r)
                txt = resp['choices'][0]['message']['content'].strip()
                if txt.startswith('```'):
                    txt = txt.strip('`').lstrip('json').strip()
                content = json.loads(txt)
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Content generation failed: {e}'})
            c.setdefault('seo_content', []).append({'topic': topic, 'keyword': keyword, 'ts': time.time(), 'content': content})
            save_db(db)
            return self._json(200, {'ok': True, 'content': content})

        if self.path == '/api/seo/rankcheck':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            keyword = str(b.get('keyword') or '').strip()
            domain = str(b.get('domain') or '').strip().replace('https://', '').replace('http://', '').split('/')[0]
            if not keyword or not domain:
                return self._json(400, {'error': 'keyword and domain required'})
            # Use Serper.dev free tier if available, else fallback to scraping
            serper_key = ENV.get('SERPER_API_KEY', '')
            position = None
            if serper_key:
                try:
                    req = urllib.request.Request('https://google.serper.dev/search',
                        data=json.dumps({'q': keyword, 'gl': 'ca', 'hl': 'en', 'num': 20}).encode(),
                        method='POST', headers={'X-API-KEY': serper_key, 'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        d = json.load(r)
                    for i, item in enumerate(d.get('organic', [])[:20], 1):
                        if domain in item.get('link', ''):
                            position = i
                            break
                except Exception as e:
                    print('[serper]', e)
            # store position
            for kw in c.get('seo_keywords', []):
                if kw.get('keyword') == keyword:
                    kw.setdefault('positions', []).append({'ts': time.time(), 'pos': position})
                    kw['positions'] = kw['positions'][-30:]
                    break
            save_db(db)
            return self._json(200, {'ok': True, 'keyword': keyword, 'domain': domain, 'position': position,
                                    'checked': time.strftime('%Y-%m-%d %H:%M')})

        if self.path == '/api/seo/backlinks':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            domain = str(b.get('domain') or '').strip().replace('https://', '').replace('http://', '').split('/')[0]
            if not domain:
                return self._json(400, {'error': 'domain required'})
            # OpenLinkProfiler free API (no key needed for basic)
            backlinks = []
            try:
                req = urllib.request.Request(f'https://openlinkprofiler.org/api/backlinks?domain={urllib.parse.quote(domain)}&format=json&limit=50',
                    headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = json.load(r)
                for item in d.get('backlinks', [])[:50]:
                    backlinks.append({'url': item.get('url', ''), 'title': item.get('title', '')[:120],
                                      'anchor': item.get('anchor_text', '')[:100], 'domain': item.get('domain', '')})
            except Exception as e:
                print('[backlinks]', e)
            # Also try ahrefs free backlink checker via scraping
            if not backlinks:
                try:
                    req = urllib.request.Request(f'https://ahrefs.com/backlink-checker/?target={urllib.parse.quote(domain)}',
                        headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=20) as r:
                        raw = r.read(200 * 1024).decode('utf-8', 'replace')
                    import re as _re
                    for m in _re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{5,80})</a>', raw):
                        if domain not in m.group(1) and 'ahrefs' not in m.group(1):
                            backlinks.append({'url': m.group(1), 'title': m.group(2).strip(), 'anchor': '', 'domain': m.group(1).split('/')[2]})
                except Exception as e:
                    print('[ahrefs scrape]', e)
            return self._json(200, {'ok': True, 'domain': domain, 'backlinks': backlinks[:50], 'count': len(backlinks)})

        if self.path == '/api/seo/vitals':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            url = str(b.get('url') or '').strip()
            if not url:
                return self._json(400, {'error': 'url required'})
            # Use Google PageSpeed Insights API (free, no key needed for basic)
            psi_key = ENV.get('PAGESPEED_API_KEY', '')
            try:
                api_url = f'https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url={urllib.parse.quote(url)}&strategy=mobile'
                if psi_key:
                    api_url += f'&key={psi_key}'
                req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.load(r)
                audits = d.get('lighthouseResult', {}).get('audits', {})
                vitals = {
                    'performance_score': round((d.get('lighthouseResult', {}).get('categories', {}).get('performance', {}).get('score') or 0) * 100),
                    'lcp': audits.get('largest-contentful-paint', {}).get('displayValue', 'N/A'),
                    'fid': audits.get('total-blocking-time', {}).get('displayValue', 'N/A'),
                    'cls': audits.get('cumulative-layout-shift', {}).get('displayValue', 'N/A'),
                    'fcp': audits.get('first-contentful-paint', {}).get('displayValue', 'N/A'),
                    'speed_index': audits.get('speed-index', {}).get('displayValue', 'N/A'),
                    'tti': audits.get('interactive', {}).get('displayValue', 'N/A'),
                }
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Vitals check failed: {e}. Try again or add a PageSpeed API key.'})
            return self._json(200, {'ok': True, 'url': url, 'vitals': vitals})

        if self.path == '/api/seo/report':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            # Generate a summary report from latest data
            audits = c.get('seo_audits', [])
            crawls = c.get('seo_crawls', [])
            kws = c.get('seo_keywords', [])
            strategy = c.get('seo_strategy', {})
            latest_audit = sorted(audits, key=lambda a: a.get('ts', 0))[-1] if audits else None
            latest_crawl = sorted(crawls, key=lambda x: x.get('ts', 0))[-1] if crawls else None
            report = {
                'generated': time.strftime('%Y-%m-%d %H:%M'),
                'business': c.get('business', 'Your Business'),
                'latest_audit': latest_audit,
                'latest_crawl': latest_crawl,
                'keywords_tracked': len(kws),
                'keywords_with_positions': sum(1 for k in kws if k.get('positions')),
                'avg_position': round(sum(p['pos'] for k in kws for p in k.get('positions', []) if p.get('pos')) / max(1, sum(1 for k in kws for p in k.get('positions', []) if p.get('pos'))), 1) if any(k.get('positions') for k in kws) else None,
                'strategy': strategy,
                'score_trend': [{'ts': a.get('ts'), 'score': a.get('score')} for a in audits[-10:]],
            }
            return self._json(200, {'ok': True, 'report': report})

        if self.path == '/api/subdomain/save':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            dom = _domain_clean(b.get('domain') or b.get('subdomain') or '')
            if not dom:
                return self._json(400, {'error': 'Domain must be 3–253 chars, letters/numbers/dots/hyphens only'})
            if dom in ('anos.ca', 'app.anos.ca', 'api.anos.ca', 'www.anos.ca'):
                return self._json(400, {'error': 'That domain is reserved'})
            if not _domain_available(db, dom, exclude_email=c['email']):
                return self._json(409, {'error': 'Domain already taken by another account'})
            c['portal_domain'] = dom
            save_db(db)
            # Kick off SSL provisioning in background
            threading.Thread(target=_provision_ssl, args=(dom,), daemon=True).start()
            return self._json(200, {'ok': True, 'domain': dom,
                                    'url': f'https://{dom}/client',
                                    'cname_target': 'app.anos.ca',
                                    'ssl_provisioning': True})

        if self.path == '/api/subdomain/remove':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            c['portal_subdomain'] = ''
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/subdomain/branding':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            branding = c.setdefault('portal_branding', {})
            for f in ('primary_color', 'accent_color', 'bg_color', 'text_color', 'logo_url', 'logo_data', 'font', 'theme'):
                if f == 'theme':
                    if f in b:
                        branding['theme'] = 'light' if b.get('theme') == 'light' else 'dark'
                    continue
                if f in b:
                    if f in ('logo_url', 'logo_data'):
                        v = str(b[f])
                        # uploaded logos arrive as resized data URLs (~<=240px) — allow the size,
                        # but only if it really is an image data URL or a plain link
                        if v.startswith('data:') and not re.match(r'^data:image/(png|jpeg|jpg|webp|gif|svg\+xml);base64,', v[:80]):
                            return self._json(400, {'error': 'logo must be an image (png/jpg/webp/gif/svg)'})
                        branding[f] = v[:600_000]
                    else:
                        branding[f] = str(b[f])[:500]
            save_db(db)
            return self._json(200, {'ok': True, 'branding': branding})

        if self.path == '/api/client/forgot-password':
            # public: request a password reset for a client portal
            b = self._body()
            email = str(b.get('email') or '').lower().strip()
            dom = str(b.get('domain') or '').lower().strip()
            if not email:
                return self._json(400, {'error': 'Email required'})
            # find the customer by domain OR by scanning all customers for matching client_email
            c = None
            db = None
            if dom:
                db2, c2 = _find_customer_by_domain(dom)
                if c2:
                    c = c2
                    db = db2
            if not c:
                db2 = load_db()
                for _, cust in db2.get('customers', {}).items():
                    cp = cust.get('client_portal') or {}
                    if cp.get('client_email', '').lower() == email:
                        c = cust
                        db = db2
                        break
            if not c:
                return self._json(404, {'error': 'No account found for that email'})
            cp = _client_portal(c)
            if not cp.get('enabled'):
                return self._json(404, {'error': 'Client portal not enabled'})
            token = _reset_token(c, email)
            save_db(db)
            reset_url = 'https://' + (dom or 'app.anos.ca') + '/client/reset?token=' + token
            # try to send email via their SMTP, fallback to our Gmail
            subject = 'Password reset — ' + c.get('business', 'Client Portal')
            body = 'Hi ' + cp.get('client_name', 'there') + ',\n\nYou requested a password reset for your client portal.\n\nClick here to set a new password:\n' + reset_url + '\n\nThis link expires in 1 hour.\n\nIf you did not request this, ignore this email.\n\n— ' + c.get('business', 'Your Contractor')
            ok, err = send_client_email(c, email, subject, body)
            if not ok:
                return self._json(200, {'ok': False, 'error': 'Could not send email. Contact support.'})
            return self._json(200, {'ok': True, 'sent_to': email})

        if self.path == '/api/client/reset-password':
            # public: set new password with token
            b = self._body()
            token = str(b.get('token') or '').strip()
            new_pass = str(b.get('password') or '').strip()
            if not token or not new_pass:
                return self._json(400, {'error': 'Token and password required'})
            if len(new_pass) < 6:
                return self._json(400, {'error': 'Password must be at least 6 characters'})
            db = load_db()
            c, email = _validate_reset_token(db, token)
            if not c:
                return self._json(400, {'error': 'Invalid or expired token'})
            cp = _client_portal(c)
            cp['temp_password'] = new_pass
            _mark_reset_used(db, token)
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/clients/invite':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead_ids = b.get('lead_ids') or []
            if not lead_ids:
                return self._json(400, {'error': 'lead_ids required'})
            cp = _client_portal(c)
            if not cp.get('enabled'):
                return self._json(400, {'error': 'Client portal not enabled — turn it on first'})
            dom = c.get('portal_domain') or 'app.anos.ca'
            url = 'https://' + dom + '/client'
            sent = []
            failed = []
            for lid in lead_ids:
                lead = next((l for l in _crm_leads(c) if l.get('id') == lid), None)
                if not lead:
                    failed.append({'id': lid, 'error': 'lead not found'})
                    continue
                email = (lead.get('email') or '').strip()
                if not email:
                    failed.append({'id': lid, 'error': 'no email on lead'})
                    continue
                if not cp.get('temp_password'):
                    cp['temp_password'] = secrets.token_urlsafe(8)
                subject = 'Your project portal — ' + c.get('business', 'Your Contractor')
                body = 'Hi ' + lead.get('client', 'there') + ',\n\nYou can now track your project online.\n\nLogin here: ' + url + '\nEmail: ' + email + '\nPassword: ' + cp['temp_password'] + '\n\nYou will see:\n- Project status and timeline\n- Photos from the job\n- Any documents or estimates\n- Live updates when work is scheduled\n\nQuestions? Reply to this email.\n\n— ' + c.get('business', 'Your Contractor')
                ok, err = send_client_email(c, email, subject, body)
                if ok:
                    sent.append(email)
                    lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'email',
                        'text': 'Portal invite sent to ' + email, 'by': 'owner'})
                else:
                    failed.append({'id': lid, 'error': err or 'send failed'})
            save_db(db)
            return self._json(200, {'ok': True, 'sent': sent, 'failed': failed, 'url': url})

        if self.path == '/api/client/portal/invite':
            # one-click invite for a multi-portal client: email or SMS their link + login
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            pid = str(b.get('portal_id') or '')
            via = b.get('via') or 'email'
            p = next((x for x in _client_portals(c) if x.get('id') == pid), None)
            if not p:
                return self._json(404, {'error': 'portal client not found'})
            dom = c.get('portal_domain') or 'app.anos.ca'
            url = 'https://' + dom + '/client'
            biz = c.get('business', 'Your Contractor')
            creds = 'Login: ' + url + '\nEmail: ' + (p.get('email') or '') + '\nPassword: ' + (p.get('password') or '')
            if via == 'email':
                if not p.get('email'):
                    return self._json(400, {'error': 'This client has no email'})
                subject = 'Your project portal — ' + biz
                body = ('Hi ' + (p.get('name') or 'there') + ',\n\n'
                        'You can now track your project online — status, photos, estimates and invoices, all in one place.\n\n'
                        + creds + '\n\nSave that link — it works on your phone too.\n\n— ' + biz)
                ok, err = send_client_email(c, p['email'], subject, body)
                if not ok:
                    return self._json(400, {'error': err or 'Email failed'})
            elif via == 'sms':
                phone = re.sub(r'[^\d+]', '', str(b.get('phone') or ''))
                if len(phone) < 10:
                    return self._json(400, {'error': 'A valid mobile number is required for a text invite'})
                ok, err = twilio_send(c, phone, biz + ': your project portal is ready — ' + creds.replace('\n', ' · '))
                if not ok:
                    return self._json(400, {'error': err or 'Text failed'})
            else:
                return self._json(400, {'error': 'via must be email or sms'})
            c.setdefault('activity', []).append({'type': 'email', 'ts': time.time(),
                'desc': f"Portal invite ({via}) sent to {p.get('name') or p.get('email')}"})
            props = _client_props_for(c, p)
            if props:
                props[0].setdefault('activities', []).append({'ts': time.time(), 'type': 'email',
                    'by': 'owner', 'text': f"Portal invite sent to {p.get('email')} via {via}"})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/apikeys':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            name = str(b.get('name') or '').strip()[:100]
            if not name:
                return self._json(400, {'error': 'name required'})
            key = 'ak_' + secrets.token_hex(24)
            keys = c.setdefault('api_keys', [])
            keys.append({'key': key, 'name': name, 'created': time.time(), 'revoked': False})
            save_db(db)
            return self._json(200, {'ok': True, 'key': key, 'name': name})

        if self.path == '/api/apikeys/revoke':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            key = str(b.get('key') or '').strip()
            keys = c.setdefault('api_keys', [])
            for k in keys:
                if k.get('key') == key:
                    k['revoked'] = True
                    save_db(db)
                    return self._json(200, {'ok': True})
            return self._json(404, {'error': 'key not found'})

        if self.path == '/api/client/portal/properties':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cp = _client_portal(c)
            if b.get('action') == 'link':
                lead_id = b.get('lead_id')
                if not lead_id:
                    return self._json(400, {'error': 'lead_id required'})
                ids = set(cp.get('property_ids') or [])
                ids.add(lead_id)
                cp['property_ids'] = list(ids)
                save_db(db)
                return self._json(200, {'ok': True, 'properties': _client_properties(c)})
            elif b.get('action') == 'unlink':
                lead_id = b.get('lead_id')
                ids = set(cp.get('property_ids') or [])
                ids.discard(lead_id)
                cp['property_ids'] = list(ids)
                save_db(db)
                return self._json(200, {'ok': True, 'properties': _client_properties(c)})
            return self._json(400, {'error': 'action must be link or unlink'})

        if self.path == '/api/client/portal/reset-password':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            new_pass = str(b.get('password') or '').strip()
            if len(new_pass) < 6:
                return self._json(400, {'error': 'Password must be at least 6 characters'})
            cp = _client_portal(c)
            cp['temp_password'] = new_pass
            save_db(db)
            return self._json(200, {'ok': True, 'password': new_pass})

        if self.path == '/api/client/portals':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            action = b.get('action', 'list')
            portals = _client_portals(c)
            if action == 'list':
                out = []
                for p in portals:
                    props = _client_props_for(c, p)
                    out.append({
                        'id': p.get('id'), 'name': p.get('name'), 'email': p.get('email'),
                        'password': p.get('password'), 'enabled': p.get('enabled', True),
                        'created': p.get('created', 0),
                        'property_count': len(props),
                        'properties': [{'id': l.get('id'), 'address': l.get('address'), 'stage': l.get('stage')} for l in props[:20]],
                    })
                return self._json(200, {'portals': out})
            if action == 'create':
                name = str(b.get('name') or '').strip()
                email = str(b.get('email') or '').lower().strip()
                password = str(b.get('password') or '').strip()
                if not email or not password:
                    return self._json(400, {'error': 'email and password required'})
                if any((p.get('email') or '').lower() == email for p in portals):
                    return self._json(409, {'error': 'A portal with that email already exists'})
                portal = {
                    'id': secrets.token_hex(6), 'name': name, 'email': email, 'password': password,
                    'token': secrets.token_hex(16), 'property_ids': b.get('property_ids') or [],
                    'enabled': True, 'created': time.time(),
                }
                portals.append(portal)
                save_db(db)
                return self._json(200, {'ok': True, 'portal': portal})
            if action == 'update':
                pid = b.get('id')
                portal = next((p for p in portals if p.get('id') == pid), None)
                if not portal:
                    return self._json(404, {'error': 'portal not found'})
                for f in ('name', 'email', 'password', 'enabled'):
                    if f in b:
                        portal[f] = b[f]
                if 'property_ids' in b:
                    portal['property_ids'] = b['property_ids']
                save_db(db)
                return self._json(200, {'ok': True, 'portal': portal})
            if action == 'reset-password':
                pid = b.get('id')
                portal = next((p for p in portals if p.get('id') == pid), None)
                if not portal:
                    return self._json(404, {'error': 'portal not found'})
                new_pass = str(b.get('password') or '').strip()
                if len(new_pass) < 6:
                    return self._json(400, {'error': 'Password must be at least 6 characters'})
                portal['password'] = new_pass
                save_db(db)
                return self._json(200, {'ok': True, 'password': new_pass})
            if action == 'delete':
                pid = b.get('id')
                c['client_portals'] = [p for p in portals if p.get('id') != pid]
                save_db(db)
                return self._json(200, {'ok': True})
            return self._json(400, {'error': 'bad action'})

        if self.path == '/api/brain/upload':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            ct = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in ct:
                return self._json(400, {'error': 'multipart upload required'})
            import re as _re
            boundary = ct.split('boundary=')[-1].strip().encode()
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            parts = raw.split(b'--' + boundary)
            title = ''
            filedata = None
            fname = ''
            for part in parts:
                if b'name="title"' in part:
                    title = part.split(b'\r\n\r\n')[-1].strip().decode('utf-8', 'replace')
                elif b'name="file"' in part:
                    head = part.split(b'\r\n\r\n')[0].decode('utf-8', 'replace')
                    m = _re.search(r'filename="([^"]+)"', head)
                    fname = m.group(1) if m else 'upload'
                    filedata = part.split(b'\r\n\r\n')[-1].rstrip(b'\r\n-')
            if not filedata:
                return self._json(400, {'error': 'no file received'})
            if len(filedata) > 10 * 1024 * 1024:
                return self._json(400, {'error': 'File too large (10MB max)'})
            # extract text based on type
            ext = fname.lower().rsplit('.', 1)[-1] if '.' in fname else ''
            text = ''
            try:
                if ext in ('txt', 'md', 'csv', 'json', 'xml', 'html', 'log'):
                    text = filedata.decode('utf-8', 'replace')[:50000]
                elif ext == 'pdf':
                    # pdftotext if available
                    import subprocess, tempfile, os as _osf2
                    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
                        tf.write(filedata)
                        tpath = tf.name
                    try:
                        out = subprocess.run(['pdftotext', tpath, '-'], capture_output=True, timeout=30)
                        text = out.stdout.decode('utf-8', 'replace')[:50000]
                    finally:
                        _osf2.unlink(tpath)
                else:
                    text = f'[{ext.upper() if ext else "FILE"} file uploaded: {fname}, {len(filedata)} bytes — binary content not extracted]'
            except Exception as e:
                text = f'[Could not extract text from {fname}: {e}]'
            if not text.strip():
                text = f'[Empty or unreadable file: {fname}]'
            # store the file on disk too
            import uuid as _uuid, base64
            from pathlib import Path as _P
            fdir = _P(str(BASE)) / 'uploads' / 'brain' / _re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
            fdir.mkdir(parents=True, exist_ok=True)
            fsave = f'{int(time.time())}_{_uuid.uuid4().hex[:6]}_{_re.sub(r"[^a-zA-Z0-9._-]", "_", fname)}'
            (fdir / fsave).write_bytes(filedata)
            note, nerr = brain.save_note(c, {
                'title': (title or fname)[:200],
                'body': f'📎 File: {fname}\\n\\n{text}',
                'tags': (['file', ext] if ext else ['file']),
            })
            if nerr:
                return self._json(400, {'error': nerr})
            if note is not None:
                note['file_url'] = f'/uploads/brain/{fdir.name}/{fsave}'
                note['file_name'] = fname
                note['file_size'] = len(filedata)
            save_db(db)
            return self._json(200, {'ok': True, 'note': note})

        if self.path == '/api/documents/upload':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            data_url = b.get('file') or ''
            filename = str(b.get('filename') or 'document')[:200]
            doc_type = str(b.get('doc_type') or 'other')[:50]
            note = str(b.get('note') or '')[:500]
            lead_id = b.get('lead_id') or None
            m = re.match(r'^data:([^;]+);base64,(.+)$', data_url, re.S)
            if not m:
                return self._json(400, {'error': 'file must be a data URL'})
            import base64
            raw = base64.b64decode(m.group(2))
            if len(raw) > 15 * 1024 * 1024:
                return self._json(400, {'error': 'File too large (15MB max)'})
            lead = next((l for l in _crm_leads(c) if l.get('id') == lead_id), None) if lead_id else None
            if lead_id and not lead:
                return self._json(404, {'error': 'lead not found'})
            doc = _save_document(c, lead, filename, raw, m.group(1), note, doc_type)
            save_db(db)
            return self._json(200, {'ok': True, 'document': doc})

        if self.path == '/api/documents/sign-link':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            doc = next((d for d in _client_documents(c) if d.get('id') == b.get('id')), None)
            if not doc:
                return self._json(404, {'error': 'document not found'})
            if not doc.get('sign_token'):
                import uuid as _uuid
                doc['sign_token'] = _uuid.uuid4().hex
            doc['sign_status'] = 'sent'
            doc['sign_sent_ts'] = time.time()
            save_db(db)
            base = _public_base(c)
            return self._json(200, {'ok': True, 'url': base + '/sign-doc/' + doc['sign_token']})

        if self.path == '/api/documents/email':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            doc = next((d for d in _client_documents(c) if d.get('id') == b.get('id')), None)
            if not doc:
                return self._json(404, {'error': 'document not found'})
            to = str(b.get('to') or '').strip()[:200]
            if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', to):
                return self._json(400, {'error': 'valid recipient email required'})
            base = _public_base(c)
            biz = (c.get('settings') or {}).get('business_name') or c.get('business') or c.get('email', '')
            if doc.get('sign_status') == 'signed' and doc.get('signed_pdf_url'):
                link = base + doc['signed_pdf_url'] + '?sigtok=' + doc.get('sign_token', '')
                intro = biz + ' has shared the signed document "' + doc.get('name', 'Document') + '" with you.'
                cta = 'View Signed Document'
            else:
                if not doc.get('sign_token'):
                    import uuid as _uu
                    doc['sign_token'] = _uu.uuid4().hex
                    doc['sign_status'] = 'sent'
                    doc['sign_sent_ts'] = time.time()
                    save_db(db)
                link = base + '/sign-doc/' + doc['sign_token']
                intro = biz + ' has sent you "' + doc.get('name', 'Document') + '" to review and sign online.'
                cta = 'Review & Sign'
            note = str(b.get('message') or '').strip()[:500]
            body = intro + '\n\n' + (note + '\n\n' if note else '') + cta + ': ' + link + '\n\n— Sent via ' + biz
            ok, err = send_client_email(c, to, doc.get('name', 'Document') + ' — ' + biz, body)
            if not ok:
                return self._json(502, {'error': err or 'email failed'})
            doc.setdefault('emails_sent', []).append({'to': to, 'ts': time.time()})
            lead = next((l for l in _crm_leads(c) if l.get('id') == doc.get('lead_id')), None)
            if lead is not None:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note', 'text': 'Document "' + doc.get('name', '') + '" emailed to ' + to, 'by': 'owner'})
            save_db(db)
            return self._json(200, {'ok': True})
        if self.path == '/api/clients2/convert':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('lead_id')), None)
            if not lead:
                return self._json(404, {'error': 'lead not found'})
            if lead.get('client_id'):
                return self._json(400, {'error': 'Already converted — this lead is a property of an existing client'})
            cl, is_new = convert_lead_to_client(c, lead)
            save_db(db)
            return self._json(200, {'ok': True, 'client': cl, 'new_client': is_new})

        if self.path == '/api/clients2/add-property':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cl = _client_by_id(c, str(b.get('client_id') or ''))
            if not cl:
                return self._json(404, {'error': 'client not found'})
            addr = str(b.get('address') or '').strip()[:500]
            if not addr:
                return self._json(400, {'error': 'address required'})
            leads = _crm_leads(c)
            import uuid as _u
            lead = {'id': _u.uuid4().hex[:12], 'created': time.time(), 'stage': 'Booked/Approved',
                    'client': cl.get('name', ''), 'address': addr, 'email': cl.get('email', ''),
                    'phone': cl.get('phone', ''), 'client_id': cl['id'], 'prop_status': 'Active',
                    'activities': [{'ts': time.time(), 'type': 'created',
                                    'text': 'Property added to client ' + cl.get('name', ''), 'by': 'owner'}]}
            leads.append(lead)
            save_db(db)
            return self._json(200, {'ok': True, 'property': lead})

        if self.path == '/api/clients2/add':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            name = str(b.get('name') or '').strip()[:200]
            if not name:
                return self._json(400, {'error': 'name required'})
            import uuid as _u
            cl = {'id': _u.uuid4().hex[:12], 'name': name,
                  'email': str(b.get('email') or '').strip()[:200],
                  'phone': str(b.get('phone') or '').strip()[:60],
                  'created': time.time()}
            _clients(c).append(cl)
            save_db(db)
            return self._json(200, {'ok': True, 'client': cl})

        if self.path == '/api/clients2/edit':
            # edit client details (name/email/phone); email change also relinks their portal login
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cl = _client_by_id(c, str(b.get('client_id') or ''))
            if not cl:
                return self._json(404, {'error': 'client not found'})
            old_email = (cl.get('email') or '').lower().strip()
            if 'name' in b:
                nm = str(b['name'] or '').strip()[:200]
                if not nm:
                    return self._json(400, {'error': 'name cannot be blank'})
                cl['name'] = nm
            if 'email' in b:
                cl['email'] = str(b['email'] or '').lower().strip()[:200]
            if 'phone' in b:
                cl['phone'] = str(b['phone'] or '').strip()[:40]
            new_email = (cl.get('email') or '').lower().strip()
            if new_email != old_email and old_email:
                for p in _client_portals(c):
                    if (p.get('email') or '').lower().strip() == old_email:
                        p['email'] = new_email
                c.setdefault('activity', []).append({'type': 'email', 'ts': time.time(),
                    'desc': f"Portal login email updated to {new_email}"})
            save_db(db)
            return self._json(200, {'ok': True, 'client': cl})

        if self.path == '/api/clients2/edit-property':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead or not lead.get('client_id'):
                return self._json(404, {'error': 'property not found'})
            if 'address' in b:
                addr = str(b['address'] or '').strip()[:500]
                if not addr:
                    return self._json(400, {'error': 'address cannot be blank'})
                lead['address'] = addr
            if 'job_date' in b:
                lead['job_date'] = str(b['job_date'] or '')[:30]
            if 'notes' in b:
                lead['notes'] = str(b['notes'] or '')[:1000]
            if 'amount' in b:
                try:
                    lead['amount'] = float(b['amount']) if b['amount'] not in (None, '') else None
                except (TypeError, ValueError):
                    pass
            if b.get('status') in ('Active', 'Completed'):
                lead['prop_status'] = b['status']
                lead['stage'] = 'Job Completed' if b['status'] == 'Completed' else 'Booked/Approved'
            save_db(db)
            return self._json(200, {'ok': True, 'property': lead})

        if self.path == '/api/clients2/delete-property':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead or not lead.get('client_id'):
                return self._json(404, {'error': 'property not found'})
            if b.get('hard'):
                import os as _os
                for p in (lead.get('photos') or []):
                    f = str(BASE) + str(p.get('url', ''))
                    try:
                        _os.remove(f)
                    except Exception:
                        pass
                c['crm_leads'] = [l for l in _crm_leads(c) if l.get('id') != lead['id']]
            else:
                lead.pop('client_id', None)
                lead.pop('prop_status', None)
                lead['stage'] = 'Lead'
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/clients2/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            cl = _client_by_id(c, str(b.get('client_id') or ''))
            if not cl:
                return self._json(404, {'error': 'client not found'})
            for l in _crm_leads(c):
                if l.get('client_id') == cl['id']:
                    l.pop('client_id', None)
                    l.pop('prop_status', None)
            c['clients'] = [x for x in _clients(c) if x.get('id') != cl['id']]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/clients2/prop-status':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            lead = next((l for l in _crm_leads(c) if l.get('id') == b.get('id')), None)
            if not lead or not lead.get('client_id'):
                return self._json(404, {'error': 'property not found'})
            st = str(b.get('status') or '')
            if st not in ('Active', 'Completed'):
                return self._json(400, {'error': 'status must be Active or Completed'})
            lead['prop_status'] = st
            lead['stage'] = 'Job Completed' if st == 'Completed' else 'Booked/Approved'
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/documents/fields':
            # {id, fields: [{page, type: signature|name|date|text, x,y,w,h (0-1 fractions), label}]}
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            doc = next((d for d in _client_documents(c) if d.get('id') == b.get('id')), None)
            if not doc:
                return self._json(404, {'error': 'document not found'})
            fields = b.get('fields') or []
            clean = []
            for f in fields[:100]:
                try:
                    role = str(f.get('role') or 'client')
                    if role not in ('client', 'company'):
                        role = 'client'
                    clean.append({
                        'page': max(1, int(f.get('page', 1))),
                        'type': str(f.get('type', 'signature')) if f.get('type') in ('signature', 'name', 'date', 'text') else 'signature',
                        'role': role,
                        'x': min(1.0, max(0.0, float(f.get('x', 0)))),
                        'y': min(1.0, max(0.0, float(f.get('y', 0)))),
                        'w': min(1.0, max(0.01, float(f.get('w', 0.3)))),
                        'h': min(1.0, max(0.01, float(f.get('h', 0.07)))),
                        'label': str(f.get('label', ''))[:80],
                    })
                except (TypeError, ValueError):
                    continue
            doc['sign_fields'] = clean
            save_db(db)
            return self._json(200, {'ok': True, 'count': len(clean)})

        if self.path == '/api/documents/sign-submit':
            b = self._body()
            token = str(b.get('token') or '')[:64]
            name = str(b.get('name') or '').strip()[:200]
            sig_data = str(b.get('signature') or '')[:200000]
            if not token or not name or not sig_data.startswith('data:image'):
                return self._json(400, {'error': 'name and signature are required'})
            db, c, doc = _find_doc_by_sign_token(token)
            if not doc:
                return self._json(404, {'error': 'invalid or expired signing link'})
            if doc.get('sign_status') == 'signed':
                return self._json(400, {'error': 'already signed'})
            import hashlib
            doc['sign_status'] = 'signed'
            doc['signed_by'] = name
            doc['signed_ts'] = time.time()
            doc['signed_ip'] = self.client_address[0]
            doc['signature_hash'] = hashlib.sha256(sig_data.encode()).hexdigest()
            # field values from the new placed-boxes flow
            submitted_fields = b.get('fields') or []
            if submitted_fields:
                vals = []
                for f in submitted_fields[:100]:
                    try:
                        v = f.get('value') or {}
                        vals.append({
                            'page': int(f.get('page', 1)),
                            'type': str(f.get('type', 'signature')),
                            'x': float(f.get('x', 0)), 'y': float(f.get('y', 0)),
                            'w': float(f.get('w', 0.3)), 'h': float(f.get('h', 0.07)),
                            'text': str(v.get('text', ''))[:300],
                            'has_signature': bool(v.get('dataUrl')),
                        })
                    except (TypeError, ValueError):
                        continue
                doc['signed_fields'] = vals
                # persist each signature image separately too
                try:
                    import base64 as _b64f, os as _osf
                    owner2 = re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
                    sig_dir2 = BASE / 'uploads' / owner2 / 'docs'
                    _osf.makedirs(sig_dir2, exist_ok=True)
                    for i, f in enumerate(submitted_fields[:100]):
                        dv = ((f.get('value') or {}).get('dataUrl') or '')
                        mm = re.match(r'^data:image/\w+;base64,(.+)$', dv, re.S)
                        if mm:
                            with open(sig_dir2 / (doc['id'] + f'_sig_{i}.png'), 'wb') as fh:
                                fh.write(_b64f.b64decode(mm.group(1)))
                except Exception as _e2:
                    print(f'[sign] extra sig save failed: {_e2!r}')
                stamped = stamp_signed_pdf(c, doc, submitted_fields)
                if stamped:
                    doc['signed_pdf_url'] = stamped
            # save signature image alongside the document
            try:
                import base64 as _b64
                m = re.match(r'^data:image/\w+;base64,(.+)$', sig_data, re.S)
                if m:
                    import os as _os
                    raw = _b64.b64decode(m.group(1))
                    owner = re.sub(r'[^a-z0-9@._-]', '', c.get('email', 'unknown'))
                    sig_dir = BASE / 'uploads' / owner / 'docs'
                    _os.makedirs(sig_dir, exist_ok=True)
                    sig_path = sig_dir / (doc['id'] + '_sig.png')
                    with open(sig_path, 'wb') as fh:
                        fh.write(raw)
                    doc['signature_file'] = str(sig_path)
            except Exception as _sig_err:
                print(f'[sign] signature image save failed: {_sig_err!r}')
            # timeline entry on the lead
            lead = next((l for l in _crm_leads(c) if l.get('id') == doc.get('lead_id')), None)
            if lead is not None:
                lead.setdefault('activities', []).append({'ts': time.time(), 'type': 'note', 'text': 'Document "' + doc.get('name', '') + '" signed by ' + name, 'by': 'client'})
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/documents/delete':
            db, c, member = self._customer_or_api(scope='documents')
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            docs = _client_documents(c)
            c['client_documents'] = [d for d in docs if d.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/smtp/save':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            s = dict(c.get('smtp') or {})
            for f in ('host', 'user', 'from_email', 'from_name', 'reply_to'):
                if f in b:
                    s[f] = str(b[f] or '').strip()[:300]
            if 'port' in b:
                try: s['port'] = int(b['port'])
                except Exception: s['port'] = 587
            if 'security' in b:
                s['security'] = 'ssl' if str(b['security']).lower() == 'ssl' else 'tls'
            if b.get('password'):
                s['password'] = str(b['password'])[:300]
            # derive domain from from_email for display
            if s.get('from_email') and '@' in s['from_email']:
                s['domain'] = s['from_email'].split('@', 1)[1]
            c['smtp'] = s
            save_db(db)
            return self._json(200, {'ok': True, 'configured': bool(smtp_conf(c))})

        if self.path == '/api/smtp/test':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            if not smtp_conf(c):
                return self._json(400, {'error': 'Save your SMTP settings first'})
            b = self._body()
            to = str(b.get('to') or c.get('email') or '').strip()
            ok, err = smtp_send(c, to, 'ANOSagent SMTP test',
                'This is a test email sent from your custom domain via ANOSagent. If you got this, your SMTP settings are working.')
            if ok:
                return self._json(200, {'ok': True, 'sent_to': to})
            return self._json(200, {'ok': False, 'error': err})

        if self.path == '/api/smtp/disconnect':
            db, c, member = self._customer_or_api()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            c['smtp'] = {}
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/seo/audit':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            url = str(b.get('url') or '').strip()
            if not url:
                return self._json(400, {'error': 'url required'})
            try:
                audit = seo_audit_url(url)
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'Could not fetch that URL: {e}'})
            import uuid as _uuid
            audit['id'] = _uuid.uuid4().hex[:10]
            audits = c.setdefault('seo_audits', [])
            audits.append(audit)
            c['seo_audits'] = audits[-25:]
            save_db(db)
            return self._json(200, {'ok': True, 'audit': audit})

        if self.path == '/api/seo/audit/delete':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            audits = c.setdefault('seo_audits', [])
            c['seo_audits'] = [a for a in audits if a.get('id') != b.get('id')]
            save_db(db)
            return self._json(200, {'ok': True})

        if self.path == '/api/seo/keywords':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            b = self._body()
            kws = c.setdefault('seo_keywords', [])
            if b.get('action') == 'delete':
                c['seo_keywords'] = [k for k in kws if k.get('keyword') != b.get('keyword')]
            else:
                kw = str(b.get('keyword') or '').strip().lower()
                if not kw:
                    return self._json(400, {'error': 'keyword required'})
                if not any(k.get('keyword') == kw for k in kws):
                    kws.append({'keyword': kw, 'target_url': str(b.get('target_url') or '')[:300],
                                'added': time.time(), 'notes': str(b.get('notes') or '')[:300]})
            save_db(db)
            return self._json(200, {'ok': True, 'keywords': c['seo_keywords']})

        if self.path == '/api/seo/strategy':
            db, c, member = self._customer()
            if not c:
                return self._json(401, {'error': 'not logged in'})
            audits = c.get('seo_audits') or []
            if not audits:
                return self._json(400, {'error': 'Run an audit first so the strategy is based on real data.'})
            latest = sorted(audits, key=lambda a: a.get('ts', 0))[-1]
            kws = c.get('seo_keywords') or []
            key = c.get('openrouter_key') or ENV.get('OPENROUTER_API_KEY', '')
            if not key:
                return self._json(400, {'error': 'No AI key available'})
            failed = [ch for ch in latest.get('checks', []) if not ch.get('ok')]
            prompt = (
                f"You are an SEO strategist for a small local business ({c.get('business', 'the business')}, "
                f"type: {c.get('company_type', 'general')}). Their site {latest.get('url')} scored {latest.get('score')}/100 "
                f"on an on-page audit. Title: {latest.get('title')!r}. Description: {latest.get('description')!r}. "
                f"Failing checks: {json.dumps([ch['name'] + ' — ' + ch['hint'] for ch in failed])}. "
                f"Tracked keywords: {json.dumps([k['keyword'] for k in kws])}.\n\n"
                "Write a practical 30-day SEO action plan for a non-technical owner. "
                "Return STRICT JSON only, no markdown, in this exact shape: "
                '{"summary": "2-3 sentence overview", "quick_wins": [{"title": "...", "why": "...", "effort": "low|medium|high"}], '
                '"content_plan": [{"week": 1, "topic": "...", "keyword": "..."}], '
                '"keywords_to_target": [{"keyword": "...", "intent": "...", "difficulty": "low|medium|high"}], '
                '"technical_fixes": [{"fix": "...", "impact": "..."}]}. '
                "5 quick wins, 4 weeks of content, 8 keywords, one technical fix per failing check."
            )
            try:
                req = urllib.request.Request('https://openrouter.ai/api/v1/chat/completions',
                    data=json.dumps({'model': 'openai/gpt-4o-mini', 'temperature': 0.4,
                        'messages': [{'role': 'system', 'content': 'You output strict JSON only.'},
                                     {'role': 'user', 'content': prompt}]}).encode(),
                    method='POST', headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.load(r)
                txt = resp['choices'][0]['message']['content'].strip()
                if txt.startswith('```'):
                    txt = txt.strip('`').lstrip('json').strip()
                strategy = json.loads(txt)
            except Exception as e:
                return self._json(200, {'ok': False, 'error': f'AI strategy failed: {e}'})
            c['seo_strategy'] = {'ts': time.time(), 'based_on': latest.get('url'), **strategy}
            save_db(db)
            return self._json(200, {'ok': True, 'strategy': c['seo_strategy']})

        self._json(404, {'error': 'not found'})

    def log_message(self, *a):
        pass


# ---- Social auto-poster: checks every minute for due scheduled posts ----
# Manual social workflow (Nov 4): posts are NEVER auto-marked posted — the owner
# copies the text, downloads the photo, posts it themselves, then clicks 'Post Now'.
# (The old _social_auto_poster thread flipped scheduled posts to 'posted' on a timer;
# removed at Jacob's request — no automatic posting of any kind.)


# ---- SSL retry cron: every hour, provision certs for domains that don't have them ----
def _ssl_retry_cron():
    while True:
        try:
            db = load_db()
            for email, c in db.get('customers', {}).items():
                dom = c.get('portal_domain') or ''
                if not dom:
                    continue
                try:
                    import subprocess
                    result = subprocess.run(['certbot', 'certificates', '--cert-name', dom],
                                          capture_output=True, text=True, timeout=10)
                    if 'Certificate Name' not in result.stdout:
                        print(f'[ssl cron] retrying cert for {dom}')
                        _provision_ssl(dom)
                except Exception as e:
                    print(f'[ssl cron] error checking {dom}: {e}')
        except Exception as e:
            print(f'[ssl cron] {e}')
        time.sleep(3600)

threading.Thread(target=_ssl_retry_cron, daemon=True).start()

def _add_months_ts(ts, months, day=None):
    """Add calendar months to a timestamp, clamping to month end. Returns noon UTC."""
    from datetime import datetime, timezone
    import calendar as _cal
    base = datetime.fromtimestamp(ts, timezone.utc)
    total = (base.year * 12 + (base.month - 1)) + months
    y = total // 12
    m = total % 12 + 1
    d = min(day or base.day, _cal.monthrange(y, m)[1])
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc).timestamp()

def _next_weekly_ts(weekday, after_ts):
    """Next occurrence of weekday (0=Mon) at noon UTC after after_ts."""
    from datetime import datetime, timezone, timedelta
    try:
        weekday = int(weekday)
    except (TypeError, ValueError):
        weekday = 0
    weekday = max(0, min(6, weekday))
    base = datetime.fromtimestamp(after_ts, timezone.utc)
    cand = datetime(base.year, base.month, base.day, 12, 0, tzinfo=timezone.utc)
    for _ in range(10):
        if cand.weekday() == weekday and cand.timestamp() > after_ts:
            return cand.timestamp()
        cand += timedelta(days=1)
    return (base + timedelta(days=7)).timestamp()

def _next_monthly_ts(day, after_ts):
    """Next occurrence of day-of-month at noon UTC after after_ts (clamped)."""
    from datetime import datetime, timezone
    import calendar as _cal
    try:
        day = int(day)
    except (TypeError, ValueError):
        day = 1
    day = max(1, min(31, day))
    base = datetime.fromtimestamp(after_ts, timezone.utc)
    for add in range(0, 48):
        total = (base.year * 12 + (base.month - 1)) + add
        y = total // 12
        m = total % 12 + 1
        d = min(day, _cal.monthrange(y, m)[1])
        cand = datetime(y, m, d, 12, 0, tzinfo=timezone.utc).timestamp()
        if cand > after_ts:
            return cand
    return _add_months_ts(after_ts, 1, day)

def _advance_recurring_ts(rec, from_ts):
    interval = rec.get('interval')
    if interval == 'week':
        return _next_weekly_ts(rec.get('day'), from_ts)
    if interval == 'month':
        return _next_monthly_ts(rec.get('day'), from_ts)
    days = {'quarter': 91, 'year': 365}.get(interval, 30)
    return from_ts + days * 86400

def _invoice_balance(inv):
    """Outstanding balance on a client invoice."""
    paid = 0.0
    for p in (inv.get('payments') or []):
        try:
            paid += float(p.get('amount') or 0)
        except (TypeError, ValueError):
            pass
    return max(0.0, round(float(inv.get('total') or 0) - paid, 2))

def _find_invoice_by_pay_token(db, token):
    for em, c in db.get('customers', {}).items():
        for inv in _client_invoices(c):
            if inv.get('pay_token') == token:
                return c, inv
    return None, None

def _sign_doc_page(token):
    t = token.replace("'", "").replace('"', '')
    try:
        html = (BASE / 'sign_page.html').read_text()
    except Exception:
        return '<html><body style="background:#0b0d10;color:#fff;font-family:sans-serif;padding:40px;text-align:center">Signing page missing — contact support.</body></html>'
    return html.replace('__TOKEN__', t)

def _auto_reminder_sweep():
    while True:
        try:
            time.sleep(6 * 3600)
            db = load_db()
            now = time.time()
            for em, c in db['customers'].items():
                s = (c.get('invoice_settings') or {})
                has_recurring = any((iv.get('recurring') or {}).get('interval') for iv in _client_invoices(c))
                has_scheduled = any(iv.get('status') == 'scheduled' and iv.get('send_ts') for iv in _client_invoices(c))
                if not s.get('auto_reminders') and not has_recurring and not has_scheduled:
                    continue
                changed = False
                for inv in _client_invoices(c):
                    if not s.get('auto_reminders') or inv.get('status') != 'sent':
                        continue
                    due = inv.get('due_ts') or 0
                    if not due:
                        continue
                    day = 86400
                    marks = inv.setdefault('auto_marks', {})
                    want, key = None, None
                    if due - 3 * day <= now < due - 2 * day and not marks.get('pre3'):
                        want, key = '3 days before due', 'pre3'
                    elif due <= now < due + day and not marks.get('due'):
                        want, key = 'due today', 'due'
                    elif due + 7 * day <= now and not marks.get('over7'):
                        want, key = '7 days overdue', 'over7'
                    if not want:
                        continue
                    ce = ''
                    cp = _client_portal(c)
                    ce = cp.get('client_email', '')
                    if not ce and inv.get('client_key'):
                        l = next((x for x in _crm_leads(c) if x.get('id') == inv['client_key']), None)
                        ce = (l or {}).get('email', '')
                    if not ce:
                        marks[key] = int(now)
                        changed = True
                        continue
                    biz = c.get('business', 'Your contractor')
                    body = ('Hi,\n\nFriendly reminder that invoice %s for $%.2f is %s.\n\n'
                            'You can view it anytime in your client portal.\n\n— %s'
                            % (inv.get('number', ''), inv.get('total', 0), want, biz))
                    try:
                        send_client_email(c, ce, 'Reminder: invoice %s — %s' % (inv.get('number', ''), want), body)
                        print('[auto-remind] sent', inv.get('number'), want, '->', ce, flush=True)
                    except Exception as e:
                        print('[auto-remind] failed', inv.get('number'), e, flush=True)
                    marks[key] = int(now)
                    inv['reminders'] = inv.get('reminders', 0) + 1
                    inv['last_reminded'] = now
                    changed = True
                # scheduled invoices: release to the client when send_ts arrives
                now2 = time.time()
                for inv in _client_invoices(c):
                    if inv.get('status') != 'scheduled' or not inv.get('send_ts'):
                        continue
                    if inv['send_ts'] > now2:
                        continue
                    inv['status'] = 'sent'
                    inv['sent_ts'] = now2
                    changed = True
                    ce = ''
                    cp = _client_portal(c)
                    lead = None
                    if inv.get('client_key'):
                        lead = next((x for x in _crm_leads(c) if x.get('id') == inv['client_key']), None)
                        ce = (lead or {}).get('email', '')
                    if not ce:
                        ce = cp.get('client_email', '')
                    if ce:
                        biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'Your contractor')
                        base = _public_base(c)
                        name = (lead or {}).get('client') or cp.get('client_name') or 'there'
                        due_lbl = time.strftime('%b %d, %Y', time.localtime(inv.get('due_ts', now2)))
                        body = (f"Hi {name},\n\n{biz} has issued invoice {inv.get('number', '')} for ${inv.get('total', 0):,.2f} — due {due_lbl}.\n\n"
                                f"You can view and pay it in your client portal:\n{base}/client\n\n— {biz}")
                        try:
                            send_client_email(c, ce, 'Invoice ' + inv.get('number', '') + ' from ' + biz, body)
                            print('[scheduled] sent', inv.get('number'), '->', ce, flush=True)
                        except Exception as e:
                            print('[scheduled] email failed', inv.get('number'), e, flush=True)
                    c.setdefault('activity', []).append({'type': 'quote', 'ts': now2,
                        'desc': f"Scheduled invoice {inv.get('number', '')} sent to client"})
                    print('[scheduled] released', inv.get('number'), flush=True)
                # recurring invoices: generate + send on schedule (runs even when auto reminders are off)
                for inv in _client_invoices(c):
                    rec = inv.get('recurring') or {}
                    if not rec.get('interval'):
                        continue
                    nxt = rec.get('next_ts', 0)
                    if not nxt or nxt > now2:
                        continue
                    until = rec.get('until_ts') or 0
                    if until and nxt > until:
                        continue
                    import uuid as _u2
                    dup = dict(inv)
                    dup['id'] = _u2.uuid4().hex[:10]
                    dup['number'] = 'INV-%03d' % (c.get('inv_counter', 0) + 1)
                    c['inv_counter'] = c.get('inv_counter', 0) + 1
                    dup['created'] = now2
                    dup['status'] = 'sent'
                    dup['payments'] = []
                    dup['reminders'] = 0
                    dup['last_reminded'] = 0
                    dup.pop('auto_marks', None)
                    dup['due_ts'] = now2 + (rec.get('due_days') or 14) * 86400
                    dup.pop('recurring', None)
                    _client_invoices(c).append(dup)
                    rec['sent_count'] = int(rec.get('sent_count') or 0) + 1
                    rec['next_ts'] = _advance_recurring_ts(rec, nxt)
                    if until and rec['next_ts'] > until:
                        rec['done'] = True
                    changed = True
                    # email the new invoice
                    ce = ''
                    cp = _client_portal(c)
                    lead = None
                    if dup.get('client_key'):
                        lead = next((x for x in _crm_leads(c) if x.get('id') == dup['client_key']), None)
                        ce = (lead or {}).get('email', '')
                    if not ce:
                        ce = cp.get('client_email', '')
                    if ce:
                        biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'Your contractor')
                        base = _public_base(c)
                        name = (lead or {}).get('client') or cp.get('client_name') or 'there'
                        due_lbl = time.strftime('%b %d, %Y', time.localtime(dup['due_ts']))
                        body = (f"Hi {name},\n\n{biz} has issued invoice {dup['number']} for ${dup.get('total', 0):,.2f} — due {due_lbl}.\n\n"
                                f"You can view and pay it in your client portal:\n{base}/client\n\n— {biz}")
                        try:
                            send_client_email(c, ce, 'Invoice ' + dup['number'] + ' from ' + biz, body)
                            print('[recurring] sent', dup['number'], '->', ce, flush=True)
                        except Exception as e:
                            print('[recurring] email failed', dup['number'], e, flush=True)
                    print('[recurring] generated', dup['number'], flush=True)
                save_db(db)
        except Exception as e:
            print('[auto-remind] sweep error:', e, flush=True)

def _daily_digest_sweep():
    """7am ET: email each owner a day-at-a-glance digest (due tasks, stale quotes,
    outstanding invoices). Once per day per customer; skips quiet days."""
    import datetime
    while True:
        try:
            time.sleep(1800)
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            try:
                from zoneinfo import ZoneInfo
                et = now_utc.astimezone(ZoneInfo('America/Toronto'))
            except Exception:
                et = now_utc.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
            if et.hour != 7:
                continue
            today = et.date().isoformat()
            db = load_db()
            now = time.time()
            changed_any = False
            for em, c in db['customers'].items():
                if c.get('digest_last') == today:
                    continue
                if (c.get('settings') or {}).get('digest') is False:
                    continue
                try:
                    today_s = today
                    tasks = [t for t in (c.get('tasks') or []) if t.get('status') != 'done' and t.get('due') and t['due'] <= today_s]
                    overdue = [t for t in tasks if t['due'] < today_s]
                    stale_q = [l for l in _crm_leads(c)
                               if l.get('stage') == 'Quote Sent' and now - (l.get('updated') or l.get('created') or now) > 3 * 86400]
                    out_invs = [i for i in _client_invoices(c) if i.get('status') == 'sent']
                    out_total = sum(i.get('total', 0) for i in out_invs)
                    new_leads = [l for l in _crm_leads(c) if l.get('stage') == 'Lead']
                    if not (tasks or stale_q or out_invs or new_leads):
                        c['digest_last'] = today
                        changed_any = True
                        continue
                    biz = (c.get('settings') or {}).get('business_name') or c.get('business', 'your business')
                    lines = [f'Morning! Here\'s your {biz} snapshot for today:', '']
                    if overdue:
                        lines.append(f'⚠ {len(overdue)} overdue task' + ('s' if len(overdue) != 1 else '') + ':')
                        for t in overdue[:5]:
                            lines.append('  • ' + (t.get('title') or 'Task') + f' (due {t["due"]})')
                    due_today = [t for t in tasks if t['due'] == today_s]
                    if due_today:
                        lines.append(f'📋 {len(due_today)} task' + ('s' if len(due_today) != 1 else '') + ' due today:')
                        for t in due_today[:5]:
                            lines.append('  • ' + (t.get('title') or 'Task'))
                    if stale_q:
                        lines.append(f'📄 {len(stale_q)} quote' + ('s' if len(stale_q) != 1 else '') + ' waiting 3+ days (a nudge doubles close rates):')
                        for l in stale_q[:5]:
                            lines.append('  • ' + (l.get('client') or l.get('title') or 'Lead') + (f' — ${l["amount"]:,.0f}' if l.get('amount') else ''))
                    if out_invs:
                        lines.append(f'💰 {len(out_invs)} outstanding invoice' + ('s' if len(out_invs) != 1 else '') + f' totalling ${out_total:,.2f}')
                    if new_leads:
                        lines.append(f'🌱 {len(new_leads)} lead' + ('s' if len(new_leads) != 1 else '') + ' waiting for a quote')
                    lines += ['', 'Open your portal: https://app.anos.ca', '— ANOS']
                    send_client_email(c, em, f'Your day at a glance — {biz}', '\n'.join(lines))
                    c['digest_last'] = today
                    changed_any = True
                    print('[digest] sent ->', em, flush=True)
                except Exception as e:
                    print('[digest] failed for', em, e, flush=True)
            if changed_any:
                save_db(db)
        except Exception as e:
            print('[digest] sweep error:', e, flush=True)

if __name__ == '__main__':
    threading.Thread(target=_auto_reminder_sweep, daemon=True).start()
    threading.Thread(target=_daily_digest_sweep, daemon=True).start()
    port = int(os.environ.get('PORT', '8789'))
    print(f'Client portal on :{port} | management key: {"yes" if MGMT_KEY else "NO (read-only dev mode)"}', flush=True)
    HTTPServer(('0.0.0.0', port), H).serve_forever()


