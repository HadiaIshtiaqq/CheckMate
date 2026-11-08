"""Hosted-approval page for the PayPal simulation. Only mounted when
PAYPAL_MODE=mock. With real Sandbox credentials the buyer approves on PayPal."""
from __future__ import annotations

import html
from urllib.parse import urlencode

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.integrations.paypal.client import MockPayPalClient, PayPalError
from app.services.container import get_container

router = APIRouter(prefix="/mock-paypal", include_in_schema=False)


def _client() -> MockPayPalClient:
    return get_container().paypal  # type: ignore[return-value]


@router.get("/checkout", response_class=HTMLResponse)
def checkout_page(token: str, ct: str = ""):
    if not _client().approval_token_valid(token, ct):
        return HTMLResponse("<h1>This approval link is invalid or incomplete.</h1>", status_code=403)
    try:
        order = _client().get_order(token)
    except PayPalError:
        return HTMLResponse("<h1>Order not found</h1>", status_code=404)
    unit = order["purchase_units"][0]
    amt = unit["amount"]
    items = "".join(f"<li>{html.escape(i['name'])} &times; {html.escape(i['quantity'])} &mdash; "
                    f"{html.escape(i['unit_amount']['value'])} {html.escape(i['unit_amount']['currency_code'])}</li>"
                    for i in unit.get("items", []))
    t = html.escape(token)
    ctok = html.escape(ct)
    status = html.escape(order["status"])
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>Simulated PayPal approval</title><style>
body{{font-family:system-ui,sans-serif;background:#f5f7fa;color:#1f2937;margin:0;padding:24px}}
.card{{max-width:440px;margin:40px auto;background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 12px #0001}}
.badge{{display:inline-block;background:#fef3c7;color:#92400e;border-radius:6px;padding:4px 8px;font-size:12px}}
h1{{font-size:20px}} .total{{font-size:28px;font-weight:700;margin:12px 0}}
button{{width:100%;padding:12px;border-radius:24px;border:0;font-size:16px;margin-top:10px;cursor:pointer}}
.pay{{background:#0070ba;color:#fff}} .cancel{{background:#e5e7eb}}</style></head><body><div class="card">
<span class="badge">SIMULATED PAYPAL &mdash; no real money moves</span>
<h1>Approve payment to CHECKMATE Demo</h1><p>Order {t} &middot; status {status}</p>
<ul>{items}</ul><div class="total">{html.escape(amt['value'])} {html.escape(amt['currency_code'])}</div>
<form method="post" action="/mock-paypal/checkout/{t}/decision"><input type="hidden" name="approve" value="1"><input type="hidden" name="ct" value="{ctok}">
<button class="pay">Approve &amp; continue</button></form>
<form method="post" action="/mock-paypal/checkout/{t}/decision"><input type="hidden" name="approve" value="0"><input type="hidden" name="ct" value="{ctok}">
<button class="cancel">Cancel and return</button></form>
<p style="font-size:12px;color:#6b7280">Configure PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET to use the real
PayPal Sandbox instead.</p></div></body></html>""")


@router.post("/checkout/{order_id}/decision")
def decision(order_id: str, approve: str = Form(...), ct: str = Form("")):
    c = _client()
    if not c.approval_token_valid(order_id, ct):
        return HTMLResponse("<h1>This approval link is invalid.</h1>", status_code=403)
    ok = approve == "1"
    try:
        c.buyer_decision(order_id, ok)
    except PayPalError:
        return HTMLResponse("<h1>Order not found</h1>", status_code=404)
    ret, cancel = c.order_urls(order_id)
    target = ret if ok else cancel
    if not target:
        return HTMLResponse(f"<p>Order {html.escape(order_id)} {'approved' if ok else 'cancelled'}.</p>")
    sep = "&" if "?" in target else "?"
    q = urlencode({"token": order_id, **({"PayerID": "MOCKPAYER01"} if ok else {})})
    return RedirectResponse(f"{target}{sep}{q}", status_code=303)
