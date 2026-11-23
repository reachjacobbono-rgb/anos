#!/usr/bin/env python3
"""
Stripe Connect for ANOSagent contractors.
Contractor connects their own Stripe account (Express), builds subscription
plans, sends clients a signup link; Stripe bills the client's card monthly
directly into the contractor's bank. ANOSagent takes no cut for now
(application_feePercent hook left in place).
"""
import json, secrets, time, urllib.parse


def _connect(c):
    """Per-contractor Connect state on the customer record."""
    return c.setdefault('connect', {'account_id': '', 'charges_enabled': False,
                                    'details_submitted': False, 'created': 0})


def _plans(c):
    return c.setdefault('sub_plans', [])


def _subs(c):
    return c.setdefault('sub_subscriptions', [])


# ---------------- account onboarding ----------------

def acct_status(stripe_request, c):
    """Return connect state, refreshing from Stripe when an account exists."""
    st = _connect(c)
    if st.get('account_id'):
        try:
            a = stripe_request('GET', f"/accounts/{st['account_id']}")
            st['charges_enabled'] = bool(a.get('charges_enabled'))
            st['details_submitted'] = bool(a.get('details_submitted'))
            st['payouts_enabled'] = bool(a.get('payouts_enabled'))
        except Exception:
            pass
    return {'connected': bool(st.get('account_id')),
            'charges_enabled': st.get('charges_enabled', False),
            'details_submitted': st.get('details_submitted', False),
            'payouts_enabled': st.get('payouts_enabled', False)}


def acct_onboard(stripe_request, c, base_url):
    """Create (or reuse) an Express account + return the onboarding URL."""
    st = _connect(c)
    if not st.get('account_id'):
        acct = stripe_request('POST', '/accounts', {
            'type': 'express',
            'email': c['email'],
            'business_type': 'individual',
            'capabilities[card_payments][requested]': 'true',
            'capabilities[transfers][requested]': 'true',
            'business_profile[mcc]': '0780',          # landscaping/horticultural
            'business_profile[url]': base_url,
            'metadata[anos_email]': c['email'],
        })
        st['account_id'] = acct['id']
        st['created'] = int(time.time())
    link = stripe_request('POST', '/account_links', {
        'account': st['account_id'],
        'refresh_url': base_url + '/?connect=refresh',
        'return_url': base_url + '/?connect=done',
        'type': 'account_onboarding',
    })
    return {'ok': True, 'url': link['url']}


# ---------------- subscription plans ----------------

def plan_save(c, b):
    name = (b.get('name') or '').strip()[:100]
    try:
        amount = round(float(b.get('amount') or 0), 2)
    except (TypeError, ValueError):
        amount = 0
    if not name or amount <= 0:
        return None, 'Plan needs a name and a monthly amount'
    interval = b.get('interval') if b.get('interval') in ('month', 'year', 'week') else 'month'
    plans = _plans(c)
    pid = b.get('id')
    if pid:
        p = next((x for x in plans if x.get('id') == pid), None)
        if not p:
            return None, 'plan not found'
        p.update({'name': name, 'amount': amount, 'interval': interval,
                  'desc': (b.get('desc') or '')[:300]})
    else:
        p = {'id': secrets.token_hex(8), 'name': name, 'amount': amount,
             'interval': interval, 'desc': (b.get('desc') or '')[:300],
             'token': secrets.token_urlsafe(12), 'created': int(time.time()),
             'active': True}
        plans.append(p)
    return p, None


def plan_delete(c, pid):
    plans = _plans(c)
    before = len(plans)
    c['sub_plans'] = [p for p in plans if p.get('id') != pid]
    return len(c['sub_plans']) < before


# ---------------- client checkout (public) ----------------

def public_plan(db, token):
    """Find (customer, plan) by public signup token."""
    for email, c in db['customers'].items():
        for p in _plans(c):
            if p.get('token') == token and p.get('active', True):
                return c, p
    return None, None


def checkout_plan(stripe_request, c, plan, base_url):
    """Create a Checkout Session in subscription mode, on the contractor's account."""
    st = _connect(c)
    if not (st.get('account_id') and st.get('charges_enabled')):
        return None, 'Stripe account not ready'
    # create a price on the connected account on the fly (cached on the plan)
    if not plan.get('stripe_price_id'):
        prod = stripe_request('POST', '/products', {
            'name': plan['name'],
            'metadata[anos_plan]': plan['id'],
        }, on_account=st['account_id'])
        price = stripe_request('POST', '/prices', {
            'product': prod['id'],
            'unit_amount': int(round(plan['amount'] * 100)),
            'currency': 'cad',
            'recurring[interval]': plan.get('interval', 'month'),
        }, on_account=st['account_id'])
        plan['stripe_price_id'] = price['id']
    sess = stripe_request('POST', '/checkout/sessions', {
        'mode': 'subscription',
        'line_items[0][price]': plan['stripe_price_id'],
        'line_items[0][quantity]': 1,
        'success_url': base_url + '/subscribe/' + plan['token'] + '?done=1',
        'cancel_url': base_url + '/subscribe/' + plan['token'],
        'metadata[anos_plan]': plan['id'],
        'metadata[anos_owner]': c['email'],
        'subscription_data[metadata][anos_plan]': plan['id'],
        'subscription_data[metadata][anos_owner]': c['email'],
    }, on_account=st['account_id'])
    return sess['url'], None


# ---------------- webhook (Connect events hit the same endpoint) ----------------

def handle_webhook(db, save_db, etype, obj, log_activity=None):
    """Handle subscription lifecycle events for contractor client subs.
    Returns True if the event was ours (matched a customer)."""
    meta = obj.get('metadata', {}) or {}
    owner_email = (meta.get('anos_owner') or '').lower().strip()
    plan_id = meta.get('anos_plan')
    c = db['customers'].get(owner_email) if owner_email else None

    if etype == 'checkout.session.completed' and plan_id and c:
        subs = _subs(c)
        plan = next((p for p in _plans(c) if p.get('id') == plan_id), None)
        sub_id = obj.get('subscription')
        if plan and sub_id and not any(s.get('stripe_sub_id') == sub_id for s in subs):
            subs.append({
                'id': secrets.token_hex(8),
                'stripe_sub_id': sub_id,
                'stripe_customer': obj.get('customer'),
                'plan_id': plan_id,
                'plan_name': plan['name'],
                'amount': plan['amount'],
                'interval': plan.get('interval', 'month'),
                'client_email': obj.get('customer_email') or obj.get('customer_details', {}).get('email', ''),
                'client_name': (obj.get('customer_details') or {}).get('name', ''),
                'status': 'active',
                'created': int(time.time()),
            })
            c.setdefault('activity', []).append({'type': 'book', 'ts': time.time(),
                'desc': f"New subscription: {plan['name']} (${plan['amount']:,.0f}/{plan.get('interval','month')}) — {obj.get('customer_email','client')}"})
            save_db(db)
        return True

    if etype in ('customer.subscription.updated', 'customer.subscription.deleted'):
        sub_id = obj.get('id')
        for ce, cust in db['customers'].items():
            hit = next((s for s in _subs(cust) if s.get('stripe_sub_id') == sub_id), None)
            if hit:
                hit['status'] = 'active' if obj.get('status') == 'active' else obj.get('status', 'canceled')
                if obj.get('current_period_end'):
                    hit['next_charge'] = obj['current_period_end']
                save_db(db)
                return True
        return False

    if etype == 'invoice.payment_failed':
        sub_id = obj.get('subscription')
        for ce, cust in db['customers'].items():
            hit = next((s for s in _subs(cust) if s.get('stripe_sub_id') == sub_id), None)
            if hit:
                hit['status'] = 'past_due'
                cust.setdefault('activity', []).append({'type': 'follow', 'ts': time.time(),
                    'desc': f"⚠ Payment failed: {hit.get('plan_name')} — {hit.get('client_email')}"})
                save_db(db)
                return True
        return False

    return bool(c)


def sub_cancel(stripe_request, c, sid):
    st = _connect(c)
    sub = next((s for s in _subs(c) if s.get('id') == sid), None)
    if not sub:
        return None, 'subscription not found'
    try:
        stripe_request('POST', f"/subscriptions/{sub['stripe_sub_id']}",
                       {'cancel_at_period_end': 'true'}, on_account=st['account_id'])
        sub['status'] = 'canceling'
        return sub, None
    except Exception as e:
        return None, str(e)


def summary(c):
    subs = _subs(c)
    active = [s for s in subs if s.get('status') == 'active']
    mrr = sum(s.get('amount', 0) for s in active if s.get('interval') == 'month')
    return {'plans': _plans(c), 'subscriptions': subs,
            'active_count': len(active), 'mrr': round(mrr, 2)}


def signup_page(c, plan, base_url, done=False):  # noqa: C901
    """Public client signup page (no login)."""
    biz = c.get('business') or c.get('name') or 'Your contractor'
    per = {'month': '/mo', 'year': '/yr', 'week': '/wk'}.get(plan.get('interval'), '/mo')
    desc = plan.get('desc') or f"Monthly service plan from {biz}."
    body_done = f"""
      <h1>You're subscribed ✓</h1>
      <p class="sub">{biz} has your card on file — billing runs automatically. Receipts come from Stripe by email.</p>
      <div class="card"><div class="pname">{plan['name']}</div>
      <div class="amt">${plan['amount']:,.2f}<span>{per}</span></div></div>"""
    body_form = f"""
      <h1>{plan['name']}</h1>
      <p class="sub">{desc}</p>
      <div class="card">
        <div class="amt">${plan['amount']:,.2f}<span>{per}</span></div>
        <div class="fine">Billed automatically to your card · cancel any time by contacting {biz}</div>
        <button id="go" onclick="go()">Subscribe with card →</button>
        <div class="err" id="err"></div>
      </div>
      <div class="secure">🔒 Secure payment by Stripe — {biz} never sees your card number.</div>"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{plan['name']} — {biz}</title>
<style>
 body {{ font-family: Inter, -apple-system, 'Segoe UI', Roboto, sans-serif; background:#080a10; color:#f0f2f7; margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }}
 .wrap {{ max-width:400px; width:100%; text-align:center; }}
 h1 {{ font-size:1.5rem; font-weight:800; letter-spacing:-.02em; }}
 .sub {{ color:#8b93a3; font-size:.88rem; margin:8px 0 22px; line-height:1.5; }}
 .card {{ background:#131824; border:1px solid #232c3d; border-radius:16px; padding:26px 24px; box-shadow:0 10px 28px rgba(0,0,0,.3); }}
 .amt {{ font-size:2.2rem; font-weight:800; }}
 .amt span {{ font-size:.9rem; color:#5c6577; font-weight:600; }}
 .pname {{ font-weight:700; font-size:1.05rem; margin-bottom:6px; }}
 .fine {{ color:#5c6577; font-size:.74rem; margin:8px 0 18px; }}
 button {{ width:100%; background:linear-gradient(135deg,#fff,#b8b8b8); border:none; color:#0a0a0a; font-weight:700; font-size:.95rem; padding:13px; border-radius:12px; cursor:pointer; }}
 button:hover {{ filter:brightness(1.06); }}
 .secure {{ color:#434c5e; font-size:.72rem; margin-top:16px; }}
 .err {{ color:#f87171; font-size:.82rem; margin-top:10px; min-height:1em; }}
 .foot {{ color:#2e3a52; font-size:.68rem; margin-top:26px; }}
</style></head><body><div class="wrap">
{body_done if done else body_form}
<div class="foot">Powered by ANOSagent</div>
</div>
<script>
async function go() {{
  const b = document.getElementById('go'); b.disabled = true; b.textContent = 'Opening checkout…';
  try {{
    const r = await fetch('/api/subscribe/{plan['token']}/checkout', {{method:'POST'}});
    const d = await r.json();
    if (d.url) location.href = d.url;
    else {{ document.getElementById('err').textContent = d.error || 'Checkout unavailable'; b.disabled = false; b.textContent = 'Subscribe with card →'; }}
  }} catch(e) {{ document.getElementById('err').textContent = 'Connection issue — try again'; b.disabled = false; }}
}}
</script></body></html>"""
