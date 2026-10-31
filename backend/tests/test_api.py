"""End-to-end API test on a real throwaway Postgres (needs: pip install pgserver)."""
import os, sys, pathlib, pytest
pgserver = pytest.importorskip("pgserver")
ROOT = pathlib.Path(__file__).resolve().parents[1]

@pytest.fixture(scope="module")
def api(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    srv.psql((ROOT / "schema.sql").read_text())
    os.environ.update(DATABASE_URL=srv.get_uri(), SECRET_KEY="k" * 32, ANTHROPIC_API_KEY="test", CRON_KEY="ck"); os.environ.pop("CHANNEL3_API_KEY", None)
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        yield c

def login(api, name):
    r = api.post("/login", json={"name": name, "secret": "demo"}); assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}, r.json()["id"]

def test_full_flow(api):
    assert api.post("/login", json={"name": "Sam", "secret": "nope"}).status_code == 401
    assert api.get("/profiles/2").status_code == 401
    sam, sid = login(api, "Sam"); maya, mid = login(api, "Maya")
    assert api.get(f"/profiles/{sid}", headers=maya).status_code == 403
    p = api.get(f"/profiles/{mid}", headers=maya).json()["profile"]
    assert "secret" not in p and float(p["balance"]) == 45 and p["goal"]["name"] == "Soccer ball" and p["goal"]["saved"] == 15 and p["goal"]["bonus_pct"] == 0
    gid = p["goal"]["id"]
    gp = lambda: api.get(f"/profiles/{mid}", headers=maya).json()["profile"]["goal"]
    assert api.post(f"/interests/{mid}", headers=maya, json={"interests": ["Art"]}).status_code == 200
    assert [x["name"] for x in api.get("/products", headers=maya).json() if x["picked"]] == ["Art Kit"]
    buy = lambda pid: api.post("/purchase", headers=maya, json={"profile_id": mid, "product_id": pid}).json()["status"]
    assert buy(5) == "completed" and buy(3) == "blocked" and buy(1) == "pending"      # in budget / over the cap / needs approval
    assert api.post(f"/rules/{mid}", headers=maya, json={"per_purchase_cap": 999, "weekly_budget": 999, "approval_threshold": 999, "allowed_stores": ["x"]}).status_code == 403
    req = api.get(f"/approvals/{sid}", headers=sam).json(); assert len(req) == 1
    assert api.post(f"/approvals/{req[0]['id']}/approve", headers=maya).status_code == 404
    assert api.post(f"/approvals/{req[0]['id']}/approve", headers=sam).status_code == 200
    assert api.post(f"/approvals/{req[0]['id']}/approve", headers=sam).status_code == 404
    bal = lambda: round(float(api.get(f"/profiles/{mid}", headers=maya).json()["profile"]["balance"]), 2)
    assert bal() == 15.02
    assert api.post(f"/freeze/{mid}", headers=sam).status_code == 200 and buy(2) == "blocked"
    api.post(f"/freeze/{mid}", headers=sam)
    # the grown-up's balance funds everything: top-ups, allowance and bonuses
    sbal = lambda: round(float(api.get(f"/profiles/{sid}", headers=sam).json()["profile"]["balance"]), 2)
    assert sbal() == 120
    assert api.post("/topup", headers=sam, json={"guardian_id": sid, "child_id": mid, "amount": 50}).status_code == 200 and bal() == 65.02 and sbal() == 70
    assert api.post("/topup", headers=sam, json={"guardian_id": sid, "child_id": mid, "amount": 500}).status_code == 409    # not enough in the balance
    # savings: money moves wallet <-> savings, and only the kid decides
    dep = lambda who, h, a: api.post("/savings", headers=h, json={"profile_id": who, "amount": a})
    out = lambda who, h, a: api.post("/savings/withdraw", headers=h, json={"profile_id": who, "amount": a})
    assert dep(mid, sam, 5).status_code == 403 and out(mid, sam, 5).status_code == 403
    assert out(mid, maya, 5).status_code == 200 and bal() == 70.02          # savings -> wallet
    assert dep(mid, maya, 5).status_code == 200 and bal() == 65.02          # wallet -> savings
    assert out(mid, maya, 100).status_code == 409 and dep(mid, maya, 50).status_code == 422
    # the bonus is optional (a percent), set by the grown-up with a slider
    assert gp()["bonus_pct"] == 0 and gp()["bonus"] == 0
    assert api.post(f"/goals/{gid}/bonus", headers=maya, json={"percent": 100}).status_code == 404
    assert api.post(f"/goals/{gid}/bonus", headers=sam, json={"percent": 150}).status_code == 422
    assert api.post(f"/goals/{gid}/bonus", headers=sam, json={"percent": 50}).status_code == 200 and gp()["bonus"] == 30
    r = dep(mid, maya, 45).json()
    assert r["reached"] and r["paid"] and bal() == 65.02 - 45 + 60 + 30 and sbal() == 40    # the bonus came out of the grown-up's balance
    assert gp() is None
    assert api.post(f"/goals/{gid}/bonus", headers=sam, json={"percent": 10}).status_code == 404     # too late once paid
    assert any(a["title"] == "Goal reached!" for a in api.get(f"/alerts/{mid}", headers=maya).json())
    assert any("reached a goal" in a["title"] for a in api.get(f"/alerts/{sid}", headers=sam).json())
    # a grown-up can start a goal with a bonus; only the kid can cancel it
    assert api.post("/goals", headers=sam, json={"child_id": mid, "name": "Bike", "target": 100, "percent": 20}).status_code == 200
    assert gp()["bonus_pct"] == 20 and api.post("/goals", headers=maya, json={"child_id": mid, "name": "Book", "target": 30}).status_code == 409
    g2 = gp()["id"]; w0 = bal()
    assert dep(mid, maya, 5).status_code == 200 and bal() == round(w0 - 5, 2)
    assert api.post(f"/goals/{g2}/cancel", headers=sam).status_code == 404
    assert api.post(f"/goals/{g2}/cancel", headers=maya).status_code == 200 and bal() == w0
    # a kid-created goal gets no bonus until the grown-up chooses; if their balance is short the bonus waits
    assert api.post("/goals", headers=maya, json={"child_id": mid, "name": "Book", "target": 30, "percent": 90}).status_code == 200 and gp()["bonus_pct"] == 0
    row = [a for a in api.get(f"/alerts/{sid}", headers=sam).json() if a["goal_id"] and a["goal_status"] == "active"][0]
    assert "Maya is saving for Book" in row["title"] and row["goal_pct"] == 0
    g3 = gp()["id"]; assert api.post(f"/goals/{g3}/bonus", headers=sam, json={"percent": 100}).status_code == 200
    import main
    main.q("update profiles set balance=5 where id=%s", (sid,))
    w1 = bal(); r = dep(mid, maya, 30).json()
    assert r["reached"] and not r["paid"] and bal() == round(w1 - 30, 2) and gp() is not None      # waits for the grown-up's funds
    assert any(a["title"] == "Add money to pay the bonus" for a in api.get(f"/alerts/{sid}", headers=sam).json())
    assert api.post("/funds/create", headers=maya, json={"amount": 10}).status_code == 403
    order = api.post("/funds/create", headers=sam, json={"amount": 100}).json(); assert "token=" + order["order_id"] in order["approve_url"]
    assert api.post("/funds/capture", headers=maya, json={"order_id": order["order_id"]}).status_code == 404       # someone else's payment
    assert api.post("/funds/capture", headers=sam, json={"order_id": order["order_id"]}).status_code == 200        # adding money pays the waiting bonus automatically
    assert api.post("/funds/capture", headers=sam, json={"order_id": order["order_id"]}).status_code == 200        # a second call changes nothing
    assert gp() is None and bal() == round(w1 - 30 + 30 + 30, 2) and sbal() == 75
    # weekly allowance is taken from the balance automatically
    assert api.post(f"/allowance/{mid}", headers=maya, json={"amount": 10}).status_code == 403
    assert api.post(f"/allowance/{mid}", headers=sam, json={"amount": 10}).status_code == 200
    before, sb = bal(), sbal(); assert main.pay_allowances() == 0 and bal() == before
    main.q("update profiles set allowance_next=now()-interval '1 minute' where id=%s", (mid,))
    assert main.pay_allowances() == 1 and bal() == round(before + 10, 2) and sbal() == sb - 10
    assert main.pay_allowances() == 0
    assert main.q("select allowance_next > now() + interval '6 days' ok from profiles where id=%s", (mid,), one=True)["ok"]
    assert any(a["title"] == "Allowance arrived" for a in api.get(f"/alerts/{mid}", headers=maya).json())
    assert api.post(f"/allowance/{mid}", headers=sam, json={"amount": 0}).status_code == 200
    # accounts and misc
    assert api.post("/snap", headers=maya, json={"profile_id": mid, "image_b64": "x"}).status_code == 503
    assert api.post("/register", json={"name": "Kid2", "secret": "1234", "guardian_id": sid}).status_code == 401
    assert api.post("/register", headers=sam, json={"name": "Kid2", "secret": "1234", "guardian_id": sid}).status_code == 200
    assert api.post("/register", headers=sam, json={"name": "kid2", "secret": "1234", "guardian_id": sid}).status_code == 409
    z = api.post("/register", headers=sam, json={"name": "Zed", "secret": "1234", "guardian_id": sid, "per_purchase_cap": 7, "weekly_budget": 20, "approval_threshold": 5, "allowed_stores": ["Toy Barn"]}).json()["id"]
    assert float(api.get(f"/profiles/{z}", headers=sam).json()["rules"]["per_purchase_cap"]) == 7
    assert api.post("/chat", headers=maya, json={"profile_id": mid, "message": ""}).status_code == 422
    assert api.post("/internal/settle-all").status_code == 403


# ---- orders, fulfilment and the agent (with a scripted stand-in for the model) ----
from types import SimpleNamespace as NS

class StubAI:
    def __init__(self, script): self.script, self.calls, self.messages = list(script), [], self
    def create(self, **kw): self.calls.append(kw); return self.script.pop(0)

def reply(*blocks, stop="end_turn"): return NS(content=list(blocks), stop_reason=stop)
text = lambda t: NS(type="text", text=t)
use = lambda i, name, **inp: NS(type="tool_use", id=f"t{i}", name=name, input=inp)

def agent_run(api, who, message, script, history=()):
    import main
    stub = StubAI(script); old = main.ai; main.ai = stub
    try: return main.run_agent(who, message, list(history)), stub
    finally: main.ai = old

def tool_result(stub, n):  # the tool_result the code sent back to the model after call n
    return stub.calls[n]["messages"][-1]["content"][0]

def test_agent_buys_only_what_was_offered_and_confirmed(api):
    sam, sid = login(api, "Sam"); maya, mid = login(api, "Maya")
    wallet = lambda: float(api.get(f"/profiles/{mid}", headers=maya).json()["profile"]["balance"])
    w0 = wallet()
    (t, picks, order, _), s = agent_run(api, mid, "buy it", [reply(use(1, "buy", product_id=6), stop="tool_use"), reply(text("I need to search first."))])
    assert tool_result(s, 1)["is_error"] and "Search for the item first" in tool_result(s, 1)["content"] and order is None and wallet() == w0
    (t, picks, order, _), s = agent_run(api, mid, "show me the art kit", [reply(use(1, "search_products", query="art kit"), stop="tool_use"), reply(use(2, "buy", product_id=5), stop="tool_use"), reply(text("Want me to buy it?"))])
    assert tool_result(s, 2)["is_error"] and "hasn't said yes" in tool_result(s, 2)["content"] and order is None and wallet() == w0     # no yes, no purchase
    (t, picks, order, _), s = agent_run(api, mid, "yes, buy the art kit", [reply(use(1, "search_products", query="art kit"), stop="tool_use"), reply(use(2, "buy", product_id=5), stop="tool_use"), reply(text("Done!"))])
    assert t == "Done!" and order["item"] == "Art Kit" and order["status"] == "completed" and order["fulfillment"] == "processing" and round(w0 - wallet(), 2) == 9.99
    assert s.calls[0]["temperature"] == 0.2 and "<kid_rules>" in s.calls[0]["system"] and any(x["name"] == "set_goal" for x in s.calls[0]["tools"])

def test_agent_safety_guards(api):
    sam, sid = login(api, "Sam"); maya, mid = login(api, "Maya")
    (t, _, _, _), s = agent_run(api, mid, "find vape pens", [reply(use(1, "search_stores", query="vape pens"), stop="tool_use"), reply(text("I can't help with that."))])
    assert tool_result(s, 1)["is_error"] and "kids can shop" in tool_result(s, 1)["content"]
    (t, _, _, _), s = agent_run(api, sid, "save money", [reply(use(1, "save_money", amount=5), stop="tool_use"), reply(text("Only kids have savings."))])
    assert "Unknown tool" in tool_result(s, 1)["content"] and not any(x["name"] == "set_goal" for x in s.calls[0]["tools"]) and "<grownup_rules>" in s.calls[0]["system"]
    (t, _, _, _), s = agent_run(api, mid, "hi", [reply(use(1, "buy", product_id="abc"), stop="tool_use"), reply()])
    assert "Invalid tool input" in tool_result(s, 1)["content"] and t == "Anything else I can help with?"      # bad input and an empty reply are both handled

def test_order_timeline_and_notifications(api):
    import main
    sam, sid = login(api, "Sam"); maya, mid = login(api, "Maya")
    first = api.get(f"/orders/{mid}", headers=maya).json()[0]; assert first["item"] == "Art Kit" and first["fulfillment"] == "processing"
    main.q("update purchases set created_at=now()-interval '3 minutes' where id=%s", (first["id"],))
    assert main.sweep_orders() >= 1
    main.q("update purchases set shipped_at=now()-interval '4 minutes' where id=%s", (first["id"],))
    assert main.sweep_orders() >= 1
    done = [o for o in api.get(f"/orders/{mid}", headers=maya).json() if o["id"] == first["id"]][0]
    assert done["fulfillment"] == "delivered" and done["delivered_at"]
    titles = [a["title"] for a in api.get(f"/alerts/{mid}", headers=maya).json()]; assert "On its way" in titles and "Delivered" in titles
    assert api.get("/health").json()["ok"]


# ---- autonomy: missions, order problems, price watching, nudges, visible reasoning ----
def fund(api, h, amount):
    o = api.post("/funds/create", headers=h, json={"amount": amount}).json()
    assert api.post("/funds/capture", headers=h, json={"order_id": o["order_id"]}).status_code == 200

def test_autonomy(api):
    import main
    from fastapi.testclient import TestClient
    sam, sid = login(api, "Sam"); fund(api, sam, 500)
    pip = api.post("/register", headers=sam, json={"name": "Pip", "secret": "1234", "guardian_id": sid, "per_purchase_cap": 30, "weekly_budget": 100, "approval_threshold": 15, "allowed_stores": ["Toy Barn", "Pixel Games"]}).json()["id"]
    pt = api.post("/login", json={"name": "Pip", "secret": "1234"}).json(); ph = {"Authorization": "Bearer " + pt["token"]}
    assert api.post("/topup", headers=sam, json={"guardian_id": sid, "child_id": pip, "amount": 150}).status_code == 200
    wallet = lambda: round(float(api.get(f"/profiles/{pip}", headers=ph).json()["profile"]["balance"]), 2)
    props = lambda h: api.get("/proposals", headers=h).json()
    cron = {"X-Cron-Key": "ck"}

    # 1. a mission becomes ONE basket proposal for the grown-up, built in the background
    (t, _, _, missions), s = agent_run(api, pip, "get an art kit and a soccer ball under $30", [reply(use(1, "start_mission", title="Art and ball", budget=30, items=[{"query": "art kit"}, {"query": "soccer ball"}], deadline="Friday"), stop="tool_use"), reply(text("On it."))])
    assert len(missions) == 1 and "Working on it" in tool_result(s, 1)["content"]
    w0 = wallet(); main.plan_mission(missions[0])
    mp = [p for p in props(sam) if p["kind"] == "mission"][0]
    assert float(mp["data"]["total"]) == 24.49 and len(mp["data"]["basket"]) == 2 and any("cheapest" in x for x in mp["trace"]) and wallet() == w0     # nothing is spent before approval
    assert api.post(f"/proposals/{mp['id']}/accept", headers=ph).status_code == 404                          # the child can't approve their own basket
    r = api.post(f"/proposals/{mp['id']}/accept", headers=sam).json(); assert [x["status"] for x in r["results"]] == ["completed", "completed"] and wallet() == round(w0 - 24.49, 2)
    assert api.post(f"/proposals/{mp['id']}/accept", headers=sam).status_code == 404                         # and never twice
    o = api.get(f"/orders/{pip}", headers=ph).json()[0]; assert any("Checked the rules" in x for x in o["trace"]) and any("approved the basket" in x for x in o["trace"])
    (t, _, _, m2), s = agent_run(api, pip, "art kit and robot dino under $20", [reply(use(1, "start_mission", title="Tight", budget=20, items=[{"query": "art kit"}, {"query": "robot dino"}]), stop="tool_use"), reply(text("ok"))])
    main.plan_mission(m2[0]); tp = [p for p in props(sam) if p["kind"] == "mission"][0]
    assert [b["name"] for b in tp["data"]["basket"]] == ["Art Kit"] and any("dropped Robot Dino" in x for x in tp["trace"])      # trims to the budget and says so
    api.post(f"/proposals/{tp['id']}/decline", headers=sam)

    # 2. order problems: refund, then a replacement that still goes through the rules
    blk = api.post("/purchase", headers=ph, json={"profile_id": pip, "product_id": 2}).json()["order"]["id"]; w1 = wallet()
    assert api.post("/internal/partner-event", json={"purchase_id": blk, "event": "cancelled"}).status_code == 403
    assert api.post("/internal/partner-event", headers=cron, json={"purchase_id": blk, "event": "cancelled"}).status_code == 200
    assert wallet() == round(w1 + 12.99, 2)
    rp = [p for p in props(ph) if p["kind"] == "replace"][0]; assert "Space Racer" in rp["title"] and any("cancelled by" in x for x in rp["trace"])
    assert api.post(f"/proposals/{rp['id']}/accept", headers=ph).json()["result"] == "pending"                 # $19.99 is over the approval line: a grown-up still decides
    late = api.post("/purchase", headers=ph, json={"profile_id": pip, "product_id": 2}).json()["order"]["id"]; w2 = wallet()
    api.post("/internal/partner-event", headers=cron, json={"purchase_id": late, "event": "delayed"})
    dp = [p for p in props(ph) if p["kind"] == "replace"][0]; assert dp["data"]["cancel_first"]
    api.post(f"/proposals/{dp['id']}/accept", headers=ph)
    assert main.q("select status from purchases where id=%s", (late,), one=True)["status"] == "cancelled" and wallet() == round(w2 + 12.99, 2)

    # 3. price watching tied to a goal, with an auto-buy limit only a grown-up can set
    main.q("update purchases set status='declined' where profile_id=%s and status='pending'", (pip,))
    goal = main.create_goal(pip, "Space Racer", 20, pip, product_id=1); gid = api.get(f"/profiles/{pip}", headers=ph).json()["profile"]["goal"]["id"]
    assert api.get(f"/profiles/{pip}", headers=ph).json()["profile"]["goal"]["watch_name"] == "Space Racer"
    assert api.post(f"/goals/{gid}/watch", headers=ph, json={"buy_below": 15}).status_code == 404
    assert api.post(f"/internal/price", headers=cron, json={"product_id": 1, "price": 17.99}).json()["watches_checked"] == 1
    pd = [p for p in props(ph) if p["kind"] == "price_drop"][0]; assert "$2.00 cheaper" in pd["title"] and "to $18.00" in pd["body"]
    api.post(f"/proposals/{pd['id']}/accept", headers=ph); assert float(api.get(f"/profiles/{pip}", headers=ph).json()["profile"]["goal"]["target"]) == 18
    api.post("/savings", headers=ph, json={"profile_id": pip, "amount": 16}); assert api.post(f"/goals/{gid}/watch", headers=sam, json={"buy_below": 15}).status_code == 200
    w3 = wallet(); api.post("/internal/price", headers=cron, json={"product_id": 1, "price": 14})
    assert api.get(f"/profiles/{pip}", headers=ph).json()["profile"]["goal"] is None and wallet() == round(w3 + 16 - 14, 2)      # goal reached early, then it bought the item
    ab = api.get(f"/orders/{pip}", headers=ph).json()[0]; assert ab["item"] == "Space Racer" and float(ab["amount"]) == 14 and any("allowed buying at" in x for x in ab["trace"])
    assert any(a["title"] == "Auto-buy for Pip" for a in api.get(f"/alerts/{sid}", headers=sam).json()) and any(a["title"] == "Price drop: I bought it" for a in api.get(f"/alerts/{pip}", headers=ph).json())

    # 4. nudges: after the allowance, and when a child keeps hitting a limit
    main.create_goal(pip, "Bike", 50, pip)
    assert api.post(f"/allowance/{pip}", headers=sam, json={"amount": 10, "first_now": True}).status_code == 200
    sp = [p for p in props(ph) if p["kind"] == "save"][0]; assert any("allowance" in x for x in sp["trace"]) and sp["data"]["amount"] in ("30", "30.00", "29", "28")
    before = wallet(); assert api.post(f"/proposals/{sp['id']}/accept", headers=ph).status_code == 200 and wallet() < before
    assert main.nudge_save(pip, ["again"]) is None                                                            # not twice within 6 days
    for _ in range(3): api.post("/purchase", headers=ph, json={"profile_id": pip, "product_id": 3})
    assert main.nudge_limits() == 1
    rl = [p for p in props(sam) if p["kind"] == "raise_limit"][0]; assert rl["data"]["to"] == "40" and "3 purchases" in rl["body"]
    assert api.post(f"/proposals/{rl['id']}/accept", headers=ph).status_code == 404                           # a child can't raise their own limit
    api.post(f"/proposals/{rl['id']}/accept", headers=sam); assert float(api.get(f"/profiles/{pip}", headers=sam).json()["rules"]["per_purchase_cap"]) == 40
    assert main.nudge_limits() == 0


# ---- the AI judges and writes; the rules bound it and take over when it fails ----
import brain
from psycopg.types.json import Jsonb

def make_family(api, adult, kid, topup=100, **rules):
    a = api.post("/register", json={"name": adult, "secret": "1234"}).json(); ah = {"Authorization": "Bearer " + a["token"]}
    k = api.post("/register", headers=ah, json={"name": kid, "secret": "1234", "guardian_id": a["id"], "allowed_stores": ["Toy Barn", "Pixel Games"], **rules}).json()["id"]
    kh = {"Authorization": "Bearer " + api.post("/login", json={"name": kid, "secret": "1234"}).json()["token"]}
    fund(api, ah, 500); assert api.post("/topup", headers=ah, json={"guardian_id": a["id"], "child_id": k, "amount": topup}).status_code == 200
    return a["id"], ah, k, kh

def mission(main, kid, title, budget, *terms):
    return main.q("insert into missions(profile_id,title,budget,items) values(%s,%s,%s,%s) returning id", (kid, title, budget, Jsonb([{"query": t, "qty": 1} for t in terms])), one=True)["id"]

def basket_of(api, ah, mid):
    return [p for p in api.get("/proposals", headers=ah).json() if p["kind"] == "mission" and p["data"]["mission_id"] == mid][0]

def test_ai_ranks_the_basket_but_rules_bound_it(api, monkeypatch):
    import main
    sid, sam, kid, kh = make_family(api, "Rhea", "Rory", per_purchase_cap=30, weekly_budget=100, approval_threshold=15)
    main.q("update products set price=19.99 where id=1")                          # earlier tests change prices: pin the one this relies on
    # the model prefers the pricier game: accepted, and the reason shows in "How I decided"
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"picks": [{"query": "games", "product_id": 1, "why": "Loves racing"}], "summary": "A fun pick."})
    p = basket_of(api, sam, (m := mission(main, kid, "Game", 50, "games")) and main.plan_mission(m) and m)
    assert [b["name"] for b in p["data"]["basket"]] == ["Space Racer"] and p["data"]["by"] == "ai" and any("Penny picked Space Racer" in t and "Loves racing" in t for t in p["trace"]) and "A fun pick." in p["body"]
    assert "_q" not in p["data"]["basket"][0]
    # an invented product id is thrown away: the cheapest allowed option is used and the trace says so
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"picks": [{"query": "games", "product_id": 999, "why": "x"}]})
    m = mission(main, kid, "Game2", 50, "games"); main.plan_mission(m); p = basket_of(api, sam, m)
    assert [b["name"] for b in p["data"]["basket"]] == ["Block Builder"] and p["data"]["by"] == "rules" and any("cheapest" in t for t in p["trace"])
    # the model may drop an item, but only when the budget really is short
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"picks": [{"query": "games", "product_id": 1, "why": "a"}, {"query": "art kit", "product_id": 5, "why": "b"}], "drop": [{"query": "art kit", "why": "Least important"}]})
    m = mission(main, kid, "Tight", 25, "games", "art kit"); main.plan_mission(m); p = basket_of(api, sam, m)
    assert [b["name"] for b in p["data"]["basket"]] == ["Space Racer"] and any("Penny left out Art Kit: Least important" in t for t in p["trace"])
    m = mission(main, kid, "Roomy", 60, "games", "art kit"); main.plan_mission(m); p = basket_of(api, sam, m)
    assert len(p["data"]["basket"]) == 2                                        # enough budget: the drop request is ignored
    # a model that picks too much still can't beat the budget
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"picks": [{"query": "games", "product_id": 1, "why": "a"}, {"query": "soccer ball", "product_id": 4, "why": "b"}]})
    m = mission(main, kid, "Over", 30, "games", "soccer ball"); main.plan_mission(m); p = basket_of(api, sam, m)
    assert float(p["data"]["total"]) <= 30 and any("I dropped" in t for t in p["trace"])
    # model down: same result as before the AI existed
    monkeypatch.setattr(brain, "decide", lambda *a, **k: None)
    m = mission(main, kid, "Plain", 50, "games"); main.plan_mission(m); assert basket_of(api, sam, m)["data"]["by"] == "rules"

def test_ai_explains_price_drops_with_only_real_numbers(api, monkeypatch):
    import main
    sid, sam, kid, kh = make_family(api, "Sasha", "Sol")
    cron = {"X-Cron-Key": "ck"}; main.q("update products set price=14.50 where id=4")
    main.create_goal(kid, "Ball", 20, kid, product_id=4)
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"title": "Soccer Ball dropped", "body": "It went from $14.50 to $12.50. You could lower your goal to $18.00 if you like."})
    api.post("/internal/price", headers=cron, json={"product_id": 4, "price": 12.50})
    pd = [p for p in api.get("/proposals", headers=kh).json() if p["kind"] == "price_drop"][0]
    assert pd["title"] == "Soccer Ball dropped" and "lower your goal to $18.00" in pd["body"] and pd["data"]["by"] == "ai" and pd["data"]["new_target"] == "18.00" and any("Penny (the AI) wrote" in t for t in pd["trace"])
    api.post(f"/proposals/{pd['id']}/decline", headers=kh)
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"title": "Cheaper!", "body": "Now only $1.00, a $99.00 saving."})      # a made-up number: rejected
    api.post("/internal/price", headers=cron, json={"product_id": 4, "price": 11.50})
    pd = [p for p in api.get("/proposals", headers=kh).json() if p["kind"] == "price_drop"][0]
    assert "$1.00 cheaper" in pd["title"] and pd["data"]["by"] == "rules" and pd["body"].startswith("Lower your goal")

def test_ai_nudges_are_clamped_and_fall_back(api, monkeypatch):
    import main
    sid, sam, kid, kh = make_family(api, "Tess", "Tom", topup=40)
    main.create_goal(kid, "Bike", 50, kid)
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"amount": 500, "title": "Save for your Bike?", "body": "Moving $20 puts Bike at $20.00. Up to you."})
    assert main.nudge_save(kid, ["test"])
    sp = [p for p in api.get("/proposals", headers=kh).json() if p["kind"] == "save"][0]
    assert sp["data"] == {"amount": "20", "by": "ai"} and sp["title"] == "Save for your Bike?"          # asked for $500: the app capped it at half the $40 wallet
    main.q("delete from proposals where child_id=%s", (kid,))
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"amount": 5, "title": "Casino time", "body": "Win big with betting!"})              # unsafe words for a kid: rejected
    assert main.nudge_save(kid, ["test"]); sp = [p for p in api.get("/proposals", headers=kh).json() if p["kind"] == "save"][0]
    assert sp["data"] == {"amount": "10", "by": "rules"}                                                      # quarter of the wallet, as before
    for i in range(3): main.q("insert into purchases(profile_id,product_id,amount,status,reason) values(%s,3,39.99,'blocked','The limit for one purchase is $25.00.')", (kid,))
    monkeypatch.setattr(brain, "decide", lambda *a, **k: {"title": "Raise Tom's limit?", "body": "Tom tried 3 big buys. Raising to $40 would allow them, or keep $25.00."})
    assert main.nudge_limits() == 1
    rl = [p for p in api.get("/proposals", headers=sam).json() if p["kind"] == "raise_limit"][0]
    assert rl["title"] == "Raise Tom's limit?" and rl["data"] == {"to": "40", "by": "ai"}

def test_brain_guards():
    assert brain.numbers_ok("It is $14.50 now, was $15", [14.5, 15]) and not brain.numbers_ok("Only $3.00", [14.5]) and brain.numbers_ok("No money here", [])
    assert brain.clean("  hi   there ", [], 20) == "hi there" and brain.clean("x" * 30, [], 20) is None and brain.clean("beer money", [], 50, kid=True) is None and brain.clean("beer money", [], 50) == "beer money"
    assert not brain.enabled()                                                                                 # PENNY_AI=off in tests

# ---- notifications: real pushes, one inbox, and a day of life once something is dealt with ----
def spy_push(monkeypatch):
    sent = []
    monkeypatch.setattr(main_mod(), "push", type("P", (), {"configured": staticmethod(lambda: True), "public_key": staticmethod(lambda: "K"),
                        "enqueue": staticmethod(lambda subs, payload, forget: subs and sent.append(({s["endpoint"] for s in subs}, payload)))}))
    return sent

def main_mod():
    import main; return main

EP = lambda n: f"https://fcm.googleapis.com/fcm/send/{n}"
def subscribe(api, h, n): return api.post("/push/subscribe", headers=h, json={"endpoint": EP(n), "p256dh": "p", "auth": "a"})

def test_push_subscription_rules(api, monkeypatch):
    sent = spy_push(monkeypatch)
    sid, sam, kid, kh = make_family(api, "Uma", "Uri")
    assert api.get("/push/key").json() == {"enabled": True, "key": "K"} and api.post("/push/subscribe", json={"endpoint": EP(1), "p256dh": "p", "auth": "a"}).status_code == 401
    for bad in ("http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://fcm.googleapis.com.evil.com/x"):
        assert api.post("/push/subscribe", headers=sam, json={"endpoint": bad, "p256dh": "p", "auth": "a"}).status_code == 422      # never post to an arbitrary address
    assert subscribe(api, sam, "dev1").status_code == 200 and subscribe(api, sam, "dev1").status_code == 200
    assert api.post("/push/unsubscribe", headers=kh, json={"endpoint": EP("dev1")}).status_code == 200                              # someone else's device: nothing happens
    assert main_mod().q("select count(*) n from push_subs where endpoint=%s", (EP("dev1"),), one=True)["n"] == 1
    assert subscribe(api, kh, "dev1").status_code == 200                                                                           # a shared device follows whoever signed in last
    assert main_mod().q("select profile_id from push_subs where endpoint=%s", (EP("dev1"),), one=True)["profile_id"] == kid
    assert api.post("/push/test", headers=kh).status_code == 200 and sent[-1][1]["title"] == "Notifications are on" and sent[-1][0] == {EP("dev1")}
    assert api.post("/push/unsubscribe", headers=kh, json={"endpoint": EP("dev1")}).status_code == 200
    assert main_mod().q("select count(*) n from push_subs where endpoint=%s", (EP("dev1"),), one=True)["n"] == 0

def test_everything_on_the_tab_is_pushed_and_leaves_a_day_after(api, monkeypatch):
    main = main_mod(); sent = spy_push(monkeypatch)
    sid, sam, kid, kh = make_family(api, "Vera", "Vin", topup=100)
    subscribe(api, sam, "vera"); subscribe(api, kh, "vin")
    last = lambda: sent[-1][1]
    inbox = lambda h: api.get("/inbox", headers=h).json()
    # a kid's purchase: a grown-up hears about what happened and is asked about what needs a yes
    buy = lambda pid: api.post("/purchase", headers=kh, json={"profile_id": kid, "product_id": pid}).json()
    assert buy(5)["status"] == "completed" and last()["title"] == "Vin: bought" and last()["url"] == "/?go=Alerts"
    assert buy(3)["status"] == "blocked" and last()["title"] == "Vin: blocked"
    n = len(sent); order = buy(6); assert order["status"] == "pending" and len(sent) == n + 1
    assert last()["title"] == "Vin wants Robot Dino" and last()["url"] == f"/?go=Alerts&item=u{order['order']['id']}"
    assert [a["item"] for a in inbox(sam)["approvals"]] == ["Robot Dino"] and {a["event"] for a in inbox(sam)["activity"]} == {"completed", "blocked"} and inbox(kh)["approvals"] == []
    # approving: the kid is told (a push that opens the Alerts tab), the request stays on the grown-up's tab as "done" for a day, then goes
    assert api.post(f"/approvals/{order['order']['id']}/approve", headers=sam).status_code == 200
    assert last()["title"] == "Approved" and sent[-1][0] == {EP("vin")}
    done = inbox(sam)["approvals"]; assert len(done) == 1 and done[0]["status"] == "completed" and done[0]["decided_at"]
    main.q("update purchases set decided_at=now()-interval '2 days' where id=%s", (order["order"]["id"],)); assert inbox(sam)["approvals"] == []
    # a suggestion: pushed to the person who decides, shown until answered, then a day as "done"
    pid_ = main.propose(kid, kid, "save", "Move $5 to savings?", "A little for your goal.", {"amount": "5"}, ["why"])
    assert last()["url"] == f"/?go=Alerts&item=p{pid_}" and last()["tag"] == f"proposal-{pid_}" and [p["status"] for p in inbox(kh)["proposals"]] == ["open"]
    main.q("update proposals set status='declined', decided_at=now() where id=%s", (pid_,)); assert [p["status"] for p in inbox(kh)["proposals"]] == ["declined"]
    main.q("update proposals set decided_at=now()-interval '2 days' where id=%s", (pid_,)); assert inbox(kh)["proposals"] == []
    # a note that asks for something: opens Add money, waits until it's done, repeats are ignored, then leaves a day after
    n = len(sent); first = main.notify(sid, "Add money to pay the bonus", "Add $5.00.", None, None, key="bonus:7", needs_action=True, act="funds")
    assert first and main.notify(sid, "Add money to pay the bonus", "Add $5.00.", None, None, key="bonus:7", needs_action=True, act="funds") is None and len(sent) == n + 1 and last()["url"] == "/?do=funds"
    api.post("/alerts/seen", headers=sam, json={"upto": 10**9})                                                                       # looking at it doesn't clear it
    assert [a["handled_at"] for a in inbox(sam)["alerts"] if a["id"] == first] == [None]
    main.resolve(sid, "bonus:7"); row = [a for a in inbox(sam)["alerts"] if a["id"] == first][0]; assert row["handled_at"]          # dealt with: still shown, as done
    main.q("update alerts set handled_at=now()-interval '2 days' where id=%s", (first,)); assert first not in [a["id"] for a in inbox(sam)["alerts"]]
    # a plain note: pushed, opens the Alerts tab on that note, leaves a day after it's been seen; a silent one is on the tab but never buzzes
    n = len(sent); info = main.notify(kid, "Allowance arrived", "$5.00 was added.")
    assert len(sent) == n + 1 and last()["url"] == f"/?go=Alerts&item=a{info}"
    quiet = main.notify(kid, "Penny stopped that purchase", "Too much.", silent=True); assert len(sent) == n + 1
    assert {a["id"]: a["handled_at"] for a in inbox(kh)["alerts"]}[info] is None
    assert api.post("/alerts/seen", headers=kh, json={"upto": info}).status_code == 200
    seen = {a["id"]: a["handled_at"] for a in inbox(kh)["alerts"]}; assert seen[info] and seen[quiet] is None                         # only what was on screen counts as seen
    main.q("update alerts set handled_at=now()-interval '2 days' where id=%s", (info,)); assert info not in [a["id"] for a in inbox(kh)["alerts"]]
    # a note nobody ever opens stops cluttering after a week
    old = main.notify(kid, "Old news", "x", silent=True); main.q("update alerts set created_at=now()-interval '8 days' where id=%s", (old,))
    main.tidy(); assert old not in [a["id"] for a in inbox(kh)["alerts"]]

def test_the_real_bonus_and_allowance_alerts_resolve_themselves(api, monkeypatch):
    main = main_mod(); sent = spy_push(monkeypatch)
    sid, sam, kid, kh = make_family(api, "Walt", "Wes", topup=50); subscribe(api, sam, "walt")
    main.q("update profiles set balance=0 where id=%s", (sid,))
    api.post(f"/allowance/{kid}", headers=sam, json={"amount": 10, "first_now": True})                                               # can't pay: asks for money
    row = [a for a in api.get("/inbox", headers=sam).json()["alerts"] if a["title"] == "Add money for the allowance"][0]
    assert not row["handled_at"] and row["needs_action"] and sent[-1][1]["url"] == "/?do=funds"
    fund(api, sam, 100)                                                                                                              # adding money pays it, and that settles the alert
    row = [a for a in api.get("/inbox", headers=sam).json()["alerts"] if a["title"] == "Add money for the allowance"][0]; assert row["handled_at"]
