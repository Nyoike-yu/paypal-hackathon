"""small PayPal REST client: Orders v2 (create and capture), Payouts, refunds and webhook verification.

with PAYPAL_CLIENT_ID and PAYPAL_SECRET set it talks to PayPal (sandbox by default, live when PAYPAL_ENV=live).
without them every call is simulated so the whole flow runs on a laptop with no keys"""
import os, time, uuid, threading
from decimal import Decimal
import httpx

class PayPalError(Exception):
    pass

def configured():
    return bool(os.getenv("PAYPAL_CLIENT_ID") and os.getenv("PAYPAL_SECRET"))

def base():
    return "https://api-m.paypal.com" if os.getenv("PAYPAL_ENV") == "live" else "https://api-m.sandbox.paypal.com"

_tok = {"v": None, "exp": 0.0}
_lock = threading.Lock()

def _client():  # tests replace this with a client that has a mock transport
    return httpx.Client(timeout=20)

def _token(h):
    with _lock:
        if _tok["v"] and _tok["exp"] > time.time() + 60:
            return _tok["v"]
        r = h.post(f"{base()}/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_SECRET"]))
        r.raise_for_status()
        d = r.json()
        _tok.update(v=d["access_token"], exp=time.time() + int(d.get("expires_in", 300)))
        return _tok["v"]

def fresh_token():
    """a brand new access token, ignoring the cache. the MCP server is started with one token so it needs a new one before that expires"""
    with _lock:
        _tok["exp"] = 0.0
    with _client() as h:
        return _token(h)

def token_ttl():
    """seconds until the cached access token expires"""
    return max(0.0, _tok["exp"] - time.time())

def _call(method, path, body=None, request_id=None):
    try:
        with _client() as h:
            headers = {"Authorization": f"Bearer {_token(h)}", "Content-Type": "application/json"}
            if request_id: headers["PayPal-Request-Id"] = request_id  # makes a retry safe: PayPal returns the first result
            r = h.request(method, base() + path, json=body, headers=headers)
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise PayPalError(str(e)) from e

def money(x):
    return f"{Decimal(str(x)):.2f}"

def create_order(amount, description, return_url, cancel_url, request_id=None):
    """starts a checkout. returns (order_id, url the buyer opens to approve the payment)"""
    if not configured():
        oid = "SIM-" + uuid.uuid4().hex[:12]
        return oid, f"{return_url}{'&' if '?' in return_url else '?'}token={oid}&simulated=1"
    d = _call("POST", "/v2/checkout/orders", {
        "intent": "CAPTURE",
        "purchase_units": [{"description": description[:120], "amount": {"currency_code": "USD", "value": money(amount)}}],
        "payment_source": {"paypal": {"experience_context": {"return_url": return_url, "cancel_url": cancel_url, "user_action": "PAY_NOW", "brand_name": "Stash", "shipping_preference": "NO_SHIPPING"}}},
    }, request_id)
    link = next((l["href"] for l in d.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    if not d.get("id") or not link:
        raise PayPalError("PayPal returned no approval link")
    return d["id"], link

def capture_order(order_id, request_id=None):
    """takes the money after the buyer approved. returns {"ok": bool, "amount": Decimal|None, "capture_id": str}"""
    if not configured():
        if not order_id.startswith("SIM-"): raise PayPalError("unknown order")
        return {"ok": True, "amount": None, "capture_id": "SIMCAP-" + order_id[4:]}
    if order_id.startswith("SIM-"): raise PayPalError("simulated order with live keys")
    d = _call("POST", f"/v2/checkout/orders/{order_id}/capture", {}, request_id)
    try:
        cap = d["purchase_units"][0]["payments"]["captures"][0]
        return {"ok": d.get("status") == "COMPLETED" and cap.get("status") == "COMPLETED", "amount": Decimal(cap["amount"]["value"]), "capture_id": cap["id"]}
    except (KeyError, IndexError, ValueError) as e:
        raise PayPalError("unexpected capture response") from e

def get_order(order_id):
    """reads one order (read-only). returns PayPal's order JSON, in demo mode a small stand-in so the flow runs with no keys"""
    if not configured():
        if not order_id.startswith("SIM-"): raise PayPalError("unknown order")
        return {"id": order_id, "status": "SIMULATED"}
    if order_id.startswith("SIM-"): raise PayPalError("simulated order with live keys")
    return _call("GET", f"/v2/checkout/orders/{order_id}")

def order_summary(d):
    """keeps only what's safe and useful to show: status, amount, date. no payer name, email or address ever leaves here. None if d isn't an order"""
    if not isinstance(d, dict) or not d.get("status"): return None
    try:
        pu = (d.get("purchase_units") or [{}])[0]
        amt = pu.get("amount") or {}
        cap = ((pu.get("payments") or {}).get("captures") or [{}])[0]
        return {"paypal_status": str(d["status"])[:30], "amount": money(amt["value"]) if amt.get("value") is not None else None,
                "currency": amt.get("currency_code"), "created": d.get("create_time"), "capture_status": cap.get("status")}
    except (TypeError, ValueError, IndexError, AttributeError, ArithmeticError):
        return None

def order_capture(d):
    """the captured payment inside an order, for refunds: {capture_id, status, amount, refunded}. internal only, never shown to the model. None if there isn't one"""
    try:
        pays = ((d.get("purchase_units") or [{}])[0].get("payments") or {})
        cap = (pays.get("captures") or [None])[0]
        if not cap or not cap.get("id"): return None
        refunded = sum((Decimal(x["amount"]["value"]) for x in pays.get("refunds") or [] if x.get("status") in ("COMPLETED", "PENDING")), Decimal(0))
        return {"capture_id": str(cap["id"]), "status": cap.get("status"), "amount": Decimal(cap["amount"]["value"]), "refunded": refunded}
    except (TypeError, ValueError, IndexError, AttributeError, KeyError, ArithmeticError):
        return None

def order_refunds(d):
    """refunds PayPal lists on an order: [{id, status, amount, invoice_id}]. empty if none or unreadable"""
    try:
        return [{"id": x.get("id"), "status": x.get("status"), "amount": Decimal(x["amount"]["value"]), "invoice_id": x.get("invoice_id")}
                for x in ((d.get("purchase_units") or [{}])[0].get("payments") or {}).get("refunds") or []]
    except (TypeError, ValueError, IndexError, AttributeError, KeyError, ArithmeticError):
        return []

_REFUND_WHY = {"CAPTURE_FULLY_REFUNDED": "PayPal says this payment was already refunded in full.",
               "REFUND_AMOUNT_EXCEEDED": "That is more than PayPal has left to refund on this payment.",
               "REFUND_NOT_ALLOWED_AFTER_180_DAYS": "PayPal only allows refunds within 180 days of the payment."}

def refund_capture(capture_id, amount, note, request_id, invoice_id):
    """sends money back to the buyer who paid. returns {"state", "ref", "why"}, state is one of:
      refunded: PayPal accepted it (COMPLETED or PENDING)
      failed:   PayPal definitely did not refund it, so the money can safely go back into the app balance
      unknown:  can't tell (timeout, PayPal busy, odd reply). ask again later with the SAME request_id, PayPal then returns the first result and never refunds twice
    never raises. without PayPal keys it's simulated like the rest of the app"""
    out = lambda state, ref=None, why=None: {"state": state, "ref": ref, "why": why}
    if not configured():
        return out("refunded", "SIMREF-" + request_id[-12:]) if capture_id.startswith("SIMCAP-") else out("failed", why="That payment isn't one I can refund.")
    if capture_id.startswith("SIMCAP-"): return out("failed", why="That top-up was made in demo mode, so there is no real payment to refund.")
    body = {"amount": {"currency_code": "USD", "value": money(amount)}, "invoice_id": invoice_id[:127], "note_to_payer": note[:255]}
    try:
        with _client() as h:
            r = h.post(f"{base()}/v2/payments/captures/{capture_id}/refund", json=body,
                       headers={"Authorization": f"Bearer {_token(h)}", "Content-Type": "application/json", "PayPal-Request-Id": request_id})
    except (httpx.HTTPError, KeyError, ValueError, ArithmeticError):
        return out("unknown")
    try: d = r.json()
    except ValueError: d = {}
    d = d if isinstance(d, dict) else {}
    if r.status_code in (200, 201):  # 200 is PayPal repeating the first answer to a retried request
        if d.get("id") and d.get("status") in ("COMPLETED", "PENDING"): return out("refunded", str(d["id"]))
        if d.get("status") in ("FAILED", "CANCELLED"): return out("failed", why="PayPal didn't complete the refund.")
        return out("unknown")
    if r.status_code in (401, 408, 429) or r.status_code >= 500: return out("unknown")
    if 400 <= r.status_code < 500:  # PayPal looked at it and said no, so nothing was sent
        issue = next((x.get("issue") for x in d.get("details") or [] if isinstance(x, dict)), None) or d.get("name")
        return out("failed", why=_REFUND_WHY.get(issue, "PayPal didn't accept the refund."))
    return out("unknown")

def payout(email, amount, note, batch_id):
    """pays a merchant from the Stash PayPal balance. returns PayPal's batch id (a stable batch_id makes a retry safe)"""
    if not configured() or not email:
        return "SIM-PAYOUT-" + batch_id
    d = _call("POST", "/v1/payments/payouts", {
        "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "You have a payment from Stash"},
        "items": [{"recipient_type": "EMAIL", "amount": {"value": money(amount), "currency": "USD"}, "receiver": email, "note": note[:100], "sender_item_id": batch_id}],
    })
    try:
        return d["batch_header"]["payout_batch_id"]
    except KeyError as e:
        raise PayPalError("unexpected payout response") from e

def verify_webhook(headers, event):
    """True only if PayPal signed this event for our webhook id"""
    wid = os.getenv("PAYPAL_WEBHOOK_ID")
    if not configured() or not wid: return False
    d = _call("POST", "/v1/notifications/verify-webhook-signature", {
        "auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"), "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"), "transmission_time": headers.get("paypal-transmission-time"), "webhook_id": wid, "webhook_event": event})
    return d.get("verification_status") == "SUCCESS"
