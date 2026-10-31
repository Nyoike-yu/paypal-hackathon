"""End-to-end tests for Penny's PayPal top-up lookups: real FastAPI app, real Postgres (pgserver from requirements-dev.txt), a fake model."""
import os, json
from types import SimpleNamespace
import pytest

from fastapi import HTTPException
import main, paypal, paypal_mcp

def login(client, name, secret="demo"):
    r = client.post("/login", json={"name": name, "secret": secret}); assert r.status_code == 200, r.text
    return r.json()["id"], {"Authorization": "Bearer " + r.json()["token"]}

def db_order(pid, amount, status="created"):
    """Insert a top-up row directly, so a test can set it up even while PayPal is pretended to be live."""
    import uuid
    oid = "SIM-" + uuid.uuid4().hex[:12]
    main.q("insert into pay_orders(order_id, profile_id, amount, status) values(%s,%s,%s,%s)", (oid, pid, amount, status))
    return oid

def make_topup(client, h, amount=20):
    r = client.post("/funds/create", json={"amount": amount}, headers=h); assert r.status_code == 200, r.text
    return r.json()["order_id"]

@pytest.fixture(scope="module")
def world(client):
    sam, hs = login(client, "Sam")
    r = client.post("/register", json={"name": "Alex", "secret": "alexpass"}); assert r.status_code == 200, r.text
    alex, ha = login(client, "Alex", "alexpass")
    maya, hm = login(client, "Maya")
    return SimpleNamespace(sam=sam, hs=hs, alex=alex, ha=ha, maya=maya, hm=hm,
                           sam_order=make_topup(client, hs, 20), alex_order=make_topup(client, ha, 35))

@pytest.fixture
def live(monkeypatch):
    """Pretend PayPal keys are set, and record every call that would reach PayPal."""
    monkeypatch.setattr(paypal, "configured", lambda: True)
    calls = SimpleNamespace(mcp=[], api=[])
    return calls

SUMMARY = {"paypal_status": "COMPLETED", "amount": "20.00", "currency": "USD", "created": "2026-10-30T10:00:00Z", "capture_status": "COMPLETED"}

# ---------- demo mode (no keys at all) ----------
def test_demo_mode_lookup_works_with_no_keys(client, world):
    out = main.topup_lookup(world.sam, world.sam_order)
    assert out["order_id"] == world.sam_order and float(out["amount"]) == 20.0
    assert out["paypal_status"] == "SIMULATED" and "demo mode" in out["checked_via"]

def test_latest_is_used_when_no_id_given(client, world):
    newest = make_topup(client, world.hs, 7)
    assert main.topup_lookup(world.sam)["order_id"] == newest

def test_list_my_topups(client, world):
    out = main.topup_list(world.sam)
    assert out["topups"][0]["app_status"] in ("created", "captured") and all(len(t["started"]) == 10 for t in out["topups"])
    ids = {t["order_id"] for t in out["topups"]}
    assert world.sam_order in ids and world.alex_order not in ids

def test_list_is_empty_for_someone_with_no_topups(client, world):
    assert main.topup_list(world.maya)["topups"] == []

# ---------- ownership: the PayPal account is shared, the answer must not be ----------
def test_cannot_look_up_someone_elses_order_and_paypal_is_never_asked(client, world, live, monkeypatch):
    monkeypatch.setattr(paypal_mcp, "get_order", lambda oid: live.mcp.append(oid) or SUMMARY)
    monkeypatch.setattr(paypal, "get_order", lambda oid: live.api.append(oid) or {"status": "COMPLETED"})
    with pytest.raises(HTTPException) as e:
        main.topup_lookup(world.sam, world.alex_order)
    assert e.value.status_code == 404 and not live.mcp and not live.api

def test_nobody_with_no_topups_gets_a_clear_404(client, world, live):
    with pytest.raises(HTTPException) as e: main.topup_lookup(world.maya)
    assert e.value.status_code == 404

@pytest.mark.parametrize("bad", ["../../v1/payments", "abc def", "a" * 80, "x?y=1", "ab"])
def test_odd_order_ids_are_rejected_before_any_call(client, world, live, monkeypatch, bad):
    monkeypatch.setattr(paypal_mcp, "get_order", lambda oid: live.mcp.append(oid) or SUMMARY)
    with pytest.raises(HTTPException) as e: main.topup_lookup(world.sam, bad)
    assert e.value.status_code == 422 and not live.mcp

# ---------- the three tiers: MCP, then the REST API, then our own record ----------
def test_tier1_mcp(client, world, live, monkeypatch):
    monkeypatch.setattr(paypal_mcp, "get_order", lambda oid: live.mcp.append(oid) or dict(SUMMARY))
    monkeypatch.setattr(paypal, "get_order", lambda oid: live.api.append(oid) or {})
    out = main.topup_lookup(world.sam, world.sam_order)
    assert out["checked_via"] == "PayPal's MCP server" and out["paypal_status"] == "COMPLETED" and live.mcp == [world.sam_order] and not live.api

def test_tier2_api_when_mcp_is_down(client, world, live, monkeypatch):
    def down(oid): raise paypal.PayPalError("not connected")
    monkeypatch.setattr(paypal_mcp, "get_order", down)
    monkeypatch.setattr(paypal, "get_order", lambda oid: {"id": oid, "status": "APPROVED", "purchase_units": [{"amount": {"currency_code": "USD", "value": "20.00"}}]})
    out = main.topup_lookup(world.sam, world.sam_order)
    assert out["checked_via"] == "the PayPal API" and out["paypal_status"] == "APPROVED" and out["amount"] == "20.00"

def test_tier3_own_record_when_paypal_is_unreachable(client, world, live, monkeypatch):
    def down(oid): raise paypal.PayPalError("down")
    monkeypatch.setattr(paypal_mcp, "get_order", down); monkeypatch.setattr(paypal, "get_order", down)
    out = main.topup_lookup(world.sam, world.sam_order)
    assert out["checked_via"] == "the app's own records" and "paypal_status" not in out and "Couldn't reach PayPal" in out["note"]

def test_tier3_when_paypal_returns_junk(client, world, live, monkeypatch):
    def down(oid): raise paypal.PayPalError("down")
    monkeypatch.setattr(paypal_mcp, "get_order", down); monkeypatch.setattr(paypal, "get_order", lambda oid: {"nope": 1})
    assert main.topup_lookup(world.sam, world.sam_order)["checked_via"] == "the app's own records"

def test_mismatch_note_when_paypal_says_completed_but_app_has_not_credited(client, world, live, monkeypatch):
    oid = db_order(world.sam, 11, "created")  # PayPal will say COMPLETED while our table still says created
    monkeypatch.setattr(paypal_mcp, "get_order", lambda o: dict(SUMMARY))
    assert "hasn't added the money" in main.topup_lookup(world.sam, oid)["note"]

def test_no_note_when_everything_agrees(client, world, live, monkeypatch):
    oid = db_order(world.sam, 12, "captured")
    monkeypatch.setattr(paypal_mcp, "get_order", lambda o: dict(SUMMARY))
    out = main.topup_lookup(world.sam, oid)
    assert out["app_status"] == "captured" and "note" not in out

# ---------- the chat path with a fake model ----------
class FakeAI:
    """Plays Claude: first asks for the given tool calls, then answers with the tool results it was given."""
    def __init__(self, *calls):
        self.calls, self.seen_tools, self.results = list(calls), None, []
        self.messages = SimpleNamespace(create=self._create)
    def _create(self, **kw):
        self.seen_tools = [t["name"] for t in kw["tools"]]
        last = kw["messages"][-1]
        if isinstance(last["content"], list) and last["content"] and isinstance(last["content"][0], dict) and last["content"][0].get("type") == "tool_result":
            self.results = [json.loads(r["content"]) for r in last["content"]]
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])
        if self.calls:
            return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id=f"t{i}", name=n, input=inp) for i, (n, inp) in enumerate(self.calls)])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])

def chat(client, h, pid, text):
    r = client.post("/chat", json={"profile_id": pid, "message": text}, headers=h); assert r.status_code == 200, r.text
    return r.json()

def test_grownup_chat_calls_lookup_and_gets_a_safe_result(client, world, live, monkeypatch):
    monkeypatch.setattr(paypal_mcp, "get_order", lambda o: dict(SUMMARY))
    fake = FakeAI(("lookup_topup", {}), ("list_my_topups", {}))
    monkeypatch.setattr(main, "ai", fake)
    assert chat(client, world.hs, world.sam, "did my last top-up go through?")["reply"] == "done"
    assert "lookup_topup" in fake.seen_tools and "list_my_topups" in fake.seen_tools
    look, lst = fake.results
    assert look["paypal_status"] == "COMPLETED" and "error" not in look and "topups" in lst

def test_chat_survives_a_lookup_for_someone_elses_order(client, world, live, monkeypatch):
    fake = FakeAI(("lookup_topup", {"order_id": world.alex_order}))
    monkeypatch.setattr(main, "ai", fake)
    assert chat(client, world.hs, world.sam, "check order")["reply"] == "done"
    assert "can't find" in fake.results[0]["error"]

def test_chat_survives_garbage_tool_input(client, world, live, monkeypatch):
    fake = FakeAI(("lookup_topup", {"order_id": 12345}), ("lookup_topup", {"order_id": None}), ("lookup_topup", {"order_id": ["x"]}))
    monkeypatch.setattr(main, "ai", fake)
    assert chat(client, world.hs, world.sam, "check")["reply"] == "done"
    assert len(fake.results) == 3

def test_children_do_not_get_the_tools_and_cannot_call_them(client, world, monkeypatch):
    fake = FakeAI(("lookup_topup", {}), ("list_my_topups", {}))
    monkeypatch.setattr(main, "ai", fake)
    assert chat(client, world.hm, world.maya, "did my top-up go through?")["reply"] == "done"
    assert "lookup_topup" not in fake.seen_tools and "list_my_topups" not in fake.seen_tools and "set_goal" in fake.seen_tools
    assert [r["error"] for r in fake.results] == ["Unknown tool", "Unknown tool"]

def test_the_other_tools_still_offered_to_grownups(client, world, monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(main, "ai", fake)
    chat(client, world.hs, world.sam, "hi")
    assert {"search_products", "search_stores", "start_mission", "buy", "lookup_topup"} <= set(fake.seen_tools) and "set_goal" not in fake.seen_tools

def test_system_prompt_mentions_topup_rules_for_grownups_only(client, world):
    import agent
    sam = main.q("select * from profiles where id=%s", (world.sam,), one=True); maya = main.q("select * from profiles where id=%s", (world.maya,), one=True)
    rules = lambda pid: main.q("select * from rules where profile_id=%s", (pid,), one=True)
    assert "lookup_topup" in agent.build_system(sam, rules(world.sam), None)
    assert "lookup_topup" not in agent.build_system(maya, rules(world.maya), None)

# ---------- health, and the app still works as before ----------
def test_health_reports_mcp_state(client):
    h = client.get("/health").json()
    assert h["ok"] is True and h["paypal_mcp"] is False

def test_existing_topup_flow_unchanged(client, world):
    oid = make_topup(client, world.hs, 5)
    before = float(main.q("select balance from profiles where id=%s", (world.sam,), one=True)["balance"])
    assert client.post("/funds/capture", json={"order_id": oid}, headers=world.hs).json() == {"ok": True, "status": "captured"}
    assert float(main.q("select balance from profiles where id=%s", (world.sam,), one=True)["balance"]) == before + 5
