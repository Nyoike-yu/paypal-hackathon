"""Refunds end to end: real FastAPI app and Postgres, PayPal played by a script. The rule being tested everywhere:
money must never be lost, created or sent twice, whatever PayPal does."""
import json, uuid, threading
from decimal import Decimal
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
import main, paypal, paypal_mcp

D = Decimal
R_OK = {"state": "refunded", "ref": "REF-1", "why": None}
R_FAIL = {"state": "failed", "ref": None, "why": "PayPal says this payment was already refunded in full."}
R_UNK = {"state": "unknown", "ref": None, "why": None}

# ---------- setup helpers ----------
def adult(client, balance=100):
    name = "Ref" + uuid.uuid4().hex[:8]
    assert client.post("/register", json={"name": name, "secret": "pw123456"}).status_code == 200
    r = client.post("/login", json={"name": name, "secret": "pw123456"}); assert r.status_code == 200, r.text
    me = SimpleNamespace(id=r.json()["id"], h={"Authorization": "Bearer " + r.json()["token"]})
    main.q("update profiles set balance=%s where id=%s", (balance, me.id))
    return me

def order(pid, amount=50, status="captured", live=False, days_old=0):
    oid = ("LIVE-" if live else "SIM-") + uuid.uuid4().hex[:10]
    main.q("insert into pay_orders(order_id, profile_id, amount, status, created_at) values(%s,%s,%s,%s, now() - %s::interval)", (oid, pid, amount, status, f"{days_old} days"))
    return oid

bal = lambda pid: main.q("select balance from profiles where id=%s", (pid,), one=True)["balance"]
rows = lambda oid: main.q("select * from refunds where order_id=%s order by id", (oid,))
proposal = lambda oid: main.q("select * from proposals where kind='refund' and data->>'order_id'=%s order by id desc limit 1", (oid,), one=True)
accept = lambda client, me, p: client.post(f"/proposals/{p['id']}/accept", json={}, headers=me.h)
alerts_of = lambda pid, title: main.q("select * from alerts where profile_id=%s and title=%s", (pid, title))

@pytest.fixture(autouse=True)
def clean():
    main.q("delete from refunds"); main.q("delete from proposals where kind='refund'")
    yield
    main.q("delete from refunds"); main.q("delete from proposals where kind='refund'")

class FakePayPal:
    """PayPal as a script: what an order looks like, which of MCP / the API is reachable, and what each refund call answers."""
    def __init__(self):
        self.mcp_up = self.api_up = True; self.cap_status = "COMPLETED"; self.paypal_refunds = []; self.replies = []; self.calls = []; self.reads = []
    def _order(self, oid):
        amt = str(main.q("select amount from pay_orders where order_id=%s", (oid,), one=True)["amount"])
        return {"id": oid, "status": "COMPLETED", "purchase_units": [{"amount": {"currency_code": "USD", "value": amt}, "payments": {
            "captures": [{"id": "CAP-" + oid, "status": self.cap_status, "amount": {"currency_code": "USD", "value": amt}}], "refunds": self.paypal_refunds}}]}
    def mcp(self, oid, timeout=15):
        if not self.mcp_up: raise paypal.PayPalError("mcp down")
        self.reads.append("mcp"); return self._order(oid)
    def api(self, oid):
        if not self.api_up: raise paypal.PayPalError("api down")
        self.reads.append("api"); return self._order(oid)
    def refund(self, capture_id, amount, note, request_id, invoice_id):
        self.calls.append({"capture_id": capture_id, "amount": amount, "request_id": request_id, "invoice_id": invoice_id, "note": note})
        return self.replies.pop(0) if self.replies else R_OK
    def already(self, amount, invoice_id=None):  # PayPal lists a refund
        self.paypal_refunds.append({"id": "RX" + uuid.uuid4().hex[:4], "status": "COMPLETED", "amount": {"value": str(amount)}, "invoice_id": invoice_id})

@pytest.fixture
def fp(monkeypatch):
    f = FakePayPal()
    monkeypatch.setattr(paypal, "configured", lambda: True)
    monkeypatch.setattr(paypal_mcp, "get_order_json", f.mcp); monkeypatch.setattr(paypal, "get_order", f.api); monkeypatch.setattr(paypal, "refund_capture", f.refund)
    return f

def travel(rid, minutes):
    main.q("update refunds set tried_at = now() - %s::interval where id=%s", (f"{minutes} minutes", rid))

# ---------- the whole flow, in demo mode (no keys) ----------
def test_demo_flow_request_then_approve(client):
    me = adult(client, 30); oid = order(me.id, 50)
    out = main.refund_request(me.id)
    assert out["status"] == "waiting_for_your_approval" and out["refund_amount"] == D("30.00") and out["balance_after"] == D("0")  # the smaller of what's left (50) and the balance (30)
    assert bal(me.id) == 30 and not rows(oid), "filing a request must not move any money"
    p = proposal(oid)
    assert p["status"] == "open" and p["title"] == "Send $30.00 back to your PayPal?" and "Nothing is sent until you tap" in p["trace"][-1]
    r = accept(client, me, p); assert r.status_code == 200 and r.json()["refund"] == "refunded", r.text
    assert bal(me.id) == 0
    (row,) = rows(oid); assert row["status"] == "refunded" and row["paypal_ref"].startswith("SIMREF-") and row["amount"] == D("30.00") and row["capture_id"].startswith("SIMCAP-")
    assert proposal(oid)["status"] == "accepted"
    assert main.q("select 1 from audit where profile_id=%s and detail like '%%refunded to PayPal%%'", (me.id,), one=True)

def test_a_second_tap_does_nothing(client):
    me = adult(client, 100); oid = order(me.id, 50); main.refund_request(me.id); p = proposal(oid)
    assert accept(client, me, p).status_code == 200
    assert accept(client, me, p).status_code == 404
    assert bal(me.id) == 50 and len(rows(oid)) == 1

def test_declining_moves_nothing(client):
    me = adult(client, 100); oid = order(me.id, 50); main.refund_request(me.id); p = proposal(oid)
    assert client.post(f"/proposals/{p['id']}/decline", json={}, headers=me.h).status_code == 200
    assert bal(me.id) == 100 and not rows(oid) and proposal(oid)["status"] == "declined"

def test_nobody_else_can_approve_my_refund(client):
    me, other = adult(client, 100), adult(client, 100); oid = order(me.id, 50); main.refund_request(me.id); p = proposal(oid)
    assert accept(client, other, p).status_code == 404
    assert bal(me.id) == 100 and bal(other.id) == 100 and not rows(oid)

def test_the_request_shows_up_in_notifications_with_its_reasoning(client):
    me = adult(client, 100); oid = order(me.id, 50); main.refund_request(me.id)
    inbox = client.get("/inbox", headers=me.h).json()
    (p,) = [x for x in inbox["proposals"] if x["kind"] == "refund"]
    assert p["status"] == "open" and p["data"]["order_id"] == oid and len(p["trace"]) >= 3

# ---------- what can be asked for ----------
def test_a_second_request_for_the_same_top_up_reuses_the_first(client):
    me = adult(client, 100); oid = order(me.id, 50)
    main.refund_request(me.id, oid, 10)
    again = main.refund_request(me.id, oid, 25)
    assert again["status"] == "already_waiting" and again["refund_amount"] == D("10")
    assert main.q("select count(*) c from proposals where kind='refund' and status='open' and data->>'order_id'=%s", (oid,), one=True)["c"] == 1

@pytest.mark.parametrize("amount,expected", [(10, "10.00"), ("12.5", "12.50"), (10.999, "10.99"), (None, "40.00"), ("", "40.00")])
def test_amounts_are_cents_rounded_down_and_default_to_the_most_that_can_go(client, amount, expected):
    me = adult(client, 40); oid = order(me.id, 50)
    assert f"{main.refund_request(me.id, oid, amount)['refund_amount']:.2f}" == expected

@pytest.mark.parametrize("amount", [0, -5, "abc", "NaN", "Infinity", "-Infinity", True, "0.001", [1], {"a": 1}])
def test_nonsense_amounts_are_refused(client, amount):
    me = adult(client, 100); oid = order(me.id, 50)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid, amount)
    assert e.value.status_code == 422 and not proposal(oid)

def test_more_than_the_limit_is_refused_and_says_what_the_limit_is(client):
    me = adult(client, 30); oid = order(me.id, 50)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid, 31)
    assert e.value.status_code == 409 and "$30.00" in e.value.detail

def test_cannot_refund_more_than_was_paid_even_with_a_big_balance(client):
    me = adult(client, 500); oid = order(me.id, 50)
    assert main.refund_request(me.id, oid)["refund_amount"] == D("50.00")
    with pytest.raises(HTTPException): main.refund_request(me.id, oid, 50.01)

@pytest.mark.parametrize("kw,code", [({"status": "created"}, 409), ({"status": "failed"}, 409), ({"status": "capturing"}, 409), ({"days_old": 181}, 409)])
def test_top_ups_that_cant_be_refunded(client, kw, code):
    me = adult(client, 100); oid = order(me.id, 50, **kw)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == code and not proposal(oid)

def test_the_180_day_edge(client):
    me = adult(client, 100)
    assert main.refund_request(me.id, order(me.id, 50, days_old=179))["status"] == "waiting_for_your_approval"

def test_no_balance_means_no_refund(client):
    me = adult(client, 0); oid = order(me.id, 50)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and "no money left" in e.value.detail

def test_someone_elses_top_up_looks_like_it_does_not_exist(client):
    me, other = adult(client), adult(client); oid = order(other.id, 50)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 404 and not proposal(oid)

def test_no_top_ups_at_all(client):
    with pytest.raises(HTTPException) as e: main.refund_request(adult(client).id)
    assert e.value.status_code == 404

@pytest.mark.parametrize("bad", ["../x", "a b", "x" * 70, "ab", "x?y"])
def test_odd_ids_are_refused(client, bad):
    with pytest.raises(HTTPException) as e: main.refund_request(adult(client).id, bad)
    assert e.value.status_code == 422

def test_no_oid_picks_the_latest_completed_top_up(client):
    me = adult(client, 100); old = order(me.id, 20, days_old=3); order(me.id, 99, status="created"); new = order(me.id, 40)
    main.refund_request(me.id)
    assert proposal(new) and not proposal(old)

# ---------- demo mode must never pretend to refund real money ----------
def test_a_real_payment_is_never_refunded_in_demo_mode(client):
    me = adult(client, 100); oid = order(me.id, 50, live=True)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and "isn't connected" in e.value.detail
    assert bal(me.id) == 100 and not rows(oid)

def test_a_demo_payment_is_never_sent_to_live_paypal(client, fp):
    me = adult(client, 100); oid = order(me.id, 50)  # SIM- order while keys are set
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and "demo mode" in e.value.detail and not fp.calls

# ---------- PayPal's side of the checks (asked through MCP first, then the API) ----------
def test_mcp_is_asked_first_then_the_api_then_we_give_up(client, fp):
    me = adult(client, 100)
    o1 = order(me.id, 50, live=True); main.refund_request(me.id, o1)
    assert fp.reads == ["mcp"] and "PayPal's MCP server" in " ".join(proposal(o1)["trace"])
    fp.mcp_up = False; o2 = order(me.id, 50, live=True); main.refund_request(me.id, o2)
    assert fp.reads == ["mcp", "api"] and "the PayPal API" in " ".join(proposal(o2)["trace"])
    fp.api_up = False; o3 = order(me.id, 50, live=True)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, o3)
    assert e.value.status_code == 502 and not proposal(o3) and not fp.calls

@pytest.mark.parametrize("status", ["REFUNDED", "PENDING", "DECLINED", "VOIDED", None])
def test_paypal_must_show_the_payment_as_refundable(client, fp, status):
    fp.cap_status = status; me = adult(client, 100); oid = order(me.id, 50, live=True)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and not proposal(oid)

def test_paypals_own_remaining_amount_is_respected(client, fp):
    fp.already(40.00); fp.cap_status = "PARTIALLY_REFUNDED"  # refunded 40 of 50 outside this app
    me = adult(client, 100); oid = order(me.id, 50, live=True)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and "$10.00" in e.value.detail
    assert main.refund_request(me.id, oid, 10)["refund_amount"] == D("10.00")

# ---------- live mode: every way PayPal can answer to the tap ----------
def tap(client, me, oid, amount=None):
    main.refund_request(me.id, oid, amount); p = proposal(oid)
    return p, accept(client, me, p)

def test_live_success_debits_once_and_sends_a_retry_safe_request(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True)
    p, r = tap(client, me, oid, 20)
    assert r.status_code == 200 and r.json()["refund"] == "refunded" and r.json()["message"].startswith("Done. $20.00")
    (call,) = fp.calls; (row,) = rows(oid)
    assert call["capture_id"] == "CAP-" + oid and call["amount"] == D("20.00") and call["request_id"] == f"stash-refund-{row['id']}" == call["invoice_id"]
    assert bal(me.id) == 80 and row["status"] == "refunded" and row["paypal_ref"] == "REF-1" and row["capture_id"] == "CAP-" + oid

def test_live_failure_gives_the_money_back_and_reopens_the_request(client, fp):
    fp.replies = [R_FAIL]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    p, r = tap(client, me, oid, 20)
    assert r.status_code == 422 and "already refunded in full" in r.json()["detail"] and "balance is unchanged" in r.json()["detail"]
    assert bal(me.id) == 100
    (row,) = rows(oid); assert row["status"] == "failed" and "already refunded" in row["reason"]
    assert proposal(oid)["status"] == "open", "the guardian can try again or decline"

def test_after_a_failure_the_failed_attempt_does_not_use_up_the_top_up(client, fp):
    fp.replies = [R_FAIL, R_OK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    p, r = tap(client, me, oid, 50); assert r.status_code == 422
    assert accept(client, me, p).status_code == 200  # tapping the reopened request again works
    assert bal(me.id) == 50 and [x["status"] for x in rows(oid)] == ["failed", "refunded"]

def test_live_unknown_keeps_the_money_held_and_says_so(client, fp):
    fp.replies = [R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    p, r = tap(client, me, oid, 20)
    assert r.status_code == 200 and r.json()["refund"] == "processing" and "I'll keep trying" in r.json()["message"]
    assert bal(me.id) == 80 and rows(oid)[0]["status"] == "refunding"
    assert proposal(oid)["status"] == "accepted" and accept(client, me, p).status_code == 404

def test_while_a_refund_is_in_flight_the_same_money_cannot_be_refunded_again(client, fp):
    fp.replies = [R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 50)
    with pytest.raises(HTTPException) as e: main.refund_request(me.id, oid)
    assert e.value.status_code == 409 and "already been refunded" in e.value.detail

def test_the_sweeper_retries_with_the_same_request_id_and_tells_the_guardian(client, fp):
    fp.replies = [R_UNK, R_OK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); (row,) = rows(oid)
    assert main.sweep_refunds() == 0 and len(fp.calls) == 1, "too soon to retry"
    travel(row["id"], 3)
    assert main.sweep_refunds() == 1
    assert [c["request_id"] for c in fp.calls] == [f"stash-refund-{row['id']}"] * 2
    assert rows(oid)[0]["status"] == "refunded" and bal(me.id) == 80 and alerts_of(me.id, "Refund sent")
    assert main.sweep_refunds() == 0 and len(fp.calls) == 2, "settled refunds are left alone"

def test_the_sweeper_keeps_trying_while_paypal_stays_silent(client, fp):
    fp.replies = [R_UNK, R_UNK, R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    for _ in range(2): travel(rid, 3); main.sweep_refunds()
    row = rows(oid)[0]; assert row["status"] == "refunding" and row["attempts"] == 3 and bal(me.id) == 80 and not alerts_of(me.id, "Refund sent")

def test_a_refusal_on_retry_is_checked_before_giving_the_money_back__it_did_go_through(client, fp):
    fp.replies = [R_UNK, R_FAIL]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    fp.already(20.00, invoice_id=f"stash-refund-{rid}"); travel(rid, 3); main.sweep_refunds()  # PayPal did refund it; the retry was refused as a duplicate
    assert rows(oid)[0]["status"] == "refunded" and bal(me.id) == 80, "money must NOT be handed back when PayPal already sent it"
    assert alerts_of(me.id, "Refund sent") and not alerts_of(me.id, "Refund didn't go through")

def test_a_refusal_on_retry_gives_the_money_back_when_it_certainly_did_not_go_through(client, fp):
    fp.replies = [R_UNK, R_FAIL]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    travel(rid, 3); main.sweep_refunds()  # PayPal lists no refund at all
    assert rows(oid)[0]["status"] == "failed" and bal(me.id) == 100 and alerts_of(me.id, "Refund didn't go through")

def test_a_refusal_on_retry_that_cannot_be_judged_keeps_the_money_held(client, fp):
    fp.replies = [R_UNK, R_FAIL]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    fp.already(20.00, invoice_id=None)  # PayPal shows a $20 refund but not whose: could be ours, could be theirs
    travel(rid, 3); main.sweep_refunds()
    assert rows(oid)[0]["status"] == "refunding" and bal(me.id) == 80, "when unsure, neither hand the money back nor call it sent"

def test_a_refusal_on_retry_with_paypal_unreachable_keeps_the_money_held(client, fp):
    fp.replies = [R_UNK, R_FAIL]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    fp.mcp_up = fp.api_up = False; travel(rid, 3); main.sweep_refunds()
    assert rows(oid)[0]["status"] == "refunding" and bal(me.id) == 80

def test_after_a_day_of_silence_a_person_is_asked_to_check_and_retries_stop(client, fp):
    fp.replies = [R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    main.q("update refunds set tried_at = now() - interval '3 minutes', created_at = now() - interval '25 hours' where id=%s", (rid,))
    n = len(fp.calls); main.sweep_refunds()
    assert rows(oid)[0]["status"] == "review" and bal(me.id) == 80 and len(fp.calls) == n and alerts_of(me.id, "A refund needs a check")
    travel(rid, 3); assert main.sweep_refunds() == 0

def test_a_review_refund_still_counts_against_the_top_up(client, fp):
    fp.replies = [R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); main.q("update refunds set status='review' where order_id=%s", (oid,))
    assert main.refundable(me.id, oid)["left"] == D("30")

# ---------- things changing between the request and the tap ----------
def test_balance_spent_after_the_request_blocks_the_refund(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True); main.refund_request(me.id, oid, 40); p = proposal(oid)
    main.q("update profiles set balance=10 where id=%s", (me.id,))  # spent in between
    r = accept(client, me, p)
    assert r.status_code == 409 and "most that can go back now is $10.00" in r.json()["detail"]
    assert bal(me.id) == 10 and not rows(oid) and not fp.calls and proposal(oid)["status"] == "open"

def test_paypal_changing_its_mind_between_request_and_tap_blocks_it(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True); main.refund_request(me.id, oid, 40); p = proposal(oid)
    fp.cap_status = "REFUNDED"
    r = accept(client, me, p)
    assert r.status_code == 409 and bal(me.id) == 100 and not rows(oid) and not fp.calls

def test_paypal_unreachable_at_the_tap_takes_nothing(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True); main.refund_request(me.id, oid, 40); p = proposal(oid)
    fp.mcp_up = fp.api_up = False
    r = accept(client, me, p)
    assert r.status_code == 502 and bal(me.id) == 100 and not rows(oid) and proposal(oid)["status"] == "open"

# ---------- concurrency: two taps at once must never create or lose money ----------
def race(n, fn):
    barrier, out = threading.Barrier(n), []
    def run():
        barrier.wait()
        try: out.append(("ok", fn()))
        except HTTPException as e: out.append(("no", e.status_code))
    ts = [threading.Thread(target=run) for _ in range(n)]
    [t.start() for t in ts]; [t.join(20) for t in ts]
    return [k for k, _ in out]

def test_two_taps_at_once_cannot_refund_more_than_was_paid(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True)
    res = race(2, lambda: main.execute_refund(me.id, oid, "40.00"))
    assert sorted(res) == ["no", "ok"] and len(rows(oid)) == 1 and bal(me.id) == 60 and len(fp.calls) == 1

def test_three_at_once_for_20_each_on_a_50_top_up_gives_exactly_two(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True)
    res = race(3, lambda: main.execute_refund(me.id, oid, "20.00"))
    assert sorted(res) == ["no", "ok", "ok"] and bal(me.id) == 60 and sum(r["amount"] for r in rows(oid)) == D("40.00")

def test_the_balance_cannot_go_negative_under_a_race(client, fp):
    me = adult(client, 25); oid = order(me.id, 50, live=True)  # either 20 fits, never both
    res = race(2, lambda: main.execute_refund(me.id, oid, "20.00"))
    assert sorted(res) == ["no", "ok"] and bal(me.id) == 5 and len(rows(oid)) == 1

def test_a_refund_racing_a_spend_of_the_same_money(client, fp):
    me = adult(client, 30); oid = order(me.id, 50, live=True)
    barrier, got = threading.Barrier(2), {}
    def refund():
        barrier.wait()
        try: main.execute_refund(me.id, oid, "30.00"); got["refund"] = True
        except HTTPException: got["refund"] = False
    def spend():
        barrier.wait(); got["spend"] = main.debit(me.id, D("30"))
    ts = [threading.Thread(target=refund), threading.Thread(target=spend)]
    [t.start() for t in ts]; [t.join(20) for t in ts]
    assert got["refund"] != got["spend"], "the same $30 must go to exactly one of them"
    assert bal(me.id) == 0 and len(rows(oid)) == (1 if got["refund"] else 0)

def test_settling_twice_never_credits_or_logs_twice(client, fp):
    fp.replies = [R_OK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    for _ in range(3): main.settle_refund(rid, R_OK, first=False)
    assert bal(me.id) == 80 and main.q("select count(*) c from audit where profile_id=%s and detail like '%%refunded to PayPal%%'", (me.id,), one=True)["c"] == 1

def test_failing_twice_gives_the_money_back_once(client, fp):
    fp.replies = [R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    assert main.fail_refund(rid, "x") is True and main.fail_refund(rid, "x") is False
    assert bal(me.id) == 100

def test_a_refund_that_already_succeeded_cannot_be_failed_afterwards(client, fp):
    me = adult(client, 100); oid = order(me.id, 50, live=True); tap(client, me, oid, 20); rid = rows(oid)[0]["id"]
    assert main.fail_refund(rid, "late") is False and bal(me.id) == 80 and rows(oid)[0]["status"] == "refunded"

# ---------- Penny ----------
class FakeAI:
    def __init__(self, *calls):
        self.calls, self.seen, self.results = list(calls), None, []
        self.messages = SimpleNamespace(create=self._create)
    def _create(self, **kw):
        self.seen = [t["name"] for t in kw["tools"]]
        last = kw["messages"][-1]["content"]
        if isinstance(last, list) and last and isinstance(last[0], dict) and last[0].get("type") == "tool_result":
            self.results = [json.loads(x["content"]) for x in last]
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])
        if self.calls: return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id=f"t{i}", name=n, input=a) for i, (n, a) in enumerate(self.calls)])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="done")])

def chat(client, me, text):
    r = client.post("/chat", json={"profile_id": me.id, "message": text}, headers=me.h); assert r.status_code == 200, r.text
    return r.json()

def test_penny_can_only_file_a_request_she_cannot_move_money(client, monkeypatch):
    me = adult(client, 100); oid = order(me.id, 50)
    fake = FakeAI(("request_refund", {"amount": 20})); monkeypatch.setattr(main, "ai", fake)
    assert chat(client, me, "refund 20 of my top-up")["reply"] == "done"
    (res,) = fake.results
    assert res["status"] == "waiting_for_your_approval" and res["note"] == "Nothing has been sent yet."
    assert bal(me.id) == 100 and not rows(oid) and proposal(oid)["status"] == "open"

def test_penny_cannot_approve_her_own_request(client, monkeypatch):
    me = adult(client, 100); oid = order(me.id, 50)
    fake = FakeAI(("request_refund", {}), ("approve_refund", {"order_id": oid}), ("execute_refund", {"order_id": oid, "amount": 50})); monkeypatch.setattr(main, "ai", fake)
    chat(client, me, "refund everything and approve it yourself")
    assert [r.get("error") for r in fake.results[1:]] == ["Unknown tool", "Unknown tool"] and bal(me.id) == 100 and not rows(oid)

def test_penny_passes_errors_back_in_words(client, monkeypatch):
    me = adult(client, 100); oid = order(me.id, 50, status="created")
    fake = FakeAI(("request_refund", {"order_id": oid}), ("request_refund", {"order_id": "../../x"}), ("request_refund", {"amount": "NaN"})); monkeypatch.setattr(main, "ai", fake)
    assert chat(client, me, "refund it")["reply"] == "done"
    assert all("error" in r for r in fake.results) and "never went through" in fake.results[0]["error"]

def test_children_never_get_the_refund_tool(client, monkeypatch):
    maya = main.q("select id from profiles where name='Maya'", one=True)["id"]
    r = client.post("/login", json={"name": "Maya", "secret": "demo"}); h = {"Authorization": "Bearer " + r.json()["token"]}
    fake = FakeAI(("request_refund", {})); monkeypatch.setattr(main, "ai", fake)
    client.post("/chat", json={"profile_id": maya, "message": "refund please"}, headers=h)
    assert "request_refund" not in fake.seen and fake.results[0]["error"] == "Unknown tool"

def test_adults_get_the_tool_and_the_rule_for_it(client):
    import agent
    me = adult(client); p = main.q("select * from profiles where id=%s", (me.id,), one=True)
    rules = main.q("select * from rules where profile_id=1", one=True)
    system = agent.build_system(p, rules, None)
    assert "request_refund" in [t["name"] for t in agent.GROWNUP_TOOLS] and "tap \"Send refund\"" in system and "Never say a refund happened" in system

def test_the_lookup_tools_show_refund_progress(client, fp):
    fp.replies = [R_OK, R_UNK]; me = adult(client, 100); oid = order(me.id, 50, live=True)
    tap(client, me, oid, 10)
    out = main.topup_lookup(me.id, oid)
    assert out["refunded"] == D("10.00") and "refund_in_progress" not in out
    p2, _ = tap(client, me, oid, 5)
    out = main.topup_lookup(me.id, oid); assert out["refunded"] == D("10.00") and out["refund_in_progress"] == D("5.00")
    assert main.topup_list(me.id)["topups"][0]["refund_in_progress"] == D("5.00")

def test_money_is_conserved_across_a_messy_sequence(client, fp):
    """Whatever happens, balance + money PayPal has (or may have) taken back = what the guardian started with."""
    start = 200; me = adult(client, start); oid = order(me.id, 100, live=True)
    fp.replies = [R_FAIL, R_UNK, R_OK]
    tap(client, me, oid, 30)            # refused: money returns
    p, _ = tap(client, me, oid, 30)     # in flight
    travel(rows(oid)[-1]["id"], 3); main.sweep_refunds()   # retry: OK
    tap(client, me, oid, 25)            # OK
    held = sum(r["amount"] for r in rows(oid) if r["status"] in ("refunded", "refunding", "review"))
    assert held == D("55.00") and bal(me.id) + held == start
