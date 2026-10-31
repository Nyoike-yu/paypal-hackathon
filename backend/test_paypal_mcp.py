"""Tests for the PayPal MCP bridge. No database, no network, no PayPal keys: a stand-in MCP server (fake_mcp_server.py) plays PayPal's."""
import os, sys, time, json, pathlib
import pytest
import paypal, paypal_mcp

HERE = pathlib.Path(__file__).parent
ORDER = {"id": "ORD1", "status": "COMPLETED", "create_time": "2026-10-30T10:00:00Z",
         "payer": {"email_address": "private@example.com", "name": {"given_name": "Secret"}},
         "purchase_units": [{"amount": {"currency_code": "USD", "value": "20.00"}, "shipping": {"address": {"address_line_1": "1 Private Street"}},
                             "payments": {"captures": [{"id": "CAP1", "status": "COMPLETED"}]}}]}

# ---------- pure functions ----------
def test_order_summary_keeps_only_safe_fields():
    s = paypal.order_summary(ORDER)
    assert s == {"paypal_status": "COMPLETED", "amount": "20.00", "currency": "USD", "created": "2026-10-30T10:00:00Z", "capture_status": "COMPLETED"}
    blob = json.dumps(s)
    assert "private@example.com" not in blob and "Secret" not in blob and "Private Street" not in blob

@pytest.mark.parametrize("bad", [None, [], "x", {}, {"status": ""}, {"status": "COMPLETED", "purchase_units": [{"amount": {"value": "abc"}}]}])
def test_order_summary_rejects_junk(bad):
    assert paypal.order_summary(bad) is None

def test_order_summary_without_amount_is_fine():
    assert paypal.order_summary({"status": "CREATED"})["amount"] is None

def test_parse_order_accepts_json_inside_text():
    assert paypal_mcp.parse_order("Order: " + json.dumps(ORDER) + " done", "ORD1")["paypal_status"] == "COMPLETED"

@pytest.mark.parametrize("text", ["", "no json here", "{not json}", "[1,2]", json.dumps({"id": "OTHER", "status": "COMPLETED"}), json.dumps({"id": "ORD1"})])
def test_parse_order_rejects_bad_replies(text):
    with pytest.raises(paypal.PayPalError):
        paypal_mcp.parse_order(text, "ORD1")

def test_demo_mode_get_order(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False); monkeypatch.delenv("PAYPAL_SECRET", raising=False)
    assert paypal.get_order("SIM-abc")["status"] == "SIMULATED"
    with pytest.raises(paypal.PayPalError): paypal.get_order("REAL-123")

def test_child_process_never_gets_our_secrets(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "DATABASE_URL", "SECRET_KEY", "PAYPAL_SECRET", "PAYPAL_CLIENT_ID", "VAPID_PRIVATE_KEY"): monkeypatch.setenv(k, "top-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128"); monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/ca.pem"); monkeypatch.setenv("PAYPAL_ENV", "live")
    env = paypal_mcp._child_env("tok")
    assert env["PAYPAL_ACCESS_TOKEN"] == "tok" and env["PAYPAL_ENVIRONMENT"] == "PRODUCTION"
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["NODE_EXTRA_CA_CERTS"] == "/ca.pem"
    assert "top-secret" not in " ".join(env.values())

# ---------- start() must be harmless whenever the feature isn't set up ----------
def _fresh(monkeypatch):
    monkeypatch.setattr(paypal_mcp, "_thread", None); monkeypatch.setattr(paypal_mcp, "_loop", None)

def test_start_is_a_noop_without_keys(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False); monkeypatch.delenv("PAYPAL_SECRET", raising=False)
    paypal_mcp.start()
    assert not paypal_mcp.available() and paypal_mcp._thread is None
    with pytest.raises(paypal.PayPalError): paypal_mcp.get_order("ORD1")

def test_start_is_a_noop_when_switched_off(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "s"); monkeypatch.setenv("PAYPAL_MCP", "off")
    paypal_mcp.start()
    assert paypal_mcp._thread is None

def test_start_is_a_noop_when_the_command_is_missing(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "s"); monkeypatch.setenv("PAYPAL_MCP_COMMAND", "definitely-not-a-real-program")
    paypal_mcp.start()
    assert paypal_mcp._thread is None

# ---------- the live bridge, against the stand-in server ----------
@pytest.fixture
def bridge(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "s"); monkeypatch.delenv("PAYPAL_MCP", raising=False)
    monkeypatch.setenv("PAYPAL_MCP_COMMAND", f'"{sys.executable}" "{HERE / "fake_mcp_server.py"}"')
    monkeypatch.setattr(paypal, "fresh_token", lambda: "test-token")
    paypal_mcp.start()
    for _ in range(300):
        if paypal_mcp.available(): break
        time.sleep(0.1)
    assert paypal_mcp.available(), "the bridge never connected"
    yield paypal_mcp
    paypal_mcp.stop()

def test_bridge_returns_a_safe_summary(bridge):
    s = bridge.get_order("ORD42")
    assert s["paypal_status"] == "COMPLETED" and s["amount"] == "20.00" and s["capture_status"] == "COMPLETED"
    assert "private@example.com" not in json.dumps(s)

@pytest.mark.parametrize("oid", ["NOT-JSON", "OTHER-ORDER", "BAD-AUTH"])
def test_bridge_turns_every_failure_into_paypalerror(bridge, oid):
    with pytest.raises(paypal.PayPalError):
        bridge.get_order(oid)

def test_bridge_still_works_after_a_failed_call(bridge):
    with pytest.raises(paypal.PayPalError): bridge.get_order("BAD-AUTH")
    assert bridge.get_order("ORD43")["paypal_status"] == "COMPLETED"

def test_stop_is_clean_and_lookups_then_fail_soft(monkeypatch):
    _fresh(monkeypatch)
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "id"); monkeypatch.setenv("PAYPAL_SECRET", "s")
    monkeypatch.setenv("PAYPAL_MCP_COMMAND", f'"{sys.executable}" "{HERE / "fake_mcp_server.py"}"')
    monkeypatch.setattr(paypal, "fresh_token", lambda: "t")
    paypal_mcp.start()
    for _ in range(300):
        if paypal_mcp.available(): break
        time.sleep(0.1)
    assert paypal_mcp.available()
    paypal_mcp.stop()
    assert not paypal_mcp.available()
    with pytest.raises(paypal.PayPalError): paypal_mcp.get_order("ORD1")
