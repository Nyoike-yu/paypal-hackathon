"""paypal.refund_capture and the order parsers, against a mock PayPal (httpx.MockTransport). No network, no keys."""
import json
from decimal import Decimal
import httpx, pytest
import paypal

def live(monkeypatch, refund_reply):
    """Pretend keys are set. refund_reply(request) -> httpx.Response (or raises). Returns the list of refund requests PayPal received."""
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "secret"); monkeypatch.delenv("PAYPAL_ENV", raising=False)
    monkeypatch.setitem(paypal._tok, "v", None); monkeypatch.setitem(paypal._tok, "exp", 0.0)
    seen = []
    def handler(req):
        if req.url.path.endswith("/v1/oauth2/token"): return httpx.Response(200, json={"access_token": "tok-123", "expires_in": 300})
        seen.append(req)
        return refund_reply(req)
    monkeypatch.setattr(paypal, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return seen

def refund(**kw):
    a = dict(capture_id="CAP123", amount=Decimal("20.00"), note="Refund of your Stash top-up", request_id="stash-refund-7", invoice_id="stash-refund-7")
    a.update(kw)
    return paypal.refund_capture(**a)

# ---------- what PayPal receives ----------
def test_the_request_is_exactly_what_paypal_documents(monkeypatch):
    seen = live(monkeypatch, lambda r: httpx.Response(201, json={"id": "REF1", "status": "COMPLETED"}))
    out = refund()
    assert out == {"state": "refunded", "ref": "REF1", "why": None}
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v2/payments/captures/CAP123/refund" and req.url.host == "api-m.sandbox.paypal.com"
    assert req.headers["authorization"] == "Bearer tok-123" and req.headers["paypal-request-id"] == "stash-refund-7"
    assert json.loads(req.content) == {"amount": {"currency_code": "USD", "value": "20.00"}, "invoice_id": "stash-refund-7", "note_to_payer": "Refund of your Stash top-up"}

def test_amount_is_sent_with_two_decimals(monkeypatch):
    seen = live(monkeypatch, lambda r: httpx.Response(201, json={"id": "R", "status": "COMPLETED"}))
    refund(amount=Decimal("5")); refund(amount=Decimal("0.5"))
    assert [json.loads(r.content)["amount"]["value"] for r in seen] == ["5.00", "0.50"]

def test_live_environment_uses_the_live_host(monkeypatch):
    seen = live(monkeypatch, lambda r: httpx.Response(201, json={"id": "R", "status": "COMPLETED"}))
    monkeypatch.setenv("PAYPAL_ENV", "live"); refund()
    assert seen[0].url.host == "api-m.paypal.com"

# ---------- refunded ----------
@pytest.mark.parametrize("code,status", [(201, "COMPLETED"), (201, "PENDING"), (200, "COMPLETED")])  # 200 = PayPal repeating its first answer to a retry
def test_accepted_means_refunded(monkeypatch, code, status):
    live(monkeypatch, lambda r: httpx.Response(code, json={"id": "REF9", "status": status}))
    assert refund() == {"state": "refunded", "ref": "REF9", "why": None}

# ---------- failed: PayPal looked and said no, so nothing was sent ----------
@pytest.mark.parametrize("issue,text", [("CAPTURE_FULLY_REFUNDED", "already refunded"), ("REFUND_AMOUNT_EXCEEDED", "more than PayPal has left"),
                                        ("REFUND_NOT_ALLOWED_AFTER_180_DAYS", "180 days"), ("SOMETHING_NEW", "didn't accept")])
def test_422_is_a_definite_no_with_a_plain_reason(monkeypatch, issue, text):
    live(monkeypatch, lambda r: httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": issue}]}))
    out = refund()
    assert out["state"] == "failed" and text in out["why"] and out["ref"] is None

@pytest.mark.parametrize("code", [400, 403, 404, 409, 422])
def test_other_4xx_are_definite_noes(monkeypatch, code):
    live(monkeypatch, lambda r: httpx.Response(code, json={"name": "X"}))
    assert refund()["state"] == "failed"

def test_4xx_with_a_junk_body_is_still_a_no(monkeypatch):
    live(monkeypatch, lambda r: httpx.Response(422, content=b"<html>nope</html>"))
    assert refund()["state"] == "failed"
    live(monkeypatch, lambda r: httpx.Response(422, json=["not", "a", "dict"]))
    assert refund()["state"] == "failed"

@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_a_refund_that_paypal_marks_failed_is_failed(monkeypatch, status):
    live(monkeypatch, lambda r: httpx.Response(201, json={"id": "R", "status": status}))
    assert refund()["state"] == "failed"

# ---------- unknown: we can't tell, so the caller must retry with the same request id and never assume ----------
@pytest.mark.parametrize("code", [401, 408, 429, 500, 502, 503, 504])
def test_busy_or_broken_paypal_is_unknown_not_failed(monkeypatch, code):
    live(monkeypatch, lambda r: httpx.Response(code, json={"name": "X"}))
    assert refund()["state"] == "unknown"

@pytest.mark.parametrize("reply", [httpx.Response(201, content=b"not json"), httpx.Response(201, json={}), httpx.Response(201, json={"id": "R"}),
                                   httpx.Response(201, json={"status": "COMPLETED"}), httpx.Response(201, json={"id": "R", "status": "WEIRD"}),
                                   httpx.Response(200, json=[1]), httpx.Response(302)])
def test_an_unreadable_success_is_unknown(monkeypatch, reply):
    live(monkeypatch, lambda r: reply)
    assert refund()["state"] == "unknown"

@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("c"), httpx.RemoteProtocolError("p")])
def test_network_trouble_is_unknown(monkeypatch, exc):
    def boom(r): raise exc
    live(monkeypatch, boom)
    assert refund()["state"] == "unknown"

def test_token_trouble_is_unknown_and_nothing_is_sent(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "secret")
    monkeypatch.setitem(paypal._tok, "v", None); monkeypatch.setitem(paypal._tok, "exp", 0.0)
    sent = []
    def handler(req):
        sent.append(req.url.path)
        return httpx.Response(500)
    monkeypatch.setattr(paypal, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert refund()["state"] == "unknown" and not any("refund" in p for p in sent)

# ---------- demo mode ----------
def test_demo_mode_simulates_only_simulated_payments(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False); monkeypatch.delenv("PAYPAL_SECRET", raising=False)
    ok = refund(capture_id="SIMCAP-abc")
    assert ok["state"] == "refunded" and ok["ref"].startswith("SIMREF-")
    assert refund(capture_id="REAL123")["state"] == "failed"

def test_a_simulated_payment_is_never_sent_to_live_paypal(monkeypatch):
    seen = live(monkeypatch, lambda r: httpx.Response(201, json={"id": "R", "status": "COMPLETED"}))
    assert refund(capture_id="SIMCAP-abc")["state"] == "failed" and not seen

# ---------- parsing PayPal's order ----------
def order(captures=None, refunds=None, value="50.00"):
    pays = {}
    if captures is not None: pays["captures"] = captures
    if refunds is not None: pays["refunds"] = refunds
    return {"id": "O1", "status": "COMPLETED", "purchase_units": [{"amount": {"currency_code": "USD", "value": value}, "payments": pays}]}

def test_order_capture_reads_the_payment_and_what_was_refunded():
    d = order([{"id": "CAP9", "status": "PARTIALLY_REFUNDED", "amount": {"value": "50.00"}}],
              [{"id": "R1", "status": "COMPLETED", "amount": {"value": "10.00"}}, {"id": "R2", "status": "PENDING", "amount": {"value": "5.50"}},
               {"id": "R3", "status": "FAILED", "amount": {"value": "99.00"}}])
    assert paypal.order_capture(d) == {"capture_id": "CAP9", "status": "PARTIALLY_REFUNDED", "amount": Decimal("50.00"), "refunded": Decimal("15.50")}

@pytest.mark.parametrize("bad", [None, [], "x", {}, {"purchase_units": []}, order(), order([]), order([{"status": "COMPLETED"}]),
                                 order([{"id": "C", "amount": {"value": "abc"}}]), order([{"id": "C", "amount": {}}])])
def test_order_capture_rejects_junk(bad):
    assert paypal.order_capture(bad) is None

def test_order_refunds_lists_them_with_our_invoice_ids():
    d = order([{"id": "C", "amount": {"value": "50"}}], [{"id": "R1", "status": "COMPLETED", "amount": {"value": "10.00"}, "invoice_id": "stash-refund-3"}])
    assert paypal.order_refunds(d) == [{"id": "R1", "status": "COMPLETED", "amount": Decimal("10.00"), "invoice_id": "stash-refund-3"}]

@pytest.mark.parametrize("bad", [None, [], {}, order(), order([], []), order(None, [{"id": "R", "amount": {"value": "x"}}]), order(None, [{"id": "R"}])])
def test_order_refunds_never_raises(bad):
    assert paypal.order_refunds(bad) == []
