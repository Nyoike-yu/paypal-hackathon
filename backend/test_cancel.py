"""Cancelling an order: from the card (POST /orders/{id}/cancel) and by telling Penny. Real app, real Postgres, a scripted fake model."""
from types import SimpleNamespace
import pytest
import main
from test_chat_memory import Script, chat, login

@pytest.fixture(autouse=True)
def clean(client):
    """Every test starts with nothing in flight for Sam and Maya, and a fresh rate limit."""
    main._hits.clear(); main.OFFERED.clear()
    main.q("update purchases set fulfillment='delivered' where status='completed' and fulfillment in ('processing','shipped')")
    main.q("update purchases set status='declined' where status='pending'")
    main.q("update profiles set balance=100 where name in ('Sam','Maya')")
    yield
    main._hits.clear()

@pytest.fixture
def people(client):
    sam, hs = login(client, "Sam"); maya, hm = login(client, "Maya")
    for h in (hs, hm): client.post("/chat/clear", headers=h)
    return SimpleNamespace(sam=sam, hs=hs, maya=maya, hm=hm)

def pid(name): return main.q("select id from products where name=%s", (name,), one=True)["id"]
def bal(p): return float(main.q("select balance from profiles where id=%s", (p,), one=True)["balance"])
def buy(client, h, p, name):
    r = client.post("/purchase", json={"profile_id": p, "product_id": pid(name)}, headers=h); assert r.status_code == 200, r.text
    return r.json()

# ---------- the button ----------
def test_cancelling_a_paid_order_puts_the_money_back(client, people):
    o = buy(client, people.hs, people.sam, "Art Kit"); oid = o["order"]["id"]
    assert o["status"] == "completed" and bal(people.sam) == 100 - 9.99
    r = client.post(f"/orders/{oid}/cancel", headers=people.hs); assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "cancelled" and float(out["refunded"]) == 9.99 and out["order"]["status"] == "cancelled" and bal(people.sam) == 100
    assert main.q("select fulfillment from purchases where id=%s", (oid,), one=True)["fulfillment"] == "cancelled"
    assert "went back to the wallet" in main.q("select trace from purchases where id=%s", (oid,), one=True)["trace"][-1]

def test_a_double_tap_refunds_once(client, people):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    assert client.post(f"/orders/{oid}/cancel", headers=people.hs).status_code == 200
    assert client.post(f"/orders/{oid}/cancel", headers=people.hs).status_code == 409 and bal(people.sam) == 100

def test_a_shipped_order_can_still_be_cancelled_but_a_delivered_one_cannot(client, people):
    a = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]; b = buy(client, people.hs, people.sam, "Soccer Ball")["order"]["id"]
    main.q("update purchases set fulfillment='shipped' where id=%s", (a,)); main.q("update purchases set fulfillment='delivered' where id=%s", (b,))
    assert client.post(f"/orders/{a}/cancel", headers=people.hs).status_code == 200
    r = client.post(f"/orders/{b}/cancel", headers=people.hs)
    assert r.status_code == 409 and bal(people.sam) == 100 - 14.50

def test_nobody_can_cancel_someone_elses_order(client, people):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    assert client.post(f"/orders/{oid}/cancel", headers=people.hm).status_code == 403  # Maya is not Sam's guardian
    assert client.post("/orders/999999/cancel", headers=people.hs).status_code == 404 and bal(people.sam) == 100 - 9.99

def test_a_grown_up_can_cancel_their_childs_order_and_the_child_is_told(client, people):
    oid = buy(client, people.hm, people.maya, "Art Kit")["order"]["id"]
    before = bal(people.maya)
    r = client.post(f"/orders/{oid}/cancel", headers=people.hs); assert r.status_code == 200, r.text
    assert bal(people.maya) == before + 9.99
    assert any("cancelled" in a["body"] for a in main.q("select body from alerts where profile_id=%s order by id desc limit 3", (people.maya,)))

def test_a_child_cancelling_tells_their_grown_up(client, people):
    oid = buy(client, people.hm, people.maya, "Art Kit")["order"]["id"]
    assert client.post(f"/orders/{oid}/cancel", headers=people.hm).status_code == 200
    inbox = client.get("/inbox", headers=people.hs).json()
    assert any(a["event"] == "cancelled" and a["who"] == "Maya" for a in inbox["activity"])

def test_a_request_waiting_for_approval_is_withdrawn_and_cannot_then_be_approved(client, people):
    o = buy(client, people.hm, people.maya, "Robot Dino")  # $24.99 is over Maya's $15 approval line
    assert o["status"] == "pending"; oid = o["order"]["id"]; before = bal(people.maya)
    r = client.post(f"/orders/{oid}/cancel", headers=people.hm).json()
    assert r["status"] == "cancelled" and float(r["refunded"]) == 0 and bal(people.maya) == before and r["note"] == "Nothing was charged."
    assert client.post(f"/approvals/{oid}/approve", headers=people.hs).status_code == 404 and bal(people.maya) == before

def test_an_approved_order_cannot_be_withdrawn_as_if_it_were_pending(client, people):
    o = buy(client, people.hm, people.maya, "Robot Dino"); oid = o["order"]["id"]
    assert client.post(f"/approvals/{oid}/approve", headers=people.hs).status_code == 200
    r = client.post(f"/orders/{oid}/cancel", headers=people.hm).json()
    assert float(r["refunded"]) == 24.99  # it was paid, so it is a refund to the wallet, not a withdrawal

def test_cancelling_is_rate_limited(client, people):
    codes = [client.post("/orders/999999/cancel", headers=people.hs).status_code for _ in range(12)]
    assert codes[-1] == 429

def test_cancelled_orders_stay_on_the_list_for_the_card(client, people):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    client.post(f"/orders/{oid}/cancel", headers=people.hs)
    rows = client.get(f"/orders/{people.sam}", headers=people.hs).json()
    assert next(r for r in rows if r["id"] == oid)["status"] == "cancelled"

# ---------- telling Penny ----------
def test_penny_will_not_cancel_without_a_yes(client, people, monkeypatch):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    fake = Script(("tool", "cancel_order", {}), ("text", "Want me to cancel it?"))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hs, people.sam, "cancel my order")
    assert "hasn't said yes" in fake.results[0]["error"] and main.q("select status from purchases where id=%s", (oid,), one=True)["status"] == "completed"

def test_penny_cancels_after_a_yes_and_the_card_shows_it(client, people, monkeypatch):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    fake = Script(("tool", "cancel_order", {}), ("text", "Done. The Art Kit is cancelled."))
    monkeypatch.setattr(main, "ai", fake)
    out = chat(client, people.hs, people.sam, "yes, cancel it")
    assert fake.results[0]["status"] == "cancelled" and out["order"]["status"] == "cancelled" and out["order"]["id"] == oid and bal(people.sam) == 100
    nxt = Script(("text", "ok")); monkeypatch.setattr(main, "ai", nxt)
    chat(client, people.hs, people.sam, "what happened to it?")
    assert "Cancelled the order for Art Kit" in nxt.requests[0]["system"]  # remembered

def test_with_two_candidates_penny_has_to_ask_which(client, people, monkeypatch):
    a = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]; b = buy(client, people.hs, people.sam, "Soccer Ball")["order"]["id"]
    fake = Script(("tool", "cancel_order", {}), ("text", "Which one?"))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hs, people.sam, "yes cancel my order")
    err = fake.results[0]["error"]
    assert "Ask which one" in err and "Art Kit" in err and "Soccer Ball" in err and bal(people.sam) == 100 - 9.99 - 14.50
    fake = Script(("tool", "cancel_order", {"order_id": b}), ("text", "Cancelled the ball."))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hs, people.sam, "yes the soccer ball")
    assert fake.results[0]["status"] == "cancelled" and main.q("select status from purchases where id=%s", (a,), one=True)["status"] == "completed"

def test_penny_cannot_cancel_another_persons_order(client, people, monkeypatch):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    fake = Script(("tool", "cancel_order", {"order_id": oid}), ("text", "I can't find it."))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hm, people.maya, "yes cancel order")
    assert "can't find" in fake.results[0]["error"] and main.q("select status from purchases where id=%s", (oid,), one=True)["status"] == "completed"

def test_penny_cannot_cancel_a_delivered_order_and_garbage_input_is_safe(client, people, monkeypatch):
    oid = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]
    main.q("update purchases set fulfillment='delivered' where id=%s", (oid,))
    fake = Script(("tool", "cancel_order", {"order_id": oid}), ("tool", "cancel_order", {"order_id": "abc"}), ("tool", "cancel_order", {"order_id": ["x"]}), ("text", "Sorry."))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hs, people.sam, "yes cancel it")
    assert all("error" in r for r in fake.results) and bal(people.sam) == 100 - 9.99

def test_list_orders_tells_penny_what_can_be_cancelled(client, people, monkeypatch):
    a = buy(client, people.hs, people.sam, "Art Kit")["order"]["id"]; b = buy(client, people.hs, people.sam, "Soccer Ball")["order"]["id"]
    main.q("update purchases set fulfillment='delivered' where id=%s", (b,))
    fake = Script(("tool", "list_orders", {}), ("text", "Here they are."))
    monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hs, people.sam, "what are my orders")
    by_id = {o["order_id"]: o for o in fake.results[0]["orders"]}
    assert by_id[a]["can_cancel"] is True and by_id[b]["can_cancel"] is False

def test_children_get_the_cancel_tools_too(client, people, monkeypatch):
    fake = Script(("text", "hi")); monkeypatch.setattr(main, "ai", fake)
    chat(client, people.hm, people.maya, "hello")
    names = [t["name"] for t in fake.requests[0]["tools"]]
    assert "cancel_order" in names and "list_orders" in names and "request_refund" not in names
