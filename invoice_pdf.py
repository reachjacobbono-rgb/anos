#!/usr/bin/env python3
"""Branded invoice PDF generator (reportlab)."""
import io, time, urllib.request

def _hex(color, fallback=(0.31, 0.49, 1.0)):
    try:
        h = color.lstrip('#')
        return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return fallback

def _money(v):
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return "$0.00"

def inv_balance(inv):
    paid = sum(p.get('amount', 0) for p in (inv.get('payments') or []))
    return round(max(0.0, (inv.get('total') or 0) - paid), 2), round(paid, 2)

def _logo_image(url, base_dir='/opt/anos/portal'):
    try:
        from reportlab.lib.utils import ImageReader
        from pathlib import Path
        if url.startswith('/'):
            p = Path(base_dir) / url.lstrip('/')
            if p.exists():
                return ImageReader(str(p))
        else:
            req = urllib.request.Request(url, headers={'User-Agent': 'anos-invoice/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return ImageReader(io.BytesIO(r.read()))
    except Exception:
        return None

def _render_kw_invoice(c, inv, client=None, base_dir='/opt/anos/portal'):
    """KW Snow Co style: navy header band + sky-blue accents, boxed totals, payment panel."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as rl
    from reportlab.lib.colors import Color, white

    NAVY = Color(0x0d / 255, 0x24 / 255, 0x40 / 255)
    SKY = Color(0x4f / 255, 0xc3 / 255, 0xf7 / 255)
    GREY = Color(0x5a / 255, 0x75 / 255, 0x90 / 255)
    LGREY = Color(0x8a / 255, 0x9f / 255, 0xb5 / 255)
    PANEL = Color(0xf2 / 255, 0xf7 / 255, 0xfb / 255)
    HAIR = Color(0xe1 / 255, 0xed / 255, 0xf7 / 255)

    settings = c.get('invoice_settings') or {}
    prof = c.get('settings_profile') or {}
    biz = (c.get('settings') or {}).get('business_name') or c.get('business') or c.get('email', '')
    biz_email = c.get('email', '')
    terms = ((c.get('settings_defaults') or {}).get('terms') or '').strip()

    W, H = LETTER
    buf = io.BytesIO()
    cv = rl.Canvas(buf, pagesize=(W, H))
    M = 54

    def charspace(s):
        try:
            cv.setCharSpace(s)
        except Exception:
            pass

    def wrap(text, width):
        words, lines, cur = str(text).split(), [], ''
        for w_ in words:
            if len(cur) + len(w_) + 1 > width:
                lines.append(cur)
                cur = w_
            else:
                cur = (cur + ' ' + w_).strip()
        if cur:
            lines.append(cur)
        return lines

    issued = time.strftime('%b %d, %Y', time.localtime(inv.get('created', time.time())))
    due = time.strftime('%b %d, %Y', time.localtime(inv.get('due_ts', time.time()))) if inv.get('due_ts') else '—'
    bal, paid_amt = inv_balance(inv)
    status = 'PAID' if inv.get('status') == 'paid' or bal <= 0 else ('VOID' if inv.get('status') == 'void' else 'DUE')

    # contract season, if this invoice is linked to one
    season_lbl = ''
    if inv.get('contract_id'):
        ct = next((x for x in c.get('contracts', []) if x.get('id') == inv.get('contract_id')), None)
        if ct:
            def _d(s):
                try:
                    return time.strftime('%b %d, %Y', time.strptime(str(s), '%Y-%m-%d'))
                except Exception:
                    return str(s or '')
            rng = f"{_d(ct.get('start'))} – {_d(ct.get('end'))}"
            season_lbl = (f"{ct.get('season', '')} · {rng}" if ct.get('season') else rng).strip(' ·')

    # ---------- header band ----------
    HH = 104
    cv.setFillColor(NAVY)
    cv.rect(0, H - HH, W, HH, stroke=0, fill=1)
    cv.setFillColor(SKY)
    cv.rect(0, H - HH, W, 5, stroke=0, fill=1)
    logo = _logo_image(settings.get('logo_url') or '', base_dir) if settings.get('logo_url') else None
    lx = M
    if logo:
        cv.setFillColor(white)
        cv.roundRect(M, H - 78, 118, 42, 7, stroke=0, fill=1)
        try:
            cv.drawImage(logo, M + 8, H - 72, width=102, height=30, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
        lx = M + 130
    cv.setFillColor(white)
    cv.setFont('Helvetica-Bold', 16)
    cv.drawString(lx, H - 66, str(biz).upper()[:34])
    cv.setFont('Helvetica-Bold', 25)
    cv.drawRightString(W - M, H - 52, 'INVOICE')
    cv.setFillColor(SKY)
    cv.setFont('Helvetica-Bold', 11)
    cv.drawRightString(W - M, H - 70, inv.get('number', ''))
    # status chip
    chip = Color(0.13, 0.65, 0.32) if status == 'PAID' else (Color(0.55, 0.57, 0.62) if status == 'VOID' else SKY)
    cv.setFillColor(chip)
    cv.roundRect(W - M - 66, H - 94, 66, 17, 4, stroke=0, fill=1)
    cv.setFillColor(NAVY if status == 'DUE' else white)
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawCentredString(W - M - 33, H - 88, status)

    # ---------- bill to + meta ----------
    y = H - HH - 30
    charspace(2)
    cv.setFillColor(SKY)
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawString(M, y, 'BILL TO')
    charspace(0)
    cv.setFillColor(NAVY)
    cname = (client or {}).get('name') or inv.get('client_name') or 'Client'
    cv.setFont('Helvetica-Bold', 13)
    cv.drawString(M, y - 17, str(cname)[:45])
    cv.setFillColor(GREY)
    cv.setFont('Helvetica', 9.5)
    ly = y - 32
    for line in [(client or {}).get('email', ''), (client or {}).get('address', '')]:
        if line:
            cv.drawString(M, ly, str(line)[:60])
            ly -= 13

    meta_rows = [('Invoice Date:', issued), ('Due Date:', due)]
    if season_lbl:
        meta_rows.append(('Season:', season_lbl))
    my = y - 4
    for lbl, val in meta_rows:
        cv.setFont('Helvetica-Bold', 9.5)
        vw = cv.stringWidth(val, 'Helvetica-Bold', 9.5)
        cv.setFillColor(NAVY)
        cv.drawRightString(W - M, my, val)
        cv.setFont('Helvetica', 9.5)
        cv.setFillColor(GREY)
        cv.drawRightString(W - M - vw - 8, my, lbl)
        my -= 14

    # ---------- line items ----------
    ty = min(my, ly) - 26
    cv.setFillColor(NAVY)
    cv.rect(M, ty - 24, W - 2 * M, 26, stroke=0, fill=1)
    cv.setFillColor(SKY)
    cv.rect(M, ty - 24, W - 2 * M, 4, stroke=0, fill=1)
    charspace(1.4)
    cv.setFillColor(white)
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawString(M + 12, ty - 15, 'DESCRIPTION')
    cv.drawRightString(W - M - 160, ty - 15, 'QTY')
    cv.drawRightString(W - M - 90, ty - 15, 'RATE')
    cv.drawRightString(W - M - 12, ty - 15, 'AMOUNT')
    charspace(0)

    yy = ty - 44
    for it in (inv.get('items') or []):
        price = it.get('price') or 0
        disc = price < 0
        col = SKY if disc else NAVY
        cv.setFillColor(col)
        cv.setFont('Helvetica-Bold', 10)
        desc_lines = wrap(it.get('desc', ''), 62)[:2]
        cv.drawString(M + 12, yy, desc_lines[0])
        cv.setFont('Helvetica', 9.5)
        cv.setFillColor(SKY if disc else GREY)
        cv.drawRightString(W - M - 160, yy, str(it.get('qty', 1)))
        cv.drawRightString(W - M - 90, yy, _money(price))
        cv.setFillColor(col)
        cv.setFont('Helvetica-Bold', 9.5)
        cv.drawRightString(W - M - 12, yy, _money(price * (it.get('qty') or 1)))
        row_h = 16
        for extra in desc_lines[1:]:
            cv.setFont('Helvetica', 8.5)
            cv.setFillColor(GREY)
            cv.drawString(M + 12, yy - row_h + 2, extra)
            row_h += 11
        yy -= (row_h + 12)
        cv.setStrokeColor(HAIR)
        cv.setLineWidth(0.7)
        cv.line(M, yy + 10, W - M, yy + 10)
        if yy < 240:
            cv.showPage()
            yy = H - 80

    # ---------- totals box ----------
    rows = [('Subtotal', _money(inv.get('subtotal', 0)))]
    if inv.get('tax'):
        rows.append((f"HST ({inv.get('tax_pct', 0):g}%)", _money(inv.get('tax', 0))))
    if inv.get('deposit') and not paid_amt:
        rows.append(('Deposit due now', _money(inv['deposit'])))
    if paid_amt:
        rows.append(('Paid to date', f"−{_money(paid_amt)}"))
    yy -= 8
    tx = W - M - 150
    for lbl, val in rows:
        cv.setFont('Helvetica', 10)
        cv.setFillColor(GREY)
        cv.drawRightString(tx, yy, lbl)
        cv.setFont('Helvetica-Bold', 10)
        cv.setFillColor(NAVY)
        cv.drawRightString(W - M - 12, yy, val)
        yy -= 16
    cv.setStrokeColor(Color(0xd3 / 255, 0xe4 / 255, 0xf2 / 255))
    cv.line(tx - 140, yy + 9, W - M - 12, yy + 9)
    yy -= 8
    cv.setFillColor(NAVY)
    cv.roundRect(tx - 140, yy - 26, (W - M) - (tx - 140), 36, 8, stroke=0, fill=1)
    charspace(1)
    cv.setFillColor(SKY)
    cv.setFont('Helvetica-Bold', 10)
    cv.drawString(tx - 128, yy - 13, 'BALANCE DUE' if paid_amt and bal > 0 else 'TOTAL DUE')
    charspace(0)
    cv.setFillColor(white)
    cv.setFont('Helvetica-Bold', 17)
    cv.drawRightString(W - M - 24, yy - 14, _money(bal if paid_amt else inv.get('total', 0)))
    yy -= 48

    # ---------- payments ----------
    if inv.get('payments'):
        cv.setFillColor(LGREY)
        charspace(1.6)
        cv.setFont('Helvetica-Bold', 7.5)
        cv.drawString(M, yy, 'PAYMENTS RECEIVED')
        charspace(0)
        yy -= 14
        for p in inv['payments'][:6]:
            cv.setFont('Helvetica', 8.5)
            cv.setFillColor(GREY)
            pts = time.strftime('%b %d, %Y', time.localtime(p.get('ts', time.time())))
            cv.drawString(M, yy, f"{pts} — {p.get('method', 'payment')}")
            cv.drawRightString(W - M - 12, yy, _money(p.get('amount', 0)))
            yy -= 13
        yy -= 6

    # ---------- payment information panel ----------
    methods = (settings.get('payment_methods') or [])
    pay_lines = [f"Please reference invoice number {inv.get('number', '')} with your payment."]
    if inv.get('note'):
        pay_lines = [str(inv['note']).strip()] + pay_lines
    ph = 30 + (len(methods) + len(pay_lines)) * 13 + 8
    if yy - ph < 110:
        cv.showPage()
        yy = H - 70
    cv.setFillColor(PANEL)
    cv.roundRect(M, yy - ph, W - 2 * M, ph, 7, stroke=0, fill=1)
    cv.setFillColor(SKY)
    cv.rect(M, yy - ph, 5, ph, stroke=0, fill=1)
    charspace(2)
    cv.setFillColor(NAVY)
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawString(M + 16, yy - 18, 'PAYMENT INFORMATION')
    charspace(0)
    py = yy - 34
    cv.setFont('Helvetica', 9)
    for m in methods:
        i = m.find(':')
        if i > 0:
            cv.setFillColor(NAVY)
            cv.setFont('Helvetica-Bold', 9)
            cv.drawString(M + 16, py, m[:i + 1])
            lw = cv.stringWidth(m[:i + 1] + ' ', 'Helvetica-Bold', 9)
            cv.setFont('Helvetica', 9)
            cv.setFillColor(GREY)
            cv.drawString(M + 16 + lw, py, m[i + 1:].strip())
        else:
            cv.setFillColor(GREY)
            cv.drawString(M + 16, py, m)
        py -= 13
    for ln in pay_lines:
        cv.setFont('Helvetica', 9)
        cv.setFillColor(GREY)
        cv.drawString(M + 16, py, ln[:110])
        py -= 13

    # ---------- terms ----------
    if terms:
        ty2 = yy - ph - 18
        if ty2 - 60 > 70:
            charspace(1.6)
            cv.setFillColor(NAVY)
            cv.setFont('Helvetica-Bold', 7.5)
            cv.drawString(M, ty2, 'TERMS & CONDITIONS')
            charspace(0)
            cv.setFillColor(LGREY)
            cv.setFont('Helvetica', 7.5)
            ty2 -= 11
            for ln in wrap(terms, 118)[:6]:
                cv.drawString(M, ty2, ln)
                ty2 -= 9.5

    # ---------- footer band ----------
    cv.setFillColor(NAVY)
    cv.rect(0, 0, W, 46, stroke=0, fill=1)
    cv.setFillColor(SKY)
    cv.rect(0, 46, W, 3, stroke=0, fill=1)
    cv.setFillColor(white)
    cv.setFont('Helvetica-Bold', 9)
    cv.drawString(M, 28, str(biz)[:40])
    cv.setFillColor(Color(1, 1, 1, 0.6))
    cv.setFont('Helvetica', 7.5)
    addr = prof.get('address') or ''
    if addr:
        cv.drawString(M, 16, str(addr)[:60])
    contact = ' | '.join(x for x in [biz_email, prof.get('phone', '')] if x)
    cv.setFillColor(Color(1, 1, 1, 0.6))
    cv.setFont('Helvetica', 7.5)
    cv.drawRightString(W - M, 28, contact[:80])
    tag = settings.get('footer') or f'GST/HST Registration No.: {prof.get("hst_number")}' if prof.get('hst_number') else (settings.get('footer') or '')
    if tag:
        cv.setFillColor(SKY)
        cv.setFont('Helvetica-Bold', 7.5)
        charspace(1)
        cv.drawRightString(W - M, 16, str(tag).upper()[:70])
        charspace(0)

    cv.showPage()
    cv.save()
    return buf.getvalue()


def render_invoice_pdf(c, inv, client=None, base_dir='/opt/anos/portal'):
    """c = owner customer record, inv = invoice dict, client = {name,email,address} optional.
    Returns PDF bytes."""
    settings0 = c.get('invoice_settings') or {}
    if settings0.get('template') == 'kw':
        return _render_kw_invoice(c, inv, client, base_dir)
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as rl
    from reportlab.lib.colors import Color, white

    settings = c.get('invoice_settings') or {}
    accent = _hex(settings.get('accent') or '#4f7dff')
    ac = Color(*accent)
    biz = (c.get('settings') or {}).get('business_name') or c.get('business') or c.get('email', '')
    biz_email = c.get('email', '')
    footer = settings.get('footer') or ''

    W, H = LETTER
    buf = io.BytesIO()
    cv = rl.Canvas(buf, pagesize=(W, H))
    M = 54

    cv.setFillColor(ac)
    cv.rect(0, H - 8, W, 8, stroke=0, fill=1)

    logo = _logo_image(settings.get('logo_url') or '', base_dir) if settings.get('logo_url') else None
    y = H - 60
    if logo:
        try:
            cv.drawImage(logo, M, y - 34, width=120, height=40, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    else:
        cv.setFillColor(ac)
        cv.setFont('Helvetica-Bold', 20)
        cv.drawString(M, y - 24, str(biz)[:40])

    cv.setFillColor(Color(0.1, 0.11, 0.13))
    cv.setFont('Helvetica-Bold', 26)
    cv.drawRightString(W - M, y - 22, 'INVOICE')
    cv.setFont('Helvetica', 10)
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.drawRightString(W - M, y - 38, inv.get('number', ''))

    bal, paid_amt = inv_balance(inv)
    status = 'PAID' if inv.get('status') == 'paid' or bal <= 0 else ('VOID' if inv.get('status') == 'void' else 'DUE')
    chip_col = Color(0.13, 0.65, 0.32) if status == 'PAID' else (Color(0.55, 0.57, 0.62) if status == 'VOID' else Color(0.91, 0.35, 0.24))
    cv.setFillColor(chip_col)
    cv.roundRect(W - M - 70, y - 64, 70, 18, 4, stroke=0, fill=1)
    cv.setFillColor(white)
    cv.setFont('Helvetica-Bold', 9)
    cv.drawCentredString(W - M - 35, y - 58, status)

    y2 = y - 96
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.setFont('Helvetica-Bold', 8)
    cv.drawString(M, y2, 'FROM')
    cv.drawString(W / 2, y2, 'BILL TO')
    cv.setFillColor(Color(0.1, 0.11, 0.13))
    cv.setFont('Helvetica-Bold', 11)
    cv.drawString(M, y2 - 16, str(biz)[:45])
    cv.setFont('Helvetica', 9.5)
    cv.setFillColor(Color(0.35, 0.37, 0.42))
    prof = c.get('settings_profile') or {}
    ly = y2 - 30
    for line in [biz_email, prof.get('phone', ''), prof.get('address', ''),
                 ('HST# ' + str(prof.get('hst_number'))) if prof.get('hst_number') else '']:
        if line:
            cv.drawString(M, ly, str(line)[:60])
            ly -= 13
    cname = (client or {}).get('name') or inv.get('client_name') or 'Client'
    cv.setFillColor(Color(0.1, 0.11, 0.13))
    cv.setFont('Helvetica-Bold', 11)
    cv.drawString(W / 2, y2 - 16, str(cname)[:45])
    cv.setFont('Helvetica', 9.5)
    cv.setFillColor(Color(0.35, 0.37, 0.42))
    cy = y2 - 30
    for line in [(client or {}).get('email', ''), (client or {}).get('address', '')]:
        if line:
            cv.drawString(W / 2, cy, str(line)[:60])
            cy -= 13

    y3 = min(cy, ly) - 22
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.setFont('Helvetica', 9)
    issued = time.strftime('%b %d, %Y', time.localtime(inv.get('created', time.time())))
    due = time.strftime('%b %d, %Y', time.localtime(inv.get('due_ts', time.time()))) if inv.get('due_ts') else '—'
    cv.drawString(W / 2, y3, f'Issued: {issued}    Due: {due}')

    y4 = y3 - 40
    cv.setFillColor(Color(0.96, 0.965, 0.975))
    cv.roundRect(M, y4 - 6, W - 2 * M, 24, 4, stroke=0, fill=1)
    cv.setFillColor(Color(0.35, 0.37, 0.42))
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawString(M + 10, y4 + 2, 'DESCRIPTION')
    cv.drawRightString(W - M - 160, y4 + 2, 'QTY')
    cv.drawRightString(W - M - 80, y4 + 2, 'RATE')
    cv.drawRightString(W - M - 10, y4 + 2, 'AMOUNT')

    yy = y4 - 26
    for it in (inv.get('items') or []):
        cv.setFillColor(Color(0.1, 0.11, 0.13))
        cv.setFont('Helvetica', 9.5)
        cv.drawString(M + 10, yy, str(it.get('desc', ''))[:58])
        cv.setFont('Helvetica', 9)
        cv.setFillColor(Color(0.35, 0.37, 0.42))
        cv.drawRightString(W - M - 160, yy, str(it.get('qty', 1)))
        cv.drawRightString(W - M - 80, yy, _money(it.get('price', 0)))
        cv.setFillColor(Color(0.1, 0.11, 0.13))
        cv.drawRightString(W - M - 10, yy, _money((it.get('price') or 0) * (it.get('qty') or 1)))
        yy -= 22
        if yy < 170:
            cv.showPage()
            yy = H - 80

    yy -= 4
    cv.setStrokeColor(Color(0.9, 0.9, 0.92))
    cv.line(M, yy + 12, W - M, yy + 12)
    tx = W - M - 220
    rows = [('Subtotal', _money(inv.get('subtotal', 0)))]
    if inv.get('tax'):
        rows.append((f"Tax ({inv.get('tax_pct', 0):g}%)", _money(inv.get('tax', 0))))
    rows.append(('Total', _money(inv.get('total', 0))))
    if inv.get('deposit') and not paid_amt:
        rows.append(('Deposit due now', _money(inv['deposit'])))
    if paid_amt:
        rows.append(("Paid to date", f"-{_money(paid_amt)}"))
        rows.append(('Balance due', _money(bal)))
    for label, val in rows:
        bold = label in ('Total', 'Balance due')
        cv.setFont('Helvetica-Bold' if bold else 'Helvetica', 10.5 if bold else 9.5)
        cv.setFillColor(ac if bold else Color(0.35, 0.37, 0.42))
        cv.drawString(tx, yy, label)
        cv.drawRightString(W - M - 10, yy, val)
        yy -= 20

    if inv.get('payments'):
        yy -= 12
        cv.setFillColor(Color(0.45, 0.47, 0.52))
        cv.setFont('Helvetica-Bold', 8)
        cv.drawString(M, yy, 'PAYMENTS')
        yy -= 16
        for p in inv['payments']:
            cv.setFont('Helvetica', 9)
            cv.setFillColor(Color(0.35, 0.37, 0.42))
            pts = time.strftime('%b %d, %Y', time.localtime(p.get('ts', time.time())))
            cv.drawString(M, yy, f"{pts} — {p.get('method', 'payment')}")
            cv.drawRightString(W - M - 10, yy, _money(p.get('amount', 0)))
            yy -= 15

    if inv.get('note'):
        yy -= 10
        cv.setFillColor(Color(0.45, 0.47, 0.52))
        cv.setFont('Helvetica-Oblique', 9)
        for line in str(inv['note']).split('\n')[:4]:
            cv.drawString(M, yy, line[:95])
            yy -= 13

    cv.setFillColor(ac)
    cv.rect(0, 60, W, 1.2, stroke=0, fill=1)
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.setFont('Helvetica', 8.5)
    cv.drawCentredString(W / 2, 44, (footer or f'Thank you for your business — {biz}')[:110])
    cv.drawCentredString(W / 2, 30, f'{inv.get("number", "")} · Issued {issued}')

    cv.showPage()
    cv.save()
    return buf.getvalue()


def render_statement_pdf(c, client_name, invoices, period_label, base_dir='/opt/anos/portal'):
    """Monthly statement: all invoices + payments + balance for one client."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as rl
    from reportlab.lib.colors import Color

    settings = c.get('invoice_settings') or {}
    accent = _hex(settings.get('accent') or '#4f7dff')
    ac = Color(*accent)
    biz = (c.get('settings') or {}).get('business_name') or c.get('business') or c.get('email', '')

    W, H = LETTER
    buf = io.BytesIO()
    cv = rl.Canvas(buf, pagesize=(W, H))
    M = 54

    cv.setFillColor(ac)
    cv.rect(0, H - 8, W, 8, stroke=0, fill=1)
    y = H - 60
    cv.setFillColor(ac)
    cv.setFont('Helvetica-Bold', 20)
    cv.drawString(M, y - 24, str(biz)[:40])
    cv.setFillColor(Color(0.1, 0.11, 0.13))
    cv.setFont('Helvetica-Bold', 24)
    cv.drawRightString(W - M, y - 22, 'STATEMENT')
    cv.setFont('Helvetica', 10)
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.drawRightString(W - M, y - 38, period_label)

    y2 = y - 76
    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.setFont('Helvetica-Bold', 8)
    cv.drawString(M, y2, 'FOR')
    cv.setFillColor(Color(0.1, 0.11, 0.13))
    cv.setFont('Helvetica-Bold', 12)
    cv.drawString(M, y2 - 16, str(client_name)[:50])

    y3 = y2 - 50
    cv.setFillColor(Color(0.96, 0.965, 0.975))
    cv.roundRect(M, y3 - 6, W - 2 * M, 24, 4, stroke=0, fill=1)
    cv.setFillColor(Color(0.35, 0.37, 0.42))
    cv.setFont('Helvetica-Bold', 8.5)
    cv.drawString(M + 10, y3 + 2, 'DATE')
    cv.drawString(M + 100, y3 + 2, 'INVOICE')
    cv.drawRightString(W - M - 200, y3 + 2, 'STATUS')
    cv.drawRightString(W - M - 100, y3 + 2, 'PAID')
    cv.drawRightString(W - M - 10, y3 + 2, 'BALANCE')

    yy = y3 - 26
    total_due = 0.0
    for inv in invoices:
        bal, paid_amt = inv_balance(inv)
        if inv.get('status') == 'sent':
            total_due += bal
        cv.setFillColor(Color(0.1, 0.11, 0.13))
        cv.setFont('Helvetica', 9)
        dt = time.strftime('%b %d, %Y', time.localtime(inv.get('created', time.time())))
        cv.drawString(M + 10, yy, dt)
        cv.drawString(M + 100, yy, inv.get('number', ''))
        status = 'PAID' if inv.get('status') == 'paid' else ('VOID' if inv.get('status') == 'void' else 'DUE')
        cv.setFillColor(Color(0.13, 0.65, 0.32) if status == 'PAID' else (Color(0.55, 0.57, 0.62) if status == 'VOID' else Color(0.91, 0.35, 0.24)))
        cv.drawRightString(W - M - 200, yy, status)
        cv.setFillColor(Color(0.1, 0.11, 0.13))
        cv.drawRightString(W - M - 100, yy, _money(paid_amt))
        cv.drawRightString(W - M - 10, yy, _money(bal))
        yy -= 20
        if yy < 120:
            cv.showPage()
            yy = H - 80

    yy -= 8
    cv.setStrokeColor(ac)
    cv.setLineWidth(1.5)
    cv.line(M, yy + 10, W - M, yy + 10)
    cv.setFont('Helvetica-Bold', 13)
    cv.setFillColor(ac)
    cv.drawString(M, yy - 12, 'Total balance due')
    cv.drawRightString(W - M - 10, yy - 12, _money(total_due))

    cv.setFillColor(Color(0.45, 0.47, 0.52))
    cv.setFont('Helvetica', 8.5)
    cv.drawCentredString(W / 2, 40, ('Thank you for your business - ' + str(biz))[:110])

    cv.showPage()
    cv.save()
    return buf.getvalue()
