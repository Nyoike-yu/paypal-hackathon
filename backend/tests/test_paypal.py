import json, httpx, pytest, paypal

def client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))

@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "secret"); monkeypatch.delenv("PAYPAL_ENV", raising=False)
    paypal._tok.update(v=None, exp=0.0)

def test_create_order_sends_the_right_request(monkeypatch):
    seen = {}
    def h(req):
        if req.url.path == "/v1/oauth2/token": return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        seen["auth"] = req.headers["authorization"]; seen["rid"] = req.headers["paypal-request-id"]; seen["body"] = json.loads(req.content)
        return httpx.Response(201, json={"id": "ORD1", "links": [{"rel": "self", "href": "x"}, {"rel": "payer-action", "href": "https://paypal.test/approve"}]})
    monkeypatch.setattr(paypal, "_client", client(h))
    oid, url = paypal.create_order(50, "Add money", "https://app/?paypal=return", "https://app/?paypal=cancel", request_id="r1")
    assert (oid, url) == ("ORD1", "https://paypal.test/approve") and seen["auth"] == "Bearer T" and seen["rid"] == "r1"
    b = seen["body"]; assert b["intent"] == "CAPTURE" and b["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "50.00"}
    assert b["payment_source"]["paypal"]["experience_context"]["return_url"] == "https://app/?paypal=return"

def test_token_is_cached(monkeypatch):
    calls = {"n": 0}
    def h(req):
        if req.url.path == "/v1/oauth2/token": calls["n"] += 1; return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        return httpx.Response(200, json={"batch_header": {"payout_batch_id": "B1"}})
    monkeypatch.setattr(paypal, "_client", client(h))
    paypal.payout("m@x.com", 5, "n", "b1"); paypal.payout("m@x.com", 6, "n", "b2")
    assert calls["n"] == 1

def test_capture_success_and_failure(monkeypatch):
    def ok(req):
        if req.url.path == "/v1/oauth2/token": return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        return httpx.Response(201, json={"status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": "C1", "status": "COMPLETED", "amount": {"value": "50.00", "currency_code": "USD"}}]}}]})
    monkeypatch.setattr(paypal, "_client", client(ok))
    r = paypal.capture_order("ORD1"); assert r["ok"] and str(r["amount"]) == "50.00" and r["capture_id"] == "C1"
    def not_approved(req):
        if req.url.path == "/v1/oauth2/token": return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]})
    monkeypatch.setattr(paypal, "_client", client(not_approved))
    with pytest.raises(paypal.PayPalError): paypal.capture_order("ORD2")

def test_payout_and_webhook(monkeypatch):
    def h(req):
        if req.url.path == "/v1/oauth2/token": return httpx.Response(200, json={"access_token": "T", "expires_in": 300})
        if req.url.path == "/v1/payments/payouts":
            item = json.loads(req.content)["items"][0]; assert item["receiver"] == "m@x.com" and item["amount"]["value"] == "9.99"
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B9"}})
        return httpx.Response(200, json={"verification_status": "SUCCESS"})
    monkeypatch.setattr(paypal, "_client", client(h))
    assert paypal.payout("m@x.com", "9.99", "Art Kit", "purchase-1") == "B9"
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH")
    assert paypal.verify_webhook({"paypal-auth-algo": "a"}, {"id": "e"}) is True
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID"); assert paypal.verify_webhook({}, {}) is False

def test_simulated_mode_without_keys(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID"); monkeypatch.delenv("PAYPAL_SECRET")
    oid, url = paypal.create_order(10, "x", "http://app/?paypal=return", "http://app/?paypal=cancel")
    assert oid.startswith("SIM-") and f"token={oid}" in url and paypal.capture_order(oid)["ok"]
    with pytest.raises(paypal.PayPalError): paypal.capture_order("REAL123")
    assert paypal.payout("", 5, "n", "b").startswith("SIM-PAYOUT")
