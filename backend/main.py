import os, re, json, uuid, time, base64, asyncio, logging, hashlib, hmac, threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Literal
from urllib.parse import quote_plus, urlencode, urlparse
import httpx, psycopg, gemini
from psycopg.rows import dict_row
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from cachetools import TTLCache
from psycopg_pool import ConnectionPool
from rules import evaluate, checks
import agent, brain, paypal, paypal_mcp, push

load_dotenv()
pool = ConnectionPool(os.environ["DATABASE_URL"], min_size=1, max_size=10, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None})  # prepare_threshold=None keeps Supabase's pooler happy

async def _sweeper():
    """Every 5 minutes: pay full goals and due weekly allowances (failed charges are retried). Claims are atomic, so extra instances are harmless."""
    while True:
        try: await asyncio.to_thread(sweep_all)
        except Exception: logging.exception("reward sweep failed")
        await asyncio.sleep(300)

@asynccontextmanager
async def lifespan(_):
    pool.open(); task = asyncio.create_task(_sweeper())
    paypal_mcp.start()  # PayPal's MCP server for read-only order lookups; does nothing if it isn't set up
    yield
    task.cancel(); await asyncio.to_thread(paypal_mcp.stop); pool.close()

app = FastAPI(title="Stash", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(","), allow_methods=["*"], allow_headers=["*"])
ai = gemini.Client(max_retries=2, timeout=30.0)
APP_URL = os.getenv("APP_URL", "http://localhost:5173").rstrip("/")
MERCHANT_EMAIL = os.getenv("MERCHANT_PAYOUT_EMAIL", "")

def q(sql, args=(), one=False):
    with pool.connection() as c:
        cur = c.execute(sql, args)
        if cur.description is None:
            return None
        return cur.fetchone() if one else cur.fetchall()

ACTIVITY = {"completed": "Bought", "blocked": "Blocked", "refused": "Stopped", "saved": "Saved", "cancelled": "Cancelled"}  # what a kid does that a grown-up sees on Alerts and is pushed about

def link(go=None, item=None, do=None):
    """Where a tapped notification lands. The web app reads these from the address."""
    return "/?" + urlencode({k: v for k, v in (("go", go), ("item", item), ("do", do)) if v})

def forget_sub(endpoint):
    q("delete from push_subs where endpoint=%s", (endpoint,))

def deliver(pid, title, body, url, tag=None):
    """Push one notification to every device this person is signed in on. Never blocks and never raises."""
    if not push.configured(): return
    try:
        push.enqueue(q("select endpoint,p256dh,auth from push_subs where profile_id=%s", (pid,)), {"title": str(title)[:80], "body": str(body or "")[:160], "url": url, "tag": tag, "pid": pid}, forget_sub)
    except Exception:
        logging.exception("push lookup failed")

def log(pid, event, detail):
    q("insert into audit(profile_id,event,detail) values(%s,%s,%s)", (pid, event, detail))
    if event in ACTIVITY:
        c = q("select name, guardian_id from profiles where id=%s", (pid,), one=True)
        if c and c["guardian_id"]: deliver(c["guardian_id"], f"{c['name']}: {ACTIVITY[event].lower()}", detail, link("Alerts"), tag=f"activity-{pid}")

def notify(pid, title, body, steps=None, goal_id=None, key=None, needs_action=False, act=None, silent=False):
    """Put a message on the person's Alerts tab and push it to their devices.
    key: one open message per key (a repeat is ignored until the first is dealt with). needs_action: stays until the thing it asks for happens (see resolve).
    act='funds': tapping it opens Add money. silent: no push (the person is looking at the app right now)."""
    row = q("""insert into alerts(profile_id,title,body,next_steps,goal_id,key,needs_action,act)
               select %s::int,%s::text,%s::text,%s::text,%s::int,%s::text,%s::boolean,%s::text
               where %s::text is null or not exists (select 1 from alerts where profile_id=%s::int and key=%s::text and handled_at is null) returning id""",
            (pid, title, body, steps, goal_id, key, needs_action, act, key, pid, key), one=True)
    if row and not silent:
        deliver(pid, title, body, link(do="funds") if act == "funds" else link("Alerts", item=f"a{row['id']}"), tag=key or f"alert-{row['id']}")
    return row and row["id"]

def resolve(pid, key):
    """The thing an alert asked for has happened: it now counts as dealt with, and leaves the tab a day later."""
    q("update alerts set handled_at=now() where profile_id=%s and key=%s and handled_at is null", (pid, key))

def tidy():
    """Housekeeping: old unopened notes stop cluttering the tab after a week, and long-finished ones are deleted."""
    q("update alerts set handled_at=now() - interval '1 day' where handled_at is null and not needs_action and created_at < now() - interval '7 days'")
    q("delete from alerts where handled_at < now() - interval '3 days'")

def debit(pid, amount):
    """Atomic: takes the money only if the balance covers it."""
    return bool(q("update profiles set balance=balance-%s where id=%s and balance>=%s returning id", (amount, pid, amount), one=True))

def pay_goal(gid):
    """Goal reached: release the savings, plus the bonus (a percent of what was saved) taken from the guardian's balance."""
    g = q("update goals set status='paying' where id=%s and status='active' and saved>=target returning *", (gid,), one=True)  # atomic claim
    if not g: return False
    c = q("select name, guardian_id from profiles where id=%s", (g["child_id"],), one=True)
    bonus = (g["saved"] * g["bonus_pct"] / 100).quantize(Decimal("0.01"))
    if bonus > 0 and not (c["guardian_id"] and debit(c["guardian_id"], bonus)):
        q("update goals set status='active' where id=%s", (gid,))  # stays full; retried when the guardian adds money
        if not g["charge_failed"]:
            q("update goals set charge_failed=true where id=%s", (gid,))
            if c["guardian_id"]: notify(c["guardian_id"], "Add money to pay the bonus", f"{c['name']} reached {g['name']}. Add ${bonus:.2f} to your balance and we'll pay the bonus automatically.", None, gid, key=f"bonus:{gid}", needs_action=True, act="funds")
            notify(g["child_id"], "Goal reached!", "Your bonus is on its way. We're waiting for your grown-up to add funds. Your savings are safe.", None, gid)
        return False
    q("update profiles set balance=balance+%s where id=%s", (g["saved"] + bonus, g["child_id"]))
    q("update goals set status='paid', paid_bonus=%s, paid_at=now() where id=%s", (bonus, gid))
    if c["guardian_id"]: resolve(c["guardian_id"], f"bonus:{gid}")
    extra = f" plus a ${bonus:.2f} bonus" if bonus > 0 else ""
    notify(g["child_id"], "Goal reached!", f"${g['saved']:.2f}{extra} is in your wallet. Time to get {g['name']}.", "Ask Penny to find it.", gid)
    if c["guardian_id"]: notify(c["guardian_id"], f"{c['name']} reached a goal", f"{g['name']}: " + (f"${bonus:.2f} bonus paid from your balance." if bonus > 0 else "no bonus was set."), None, gid)
    log(g["child_id"], "paid", f"{g['name']}: ${g['saved']:.2f} saved, ${bonus:.2f} bonus")
    return True

def sweep_goals():
    return sum(pay_goal(r["id"]) for r in q("select id from goals where status='active' and saved>=target"))

def pay_allowances():
    """Weekly allowance: taken from the guardian's balance into the child's wallet. The claim is atomic, so it can't pay twice."""
    paid = 0
    for r in q("select id, allowance_next from profiles where allowance>0 and allowance_next <= now()"):
        due = r["allowance_next"]
        c = q("""update profiles set allowance_next = case when %s::timestamptz + interval '7 days' > now() then %s::timestamptz + interval '7 days' else now() + interval '7 days' end
                 where id=%s and allowance>0 and allowance_next=%s returning name, guardian_id, allowance, allowance_failed""", (due, due, r["id"], due), one=True)
        if not c: continue  # another instance claimed it
        amt = c["allowance"]
        if not (c["guardian_id"] and debit(c["guardian_id"], amt)):
            q("update profiles set allowance_next=%s where id=%s and allowance>0", (due, r["id"]))  # try again on the next sweep
            if c["guardian_id"] and not c["allowance_failed"]:
                q("update profiles set allowance_failed=true where id=%s", (r["id"],))
                notify(c["guardian_id"], "Add money for the allowance", f"Your balance is too low to send {c['name']}'s ${amt:.2f} allowance. Add money and we'll send it automatically.", key=f"allowance:{r['id']}", needs_action=True, act="funds")
            continue
        q("update profiles set balance=balance+%s, allowance_failed=false where id=%s", (amt, r["id"]))
        if c["guardian_id"]: resolve(c["guardian_id"], f"allowance:{r['id']}")
        log(r["id"], "allowance", f"${amt:.2f} weekly allowance")
        notify(r["id"], "Allowance arrived", f"${amt:.2f} was added to your wallet.")
        if c["guardian_id"]: notify(c["guardian_id"], f"Allowance sent to {c['name']}", f"${amt:.2f} was taken from your balance and added to their wallet.")
        nudge_save(r["id"], ["Your weekly allowance just arrived"])
        paid += 1
    return paid

def sweep_all():
    n = sweep_goals() + pay_allowances() + sweep_orders() + run_missions() + check_watches() + nudge_stalled() + nudge_limits() + sweep_refunds()
    tidy()
    return n

MAX_GOALS = 20  # per child; a guard against a runaway loop, not a limit anyone should meet
_gkey = lambda t: " ".join(str(t).split()).casefold()  # goal names match ignoring case and extra spaces

def create_goal(child_id, name, target, by, percent=0, product_id=None):
    name = str(name).strip()
    if not name or len(name) > 60: raise HTTPException(422, "Give the goal a short name.")
    t = D(target)
    if t <= 0 or t > 10000: raise HTTPException(422, "Pick a target between $0.01 and $10,000.")
    pct = int(percent) if by != child_id else 0  # only a grown-up adds a bonus, and it's optional
    watch = q("select id, price from products where id=%s", (product_id,), one=True) if product_id else None
    if product_id and not watch: raise HTTPException(422, "Unknown item.")
    with plock(child_id):  # a child can have many goals, but not two with the same name (and not an endless pile)
        mine = q("select name from goals where child_id=%s and status in ('active','paying')", (child_id,))
        if any(_gkey(m["name"]) == _gkey(name) for m in mine): raise HTTPException(409, f"There's already a goal called {name}.")
        if len(mine) >= MAX_GOALS: raise HTTPException(409, f"That's {MAX_GOALS} goals already. Finish or cancel one before adding another.")
        g = q("insert into goals(child_id,name,target,created_by,bonus_pct,watch_product_id,watch_price) values(%s,%s,%s,%s,%s,%s,%s) returning id",
              (child_id, name, t, by, pct, watch and watch["id"], watch and watch["price"]), one=True)
    c = q("select name, guardian_id from profiles where id=%s", (child_id,), one=True)
    log(child_id, "goal", f"Goal: {name}, ${t:.2f}")
    if by == child_id and c["guardian_id"]:
        notify(c["guardian_id"], f"{c['name']} is saving for {name}", f"Goal ${t:.2f}. A bonus is optional, but any percent helps them stay motivated.", None, g["id"])
    elif by != child_id:
        notify(child_id, "A new goal", f"A grown-up set a goal for you: {name}, ${t:.2f}." + (f" Reach it for a {pct}% bonus." if pct else ""), "Move money from your wallet whenever you like.", g["id"])
    return {"id": g["id"], "name": name, "target": float(t)}

def bonus_on_existing(child_id, name, percent):
    """If `name` is one of the child's goals (same words, ignoring case and extra spaces), set that goal's bonus and return it.
    Returns None when no goal has that name, so the caller starts a new goal. Target and saved money are never touched here."""
    with plock(child_id):  # same lock as deposits, so the goal can't reach its target halfway through
        g = next((x for x in q("select id, name, target, bonus_pct, status from goals where child_id=%s and status in ('active','paying') order by id", (child_id,)) if _gkey(x["name"]) == _gkey(name)), None)
        if not g: return None
        if g["status"] != "active": raise HTTPException(409, f"{g['name']} is already reached. Its bonus is being paid, so it can't change now.")
        pct = int(percent)
        q("update goals set bonus_pct=%s where id=%s and status='active'", (pct, g["id"]))
    if pct != int(g["bonus_pct"]):
        log(child_id, "goal", f"Bonus on {g['name']} is now {pct}%")
        notify(child_id, "Your bonus changed", (f"Reach {g['name']} and get a {pct}% bonus." if pct else f"There's no bonus on {g['name']} right now. It's still your goal."), "Move money from your wallet whenever you like.", g["id"])
    return {"id": g["id"], "name": g["name"], "target": float(g["target"]), "bonus_pct": pct, "updated": True}

def pick_goal(pid, goal_id=None):
    """The open goal money should move to or from. Several goals and none named is an error that lists them, so nobody guesses."""
    if goal_id is not None:
        g = q("select * from goals where id=%s and child_id=%s and status='active'", (goal_id, pid), one=True)
        if not g: raise HTTPException(404, "That goal isn't open any more.")
        return g
    gs = q("select * from goals where child_id=%s and status='active' order by id", (pid,))
    if not gs: raise HTTPException(409, "Start a goal first.")
    if len(gs) > 1: raise HTTPException(409, "Which goal? " + ", ".join(x["name"] for x in gs) + ".")
    return gs[0]

def goal_by_name(pid, name):
    """The id of the open goal the model named, or None when it named none (pick_goal then decides)."""
    if not name: return None
    g = next((x for x in q("select id, name from goals where child_id=%s and status='active'", (pid,)) if _gkey(x["name"]) == _gkey(name)), None)
    if not g: raise HTTPException(404, f"There's no open goal called {name}.")
    return g["id"]

def deposit(pid, amount, goal_id=None):
    a = D(amount)
    if a <= 0: raise HTTPException(422, "Pick an amount above zero.")
    with plock(pid):  # serialised with purchases and cancels for this child
        g = pick_goal(pid, goal_id)
        need = g["target"] - g["saved"]
        if a > need: raise HTTPException(422, f"You only need ${need:.2f} more.")
        if not q("update profiles set balance=balance-%s where id=%s and balance>=%s returning id", (a, pid, a), one=True):
            raise HTTPException(409, "There isn't enough in your wallet.")
        q("update goals set saved=saved+%s, last_saved_at=now() where id=%s", (a, g["id"]))
        log(pid, "saved", f"${a:.2f} moved to {g['name']}")
    paid = pay_goal(g["id"])
    return {"goal_id": g["id"], "name": g["name"], "saved": float(g["saved"] + a), "target": float(g["target"]), "reached": g["saved"] + a >= g["target"], "paid": paid}

def take_back(pid, amount, goal_id=None):
    a = D(amount)
    if a <= 0: raise HTTPException(422, "Pick an amount above zero.")
    with plock(pid):
        g = pick_goal(pid, goal_id)
        if not q("update goals set saved=saved-%s where id=%s and status='active' and saved>=%s returning id", (a, g["id"], a), one=True): raise HTTPException(409, "There isn't that much in savings.")
        q("update profiles set balance=balance+%s where id=%s", (a, pid))
        log(pid, "saved", f"${a:.2f} moved back from {g['name']}")
    return {"ok": True}

# ---------- Rules engine: plain code, never the model ----------
D = lambda x: Decimal(str(x))

def load_ctx(pid):
    """One round trip: profile, rules and 7-day spend in a single indexed query."""
    return q("""select p.frozen, p.balance, r.per_purchase_cap, r.weekly_budget, r.approval_threshold, r.allowed_stores,
             (select coalesce(sum(amount),0) from purchases where profile_id=p.id and status='completed' and created_at > now()-interval '7 days') spent
             from profiles p join rules r on r.profile_id=p.id where p.id=%s""", (pid,), one=True)

def judge(ctx, price, store, partner=True):
    rules = {k: ctx[k] for k in ("per_purchase_cap", "weekly_budget", "approval_threshold", "allowed_stores")}
    return evaluate(ctx["frozen"], ctx["balance"], rules, ctx["spent"], D(price), store, partner)

def judge_checks(ctx, price, store, partner=True):
    """The rule checklist for one price, for display (the decision itself comes from judge)."""
    rules = {k: ctx[k] for k in ("per_purchase_cap", "weekly_budget", "approval_threshold", "allowed_stores")}
    return checks(ctx["frozen"], ctx["balance"], rules, ctx["spent"], D(price), store, partner)

def check_rules(pid, prod, partner=True):
    return judge(load_ctx(pid), prod["price"], prod["store"], partner)

def settle(pid, prod, why, purchase_id=None, trace=None):
    """Pay for a partner-store item: the price leaves the child's wallet, PayPal pays the merchant, and the order is recorded.
    The debit is atomic and comes first; it is given back if PayPal fails."""
    if not debit(pid, prod["price"]): raise HTTPException(409, "Not enough balance.")
    try:
        ref = paypal.payout(MERCHANT_EMAIL, prod["price"], f"{prod['name']} ({prod['store']})", f"purchase-{purchase_id or uuid.uuid4().hex[:12]}")
    except paypal.PayPalError:
        q("update profiles set balance=balance+%s where id=%s", (prod["price"], pid))
        raise HTTPException(502, "The payment didn't go through. Nothing was charged.")
    if purchase_id:
        q("update purchases set status='completed', reason=%s, paypal_ref=%s, fulfillment='processing', trace=coalesce(trace,'[]'::jsonb) || %s where id=%s", (why, ref, Jsonb(["A grown-up approved it", "Paid and ordered"]), purchase_id))
        return purchase_id
    return q("insert into purchases(profile_id,product_id,amount,status,reason,paypal_ref,fulfillment,trace) values(%s,%s,%s,'completed',%s,%s,'processing',%s) returning id", (pid, prod["id"], prod["price"], why, ref, Jsonb(trace or [])), one=True)["id"]

ORDER_SQL = """select pu.id, p.name as item, p.store, pu.amount, pu.status, pu.fulfillment, pu.reason, pu.created_at, pu.shipped_at, pu.delivered_at,
               (pu.reason ilike 'approved%%') as was_approved, pu.trace from purchases pu join products p on p.id=pu.product_id"""

def attempt(pid, product_id, trace=None, approved=False):
    """Run a purchase through the rules. approved=True means a person already said yes to the approval line (the other limits still apply)."""
    prod = q("select * from products where id=%s", (product_id,), one=True)
    if not prod: raise HTTPException(404, "Unknown product")
    steps = list(trace or [])
    with plock(pid):  # serialises check + debit per profile, so concurrent buys can't beat the weekly budget (single process)
        verdict, why = check_rules(pid, prod)
        if verdict == "needs_approval" and approved:
            who = q("select role from profiles where id=%s", (pid,), one=True)["role"]
            verdict, why = "ok", "Approved by guardian." if who == "child" else "Approved by you."
        steps.append(f"Checked the rules: {why}")
        if verdict == "ok":
            steps.append("Paid and ordered")
            oid = settle(pid, prod, why, trace=steps); status = "completed"
        else:
            status = "pending" if verdict == "needs_approval" else "blocked"
            steps.append("Sent to a grown-up for approval" if status == "pending" else "Not bought")
            oid = q("insert into purchases(profile_id,product_id,amount,status,reason,trace) values(%s,%s,%s,%s,%s,%s) returning id", (pid, prod["id"], prod["price"], status, why, Jsonb(steps)), one=True)["id"]
    log(pid, "bought" if approved and status == "completed" else status, f"{prod['name']} (${float(prod['price']):.2f}): {why}")  # a grown-up's own yes isn't news to them
    if status == "pending":
        c = q("select name, guardian_id from profiles where id=%s", (pid,), one=True)
        if c and c["guardian_id"]: deliver(c["guardian_id"], f"{c['name']} wants {prod['name']}", f"${float(prod['price']):.2f}. Tap to approve or decline.", link("Alerts", item=f"u{oid}"), tag=f"approval-{oid}")
    return {"status": status, "reason": why, "product": prod["name"], "order": q(ORDER_SQL + " where pu.id=%s", (oid,), one=True) if status != "blocked" else None}

def sweep_orders():
    """Demo fulfilment for the mock partner stores: shipped after 2 minutes, delivered 3 minutes later. A real partner reports this itself."""
    n = 0
    for r in q("""update purchases set fulfillment='shipped', shipped_at=now() where status='completed' and fulfillment='processing' and created_at < now() - interval '2 minutes'
                  returning profile_id, (select name from products where id=product_id) as item"""):
        notify(r["profile_id"], "On its way", f"{r['item']} has shipped."); n += 1
    for r in q("""update purchases set fulfillment='delivered', delivered_at=now() where status='completed' and fulfillment='shipped' and shipped_at < now() - interval '3 minutes'
                  returning profile_id, (select name from products where id=product_id) as item"""):
        notify(r["profile_id"], "Delivered", f"{r['item']} has arrived."); n += 1
    return n


# ---------- Autonomy: the agent proposes, the rules decide, a person approves ----------
def propose(profile_id, child_id, kind, title, body, data=None, trace=None):
    """File a proposal for a person to accept or decline. The agent never spends on its own authority."""
    row = q("insert into proposals(profile_id,child_id,kind,title,body,data,trace) values(%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning id",
            (profile_id, child_id, kind, title, body, Jsonb(data or {}), Jsonb(trace or [])), one=True)
    if row: deliver(profile_id, title, body, link("Alerts", item=f"p{row['id']}"), tag=f"proposal-{row['id']}")
    return row["id"] if row else None

def recently(child_id, kind, days):
    return bool(q("select 1 from proposals where child_id=%s and kind=%s and created_at > now() - %s::interval", (child_id, kind, f"{days} days"), one=True))

def plan_mission(mid):
    """Turn a mission ("an art kit and a soccer ball under $30") into one basket proposal, in the background."""
    m = q("update missions set status='planning' where id=%s and status='queued' returning *", (mid,), one=True)
    if not m: return None
    p = q("select id, name, role, guardian_id, interests from profiles where id=%s", (m["profile_id"],), one=True)
    ctx, rules = load_ctx(p["id"]), q("select * from rules where profile_id=%s", (p["id"],), one=True)
    room = min(m["budget"], rules["weekly_budget"] - ctx["spent"], ctx["balance"])
    trace = [f"Mission: {m['title']}, budget ${m['budget']:.2f}", f"Spending room is ${max(room, 0):.2f}: the lowest of the budget, the weekly limit that is left, and the wallet"]
    groups, missing = [], []
    for it in m["items"]:
        term, qty = str(it["query"])[:60], max(1, min(int(it.get("qty", 1)), 5))
        like = f"%{term}%"
        rows = q("select id,name,store,price from products where (name ilike %s or category ilike %s) and store = any(%s) and price <= %s order by price limit 5", (like, like, rules["allowed_stores"], rules["per_purchase_cap"]))  # the rules decide what is on the table
        if not rows:
            missing.append(term); trace.append(f"Looked for '{term}': nothing in the allowed stores within the per-purchase limit"); continue
        groups.append({"query": term, "qty": qty, "options": rows})
    ranked = brain.rank_basket({"name": p["name"], "role": p["role"], "interests": p["interests"] or []}, m["title"], groups, max(room, D(0))) if groups else None  # the model picks among them
    if groups and not ranked and brain.enabled(): trace.append("The AI helper wasn't available, so I used the rules: the cheapest allowed option for each item")
    basket = []
    for g_ in groups:
        term, opts = g_["query"], {o["id"]: o for o in g_["options"]}
        pick = ranked and ranked["picks"].get(term)
        if pick:
            b = opts[pick[0]]; trace.append(f"Penny picked {b['name']} at {b['store']} for ${b['price']:.2f} for '{term}': {pick[1]}")
        else:
            b = g_["options"][0]; trace.append(f"Looked for '{term}': {len(opts)} match(es), picked {b['name']} at {b['store']} for ${b['price']:.2f} (the cheapest)")
        basket.append({"product_id": b["id"], "name": b["name"], "store": b["store"], "price": float(b["price"]), "qty": g_["qty"], "_q": term})
    total = sum(D(b["price"]) * b["qty"] for b in basket)
    if ranked and total > room:  # the model says what matters least; the code only accepts that when the budget really is short
        for b in [b for b in basket if b["_q"] in ranked["drop"]]:
            if total > room:
                basket.remove(b); total -= D(b["price"]) * b["qty"]
                trace.append(f"Over the spending room, so Penny left out {b['name']}: {ranked['drop'][b['_q']]}"); missing.append(b["name"])
    while basket and total > room:  # and the budget is a hard limit either way
        worst = max(basket, key=lambda b: b["price"] * b["qty"]); basket.remove(worst); total -= D(worst["price"]) * worst["qty"]
        trace.append(f"Over the spending room, so I dropped {worst['name']}"); missing.append(worst["name"])
    if not basket:
        q("update missions set status='failed' where id=%s", (mid,))
        notify(p["id"], "I couldn't finish that mission", f"{m['title']}: nothing fit the budget and rules. " + ("Try a bigger budget or different items." if missing else ""), None)
        return None
    trace.append(f"Basket total ${total:.2f}. One approval covers all of it")
    lines = ", ".join(f"{b['name']} ${b['price']:.2f}" + (f" x{b['qty']}" if b["qty"] > 1 else "") for b in basket)
    body = f"{lines}. Total ${total:.2f} of your ${m['budget']:.2f} budget." + (f" I couldn't include: {', '.join(missing)}." if missing else "") + (f" {ranked['summary']}" if ranked and ranked.get("summary") else "")
    approver = p["guardian_id"] if p["role"] == "child" and p["guardian_id"] else p["id"]
    pid_ = propose(approver, p["id"], "mission", f"{m['title']}: approve this basket?" if approver != p["id"] else f"{m['title']}: ready to order?", body,
                   {"mission_id": mid, "basket": [{k: v for k, v in b.items() if k != "_q"} for b in basket], "total": str(total), "buyer": p["name"], "by": "ai" if ranked else "rules"}, trace)
    q("update missions set status='proposed' where id=%s", (mid,))
    if approver != p["id"]: notify(p["id"], "Mission ready", f"I sent your basket for {m['title']} to a grown-up. You'll hear back soon.", None)
    return pid_

def run_missions():
    return sum(1 for r in q("select id from missions where status='queued' and created_at < now() - interval '1 minute'") if plan_mission(r["id"]))

def cancel_order(purchase_id, event_note):
    """Cancel a paid order that hasn't arrived and put the money back in the wallet. (A real store would also refund through PayPal.)"""
    row = q("update purchases set status='cancelled', fulfillment='cancelled' where id=%s and status='completed' and fulfillment in ('processing','shipped') returning profile_id, amount", (purchase_id,), one=True)
    if not row: raise HTTPException(409, "That order can't be cancelled any more.")
    q("update profiles set balance=balance+%s where id=%s", (row["amount"], row["profile_id"]))
    log(row["profile_id"], "refund", f"${row['amount']:.2f} back: {event_note}")
    return row

def cancellable(r):
    """An order a person can still call off: a request waiting for approval, or a paid one that hasn't been delivered."""
    return r["status"] == "pending" or (r["status"] == "completed" and r["fulfillment"] in ("processing", "shipped"))

def order_list(pid):
    """A person's recent orders, for Penny's list_orders tool."""
    rows = q(ORDER_SQL + " where pu.profile_id=%s and pu.status in ('completed','pending','cancelled','declined') order by pu.id desc limit 5", (pid,))
    return {"orders": [{"order_id": r["id"], "item": r["item"], "store": r["store"], "amount": r["amount"], "status": r["status"], "delivery": r["fulfillment"], "can_cancel": cancellable(r),
                        "date": r["created_at"].strftime("%Y-%m-%d")} for r in rows]} if rows else {"orders": [], "note": "No orders yet."}

def cancel_my_order(by, order_id=None, owner=None):
    """Call off an order that hasn't been delivered, for the person who made it or their guardian. One function for the button, the API and Penny.
    A paid order goes back to the wallet; a request nobody has approved yet is simply withdrawn. Both claims are atomic, so a double tap or a race with
    the guardian's approval or the delivery sweep can't do it twice. Raises HTTPException with a plain reason when it can't."""
    owner = owner or by
    if order_id is None:  # "cancel my order": fine when there's exactly one candidate, otherwise ask which
        rows = [r for r in q(ORDER_SQL + " where pu.profile_id=%s and pu.status in ('pending','completed') order by pu.id desc limit 10", (owner,)) if cancellable(r)]
        if not rows: raise HTTPException(404, "There's no order that can be cancelled. Delivered orders can't be.")
        if len(rows) > 1: raise HTTPException(409, "More than one order can be cancelled (" + "; ".join(f"order {r['id']}: {r['item']} ${r['amount']:.2f}" for r in rows[:4]) + "). Ask which one.")
        order_id = rows[0]["id"]
    with plock(owner):
        o = q(ORDER_SQL + " where pu.id=%s and pu.profile_id=%s", (order_id, owner), one=True)
        if not o: raise HTTPException(404, "I can't find that order on your account.")
        who = "you" if by == owner else "a grown-up"
        if o["status"] == "pending":
            if not q("update purchases set status='cancelled', decided_at=now() where id=%s and status='pending' returning id", (order_id,), one=True):
                raise HTTPException(409, "That request was just decided, so it can't be cancelled.")
            back, step = D(0), f"Cancelled by {who} before it was approved"
        else:
            back = cancel_order(order_id, f"cancelled by {who}")["amount"]  # 409 if it was delivered a moment ago
            step = f"Cancelled by {who}. ${back:.2f} went back to the wallet"
        q("update purchases set trace=coalesce(trace,'[]'::jsonb) || %s where id=%s", (Jsonb([step]), order_id))
    if by == owner: log(owner, "cancelled", f"{o['item']} (${o['amount']:.2f}): " + ("request withdrawn, nothing was charged" if back == 0 else "cancelled, money back in the wallet"))
    else: notify(owner, "Order cancelled", f"A grown-up cancelled {o['item']}." + (f" ${back:.2f} is back in your wallet." if back else " Nothing was charged."))
    return {"status": "cancelled", "item": o["item"], "refunded": back, "where": "the wallet" if back else None,
            "note": f"${back:.2f} is back in the wallet." if back else "Nothing was charged.", "order": q(ORDER_SQL + " where pu.id=%s", (order_id,), one=True)}

def suggest_replacement(o, trace, cancel_first):
    rules = q("select * from rules where profile_id=%s", (o["profile_id"],), one=True)
    rep = q("select id,name,store,price from products where category=%s and id<>%s and store = any(%s) and price <= %s order by abs(price - %s) limit 1", (o["category"], o["product_id"], rules["allowed_stores"], rules["per_purchase_cap"], o["amount"]), one=True)
    if not rep:
        notify(o["profile_id"], "No replacement found", f"I couldn't find a similar {o['category']} item that fits your rules.", None); return None
    trace = trace + [f"Looked for similar {o['category']} items within the rules: closest is {rep['name']} at {rep['store']} for ${rep['price']:.2f}"]
    return propose(o["profile_id"], o["profile_id"], "replace", f"Swap in {rep['name']}?",
                   f"{rep['name']} at {rep['store']} is ${rep['price']:.2f}, close to the ${o['amount']:.2f} for {o['item']}." + (" I'll cancel the late order and refund you first." if cancel_first else ""),
                   {"product_id": rep["id"], "order_id": o["id"], "cancel_first": cancel_first}, trace)

def handle_partner_event(purchase_id, event):
    """A partner store reports a problem. cancelled: refund and offer a replacement. delayed: offer to cancel and swap."""
    o = q("select pu.id, pu.profile_id, pu.product_id, pu.amount, p.category, p.name as item, p.store from purchases pu join products p on p.id=pu.product_id where pu.id=%s", (purchase_id,), one=True)
    if not o: raise HTTPException(404, "Unknown order")
    if event == "cancelled":
        cancel_order(purchase_id, f"{o['store']} cancelled {o['item']}")
        notify(o["profile_id"], "Order cancelled", f"{o['store']} couldn't send {o['item']}. Your ${o['amount']:.2f} is back in your wallet.", None)
        return suggest_replacement(o, [f"{o['item']} was cancelled by {o['store']}", f"Refunded ${o['amount']:.2f} to the wallet"], False)
    if event == "delayed":
        notify(o["profile_id"], "Running late", f"{o['item']} from {o['store']} is taking longer than expected.", None)
        return suggest_replacement(o, [f"{o['item']} from {o['store']} is running late"], True)
    raise HTTPException(422, "Unknown event")

def check_watches():
    """Goals tied to an item: notice a price drop, offer to lower the target, and (only if a grown-up set a buy-below price) buy it."""
    n = 0
    for g in q("""select g.*, p.price as now_price, p.name as item, p.store, pr.guardian_id, pr.name as kid from goals g join products p on p.id=g.watch_product_id
                  join profiles pr on pr.id=g.child_id where g.status='active' and g.watch_product_id is not null and g.watch_price is distinct from p.price"""):
        old, new = g["watch_price"], g["now_price"]
        q("update goals set watch_price=%s where id=%s", (new, g["id"]))
        if new > old: continue
        n += 1
        trace = [f"Checked the price of {g['item']} at {g['store']}: ${old:.2f} to ${new:.2f}"]
        if g["buy_below"] is not None and new <= g["buy_below"] and g["saved"] >= new:
            trace += [f"{g['kid']}'s grown-up allowed buying at ${g['buy_below']:.2f} or less", f"Savings of ${g['saved']:.2f} cover the price: lowering the goal to ${new:.2f}"]
            q("update goals set target=%s where id=%s and status='active' and target > %s", (new, g["id"], new))
            if pay_goal(g["id"]):  # releases the savings and pays any bonus
                r = attempt(g["child_id"], g["watch_product_id"], trace=trace + ["Goal reached, so I used the money to buy it"], approved=True)
                msg_ = f"Bought {g['item']} for ${new:.2f}." if r["status"] == "completed" else f"I tried to buy {g['item']} but: {r['reason']}"
                notify(g["child_id"], "Price drop: I bought it", msg_, None)
                if g["guardian_id"]: notify(g["guardian_id"], f"Auto-buy for {g['kid']}", msg_, None)
            continue
        drop = old - new; new_target = max(g["saved"], g["target"] - drop)
        if new_target < g["target"]:
            title, body, by = f"{g['item']} just got ${drop:.2f} cheaper", f"Lower your goal from ${g['target']:.2f} to ${new_target:.2f}?", "rules"
            steps = trace + [f"The goal drops by the same ${drop:.2f}, never below what you've saved"]
            ai = brain.explain_price_drop({"kid": g["kid"], "item": g["item"], "store": g["store"], "old": old, "new": new, "drop": drop, "old_target": g["target"], "new_target": new_target, "saved": g["saved"]})
            if ai: title, body, by, steps = ai[0], ai[1], "ai", steps + ["Penny (the AI) wrote the explanation. The numbers come from the app"]
            propose(g["child_id"], g["child_id"], "price_drop", title, body, {"goal_id": g["id"], "new_target": str(new_target), "by": by}, steps)
    return n

def nudge_save(child_id, why, goal_id=None):
    """Suggest moving a slice of the wallet into savings, at most once every 6 days.
    Penny (the model) picks the amount and writes the note; the app caps the amount at half the wallet and falls back to a quarter of it."""
    g = (q("select id, name, target, saved from goals where id=%s and child_id=%s and status='active'", (goal_id, child_id), one=True) if goal_id else
         q("select id, name, target, saved from goals where child_id=%s and status='active' and saved < target order by id limit 1", (child_id,), one=True))
    kid = q("select name, balance from profiles where id=%s", (child_id,), one=True); bal = kid["balance"]
    if not g or recently(child_id, "save", 6): return None
    need = g["target"] - g["saved"]
    amt = min(need, max(Decimal(1), (bal / 4).to_integral_value(rounding="ROUND_FLOOR")))
    if amt < 1 or bal < amt: return None
    title, body, by = f"Move ${amt:.0f} to savings?", f"That brings {g['name']} to ${g['saved'] + amt:.2f} of ${g['target']:.2f}.", "rules"
    steps = why + [f"Wallet is ${bal:.2f}; a quarter of it is a comfortable amount to move", f"{g['name']} still needs ${need:.2f}"]
    ai = brain.write_save_nudge({"kid": kid["name"], "goal": g["name"], "target": g["target"], "saved": g["saved"], "need": need, "wallet": bal, "reasons": why})
    if ai:
        amt, title, body, by = Decimal(ai[0]), ai[1], ai[2], "ai"
        steps = why + [f"Wallet is ${bal:.2f} and {g['name']} still needs ${need:.2f}", f"Penny (the AI) suggested ${amt:.0f} and wrote this note. The app keeps it to half the wallet or less"]
    return propose(child_id, child_id, "save", title, body, {"amount": str(amt), "by": by, "goal_id": g["id"]}, steps)

def nudge_stalled():
    return sum(1 for r in q("select id, child_id from goals where status='active' and coalesce(last_saved_at, created_at) < now() - interval '7 days' order by id") if nudge_save(r["child_id"], ["You haven't added to this goal for a week"], r["id"]))

def nudge_limits():
    """Tell a grown-up when a child keeps hitting the per-purchase limit, and suggest a new one based on what they actually tried."""
    n = 0
    for r in q("""select profile_id, count(*) n, max(amount) top from purchases where status='blocked' and reason like 'The limit for one purchase%%' and created_at > now() - interval '7 days'
                  group by profile_id having count(*) >= 3"""):
        c = q("select p.name, p.guardian_id, ru.per_purchase_cap cap from profiles p join rules ru on ru.profile_id=p.id where p.id=%s", (r["profile_id"],), one=True)
        if not c or not c["guardian_id"] or recently(r["profile_id"], "raise_limit", 7): continue
        new = r["top"].to_integral_value(rounding="ROUND_CEILING")
        if new > c["cap"]:
            title, body, by = f"Raise {c['name']}'s limit to ${new:.0f}?", f"{c['name']} tried {r['n']} purchases over the ${c['cap']:.2f} limit this week, up to ${r['top']:.2f}.", "rules"
            steps = [f"Counted {r['n']} purchases blocked by the per-purchase limit in the last 7 days", f"The biggest was ${r['top']:.2f}", f"A limit of ${new:.0f} would have allowed all of them"]
            ai = brain.write_limit_nudge({"kid": c["name"], "cap": c["cap"], "tries": r["n"], "top": r["top"], "new_cap": new})
            if ai: title, body, by, steps = ai[0], ai[1], "ai", steps + ["Penny (the AI) wrote this note. The numbers come from the app"]
            propose(c["guardian_id"], r["profile_id"], "raise_limit", title, body, {"to": str(new), "by": by}, steps); n += 1
    return n

def execute_proposal(pr, uid):
    d, kind = pr["data"], pr["kind"]
    if kind == "refund": return execute_refund(uid, d["order_id"], d["amount"])
    if kind == "mission":
        buyer, results = pr["child_id"], []
        for line in d["basket"]:
            for _ in range(line["qty"]):
                try: r = attempt(buyer, line["product_id"], trace=[f"Part of the mission: {d.get('buyer', '')}'s basket", "A person approved the basket"], approved=True)
                except HTTPException as e: results.append({"name": line["name"], "status": "failed", "reason": e.detail}); break
                results.append({"name": line["name"], "status": r["status"], "reason": r["reason"]})
        q("update missions set status='done' where id=%s", (d["mission_id"],))
        done = sum(1 for x in results if x["status"] == "completed")
        notify(buyer, "Mission done", f"{done} of {len(results)} items ordered.", None)
        return {"results": results}
    if kind == "replace":
        if d.get("cancel_first"): cancel_order(d["order_id"], "swapped for a replacement")
        return {"result": attempt(pr["child_id"], d["product_id"], trace=["You accepted the replacement"], approved=False)["status"]}
    if kind == "price_drop":
        q("update goals set target=%s where id=%s and status='active' and target > %s", (D(d["new_target"]), d["goal_id"], D(d["new_target"])))
        pay_goal(d["goal_id"]); log(pr["child_id"], "goal", f"Goal lowered to ${float(d['new_target']):.2f}")
        return {"ok": True}
    if kind == "save":
        return deposit(pr["child_id"], d["amount"], d.get("goal_id"))
    if kind == "raise_limit":
        if not q("update rules set per_purchase_cap=%s where profile_id=%s and profile_id in (select id from profiles where guardian_id=%s) returning profile_id", (D(d["to"]), pr["child_id"], uid), one=True): deny()
        log(pr["child_id"], "rules", f"Per-purchase limit raised to ${float(d['to']):.0f}")
        return {"ok": True}
    raise HTTPException(422, "Unknown proposal")

# ---------- Real-store search (Channel3) ----------
OFFERED = TTLCache(maxsize=5000, ttl=900)  # (profile, product id) the agent showed, so it can only buy what was really offered
OFFERS = TTLCache(maxsize=2000, ttl=900)  # (profile, url) -> offer; bounded, expires, never shared across users
_lock = threading.Lock()
_locks = [threading.Lock() for _ in range(64)]
def plock(pid): return _locks[pid % 64]
_hits = TTLCache(maxsize=10000, ttl=600)

def throttle(pid, limit=20, window=60):
    """Sliding-window limiter for costly calls (in-memory: use Redis once there is more than one instance)."""
    with _lock:
        now, dq = time.monotonic(), _hits.setdefault(pid, deque())
        while dq and now - dq[0] > window: dq.popleft()
        if len(dq) >= limit: raise HTTPException(429, "Slow down a little and try again soon.")
        dq.append(now)

def product_image(p):
    """First usable photo URL from a Channel3 product. Field names vary, so try the likely ones; https only."""
    for k in ("image_urls", "images", "image_url", "image", "thumbnail"):
        v = p.get(k)
        for x in (v if isinstance(v, list) else [v]):
            u = x.get("url") if isinstance(x, dict) else x
            if isinstance(u, str) and u.startswith("https://"): return u
    return None

def web_search(pid, query=None, image_b64=None):
    key = os.getenv("CHANNEL3_API_KEY")
    if not key: raise HTTPException(503, "Store search isn't set up yet.")
    body = {"query": query} if query else {"base64_image": image_b64}
    try:
        res = httpx.post("https://api.trychannel3.com/v1/search", headers={"x-api-key": key}, json=body, timeout=30)
    except httpx.HTTPError as e:
        logging.warning("Channel3 search failed to connect: %s", type(e).__name__)
        raise HTTPException(502, "Product search failed")
    if res.status_code >= 400:
        logging.warning("Channel3 search returned %s: %s", res.status_code, res.text[:300])  # 401/403 = key problem, 429 = quota
        raise HTTPException(502, "Product search failed")
    out, bad, ctx = [], None, load_ctx(pid)
    try: data = res.json()
    except ValueError: data = None
    items = (data.get("products") if isinstance(data, dict) else data) or []
    for p in items[:5]:
        try:
            offers = [o for o in p.get("offers") or [] if _offer_price(o) is not None and str(o.get("availability", "InStock")).replace("_", "").lower() in ("instock", "limitedavailability")]
            if not offers: continue
            o = min(offers, key=_offer_price)
            price, store = _offer_price(o), o.get("domain") or o.get("merchant_domain")
            if not store or not o.get("url"): raise KeyError("domain or url")
            verdict, why = judge(ctx, price, store, False)
            with _lock: OFFERS[(pid, o["url"])] = {"title": p.get("title"), "store": store, "price": price}
            out.append({"title": p.get("title"), "store": store, "price": price, "url": o["url"], "image": product_image(p), "verdict": verdict, "why": why, "checks": judge_checks(ctx, price, store, False)})
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            bad = bad or p  # Channel3 sent something shaped differently from what we expect
    if bad is not None and not out:
        logging.warning("Channel3 products we couldn't read. First one: %s", json.dumps(bad, default=str)[:700])  # paste this line if search still fails
    return out

def _offer_price(o):
    """An offer's price as a number. Channel3's docs show {"price": {"amount": 9}}, {"price": {"price": 9}} and a bare number, so all three work. None if there isn't one."""
    v = o.get("price") if isinstance(o, dict) else None
    if isinstance(v, dict): v = v.get("amount", v.get("price"))
    try: return float(v) if v is not None else None
    except (TypeError, ValueError): return None

# Backup when the live price search fails or finds nothing: links that open each store's own search page. Built by code from this list, never
# written by the model, so they can't be made up. Add or remove stores here (use {q} where the search words go).
STORE_LINKS = [("Jumia", "https://www.jumia.co.ke/catalog/?q={q}"), ("Kilimall", "https://www.kilimall.co.ke/search?q={q}"), ("Amazon", "https://www.amazon.com/s?k={q}"),
               ("Best Buy", "https://www.bestbuy.com/site/searchpage.jsp?st={q}"), ("eBay", "https://www.ebay.com/sch/i.html?_nkw={q}")]

def store_links(pid, term):
    words, out = quote_plus(term), []
    for name, tpl in STORE_LINKS:
        url = tpl.format(q=words)
        with _lock: OFFERS[(pid, url)] = {"title": term, "store": name, "price": None}
        out.append({"title": term, "store": name, "url": url, "link_only": True})
    return out

# ---------- Penny's memory ----------
# The last day of chat is kept in the chat_messages table (never trusted from the browser). Each assistant row also keeps short "memo" facts
# from the tools that turn used (what was shown with ids and prices, what was bought), so a "yes" next turn can buy the item that was offered.
def ago(mins): return "just now" if mins < 1 else f"{mins} min ago" if mins < 90 else f"{round(mins / 60)} h ago"

def safe_title(t): return " ".join(re.sub(r"[^\w\s$.,&'()/+-]", "", str(t or "")).split())[:50]  # one line, plain characters only  # titles from other stores are untrusted text on its way into the prompt

def tool_note(name, inp, result):
    """One short fact about what a tool just returned. Never raises: a failure here must not hide that a purchase happened."""
    try:
        if name == "search_products" and isinstance(result, list):
            return ["Showed partner items: " + "; ".join(f"{x['name']} ({x['store']}, ${x['price']:.2f}, id {x['id']})" for x in result[:5])] if result else []
        if name == "search_stores" and isinstance(result, list):
            return ["Showed other-store results: " + "; ".join(f"{safe_title(o['title'])} ({safe_title(o['store'])}, ${float(o['price']):.2f})" for o in result[:3])] if result else []
        if name == "buy" and isinstance(result, dict): return [f"Tried to buy {safe_title(result.get('product'))}: {result.get('status')}. {safe_title(result.get('reason'))}" + (f" (order {result['order']['id']})" if isinstance(result.get("order"), dict) and result["order"].get("id") else "")]
        if name == "cancel_order" and isinstance(result, dict):
            return [f"Cancelled the order for {safe_title(result.get('item'))}" + (f"; ${float(result['refunded']):.2f} went back to the wallet" if result.get("refunded") else " (nothing had been charged)")]
        if name == "start_mission": return [f"Started the mission \"{safe_title(inp.get('title'))}\" with a ${float(inp['budget']):.2f} budget"]
        if name == "set_goal" and isinstance(result, dict): return [f"Started the savings goal {safe_title(result.get('name'))} (${float(result['target']):.2f})"]
        if name == "save_money" and isinstance(result, dict): return [f"Moved money into savings for {safe_title(result.get('name'))}: ${result['saved']:.2f} of ${result['target']:.2f}" + (" (goal reached)" if result.get("reached") else "")]
    except Exception:
        logging.exception("couldn't note a tool result")
    return []

def load_memory(pid):
    """The recent turns for the model, plus the facts the tools returned in them. Items shown in the last 30 minutes count as offered again (so a yes can buy them)."""
    rows = q("""select role, content, memo, offered, round(extract(epoch from now() - created_at) / 60)::int as mins from chat_messages
                where profile_id=%s and created_at > now() - interval '24 hours' order by id desc limit 12""", (pid,))[::-1]
    facts = []
    for r in rows:
        if r["memo"]: facts.append(f"{ago(r['mins'])}: {r['memo']}")
        if r["mins"] <= 30:
            for o in r["offered"] or []:
                try: OFFERED[(pid, int(o["id"]))] = D(o["price"])
                except (KeyError, TypeError, ValueError, ArithmeticError): pass
    return [{"role": r["role"], "content": r["content"]} for r in rows], facts[-6:]

def remember(pid, message, text, picks, order, notes, offered):
    """Save one finished turn. A failure here is logged and never breaks the reply."""
    try:
        with pool.connection() as c, c.transaction():
            c.execute("insert into chat_messages(profile_id, role, content) values(%s,'user',%s)", (pid, (message or "").strip()[:500] or "(sent a photo)"))
            c.execute("insert into chat_messages(profile_id, role, content, memo, offered, picks, order_id) values(%s,'assistant',%s,%s,%s,%s,%s)",
                      (pid, text[:1500], " ".join(notes)[:1200] or None, Jsonb(offered), Jsonb(picks) if picks else None, order["id"] if order else None))
            c.execute("delete from chat_messages where profile_id=%s and id not in (select id from chat_messages where profile_id=%s order by id desc limit 60)", (pid, pid))
    except Exception:
        logging.exception("couldn't save the chat")

# ---------- Agent / coach ----------
def run_agent(pid, message, history=None, image_b64=None):
    """One chat turn. Returns (reply text, product picks, order, mission ids). The model proposes; the code decides what is allowed."""
    p = q("select * from profiles where id=%s", (pid,), one=True)
    r = q("select * from rules where profile_id=%s", (pid,), one=True)
    g = q("select name, target, saved, bonus_pct from goals where child_id=%s and status='active' order by id", (pid,))
    kid = p["role"] == "child"
    past, facts = load_memory(pid) if history is None else ([dict(h) for h in history][-8:], [])  # None: Penny's own saved memory of this chat
    system = agent.build_system(p, r, g, facts)
    tools = agent.TOOLS + (agent.GOAL_TOOLS if kid else agent.GROWNUP_TOOLS)
    msgs = past
    while msgs and msgs[0]["role"] != "user": msgs.pop(0)  # the API needs the first turn to come from the user
    content = [{"type": "text", "text": message or "Find this for me."}]
    if image_b64: content.insert(0, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}})
    msgs.append({"role": "user", "content": content if image_b64 else message})
    picks, order, missions, trace = [], None, [], []  # trace: what the agent really did this turn, shown to people as "How Penny decided"
    notes, offered = [], []  # what this turn's tools returned, saved with the reply as Penny's memory
    def done(text):
        remember(pid, message, text, picks, order, notes, offered)
        return text, picks, order, missions
    for _ in range(6):
        try:
            res = ai.messages.create(model=gemini.model_name(), max_tokens=700, system=system, tools=tools, messages=msgs)
        except gemini.APIError as e:
            logging.warning("Penny: the Gemini call failed: %s", str(e)[:400])  # so the Render logs say why (quota, bad model name, blocked request)
            raise HTTPException(502, "Penny is unavailable right now. Try again soon.")
        if res.stop_reason != "tool_use":
            text = "".join(b.text for b in res.content if b.type == "text").strip()
            return done(text or "Anything else I can help with?")
        msgs.append({"role": "assistant", "content": res.content})
        out = []
        for b in res.content:
            if b.type != "tool_use": continue
            try:
                inp = b.input or {}
                if b.name in ("search_products", "search_stores"):
                    term = str(inp["query"]).strip()[:100]
                    if not term: raise ValueError
                    if kid and agent.unsafe_for_kids(term): raise HTTPException(422, "That isn't something kids can shop for. Suggest asking a grown-up.")
                if b.name == "search_products":
                    like = f"%{term}%"
                    result = q("select id,store,name,price from products where name ilike %s or category ilike %s limit 10", (like, like))
                    for x in result: OFFERED[(pid, x["id"])] = x["price"]  # the price the person was shown
                    trace.append(f'Searched partner stores for "{term}": {len(result)} result(s)')
                elif b.name == "search_stores":
                    try: found = web_search(pid, query=term)
                    except HTTPException as e:
                        logging.warning("search_stores fell back to store links: %s", e.detail); found = []
                    if found:
                        picks = found[:3]
                        result = [{k: o[k] for k in ("title", "store", "price", "verdict")} for o in found]
                        trace.append(f'Searched other stores for "{term}": {len(found)} result(s), cheapest ${min(o["price"] for o in found):.2f}')
                    else:
                        picks = store_links(pid, term)
                        result = {"note": "Live prices aren't available right now, so the app is showing links that open each store's own search. No prices are known: say so in one sentence and don't invent any.",
                                  "stores": [x["store"] for x in picks]}
                        trace.append(f'Live search gave nothing for "{term}", so showed {len(picks)} store search links')
                elif b.name == "buy":
                    prod_id = int(inp["product_id"])
                    if (pid, prod_id) not in OFFERED: raise HTTPException(409, "Search for the item first, then confirm it with the person.")
                    shown, now = OFFERED.get((pid, prod_id)), q("select price from products where id=%s", (prod_id,), one=True)
                    if now and isinstance(shown, Decimal) and now["price"] != shown:  # they said yes to one price: never charge another
                        OFFERED[(pid, prod_id)] = now["price"]
                        raise HTTPException(409, f"The price changed from ${shown:.2f} to ${now['price']:.2f}. Tell the person the new price and ask again.")
                    if not agent.confirmed(message): raise HTTPException(409, "The person hasn't said yes yet. Tell them the item, store and price, and ask if they want it.")
                    result = attempt(pid, prod_id, trace=trace + ["You said yes"]); order = result.get("order") or order
                elif b.name == "start_mission":
                    items = [{"query": str(i["query"]).strip()[:60], "qty": max(1, min(int(i.get("qty", 1)), 5))} for i in inp["items"][:8] if str(i.get("query", "")).strip()]
                    budget = D(inp["budget"])
                    if not items or budget <= 0 or budget > 1000: raise HTTPException(422, "A mission needs at least one item and a budget between $1 and $1,000.")
                    if kid and any(agent.unsafe_for_kids(i["query"]) for i in items): raise HTTPException(422, "That isn't something kids can shop for.")
                    mid = q("insert into missions(profile_id,title,budget,items,deadline) values(%s,%s,%s,%s,%s) returning id",
                            (pid, str(inp.get("title") or "Shopping mission").strip()[:80], budget, Jsonb(items), str(inp.get("deadline") or "")[:40] or None), one=True)["id"]
                    missions.append(mid)
                    result = {"mission_id": mid, "note": "Working on it in the background. One approval request for the whole basket will appear in Notifications."}
                elif b.name == "list_orders":
                    result = order_list(pid); trace.append("Looked up recent orders")
                elif b.name == "cancel_order":
                    if not agent.confirmed(message): raise HTTPException(409, "The person hasn't said yes yet. Tell them which item and what happens (a paid order goes back to the wallet), and ask if they want it cancelled.")
                    result = cancel_my_order(pid, int(inp["order_id"]) if inp.get("order_id") is not None else None)
                    order = result.pop("order", None) or order; trace.append(f"Cancelled the order for {result['item']}")
                elif b.name == "list_my_topups" and not kid:
                    result = topup_list(pid)
                    trace.append("Looked up recent PayPal top-ups")
                elif b.name == "request_refund" and not kid:
                    result = refund_request(pid, inp.get("order_id"), inp.get("amount"))
                    trace.append("Filed a refund request for the grown-up to approve")
                elif b.name == "lookup_topup" and not kid:
                    result = topup_lookup(pid, inp.get("order_id"))
                    trace.append(f"Checked a top-up with {result.get('checked_via', 'PayPal')}")
                elif b.name == "set_goal" and kid:
                    item = int(inp["product_id"]) if inp.get("product_id") is not None else None
                    if item is not None and (pid, item) not in OFFERED: raise HTTPException(409, "Search for the item first, then start the goal.")
                    result = create_goal(pid, inp["name"], inp["target"], pid, product_id=item)
                elif b.name == "save_money" and kid:
                    result = deposit(pid, inp["amount"], goal_by_name(pid, inp.get("goal")))
                elif b.name == "move_to_wallet" and kid:
                    result = take_back(pid, inp["amount"], goal_by_name(pid, inp.get("goal")))
                else:
                    result = {"error": "Unknown tool"}
                if not (isinstance(result, dict) and "error" in result):
                    notes += tool_note(b.name, inp, result)
                    if b.name == "search_products": offered += [{"id": x["id"], "price": str(x["price"])} for x in result]
            except HTTPException as e:
                result = {"error": e.detail}
            except (KeyError, TypeError, ValueError, ArithmeticError):
                result = {"error": "Invalid tool input"}
            out.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result, default=str), "is_error": isinstance(result, dict) and "error" in result})
        msgs.append({"role": "user", "content": out})
    return done("I'm still working on that. Tell me a bit more about what you'd like.")

# ---------- Auth: signed, expiring bearer tokens (stdlib only; fails closed without SECRET_KEY) ----------
SECRET = os.environ["SECRET_KEY"].encode()
if len(SECRET) < 16: raise RuntimeError("SECRET_KEY must be a long random string")
def _sign(p): return hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()

def make_token(uid, ttl=86400):
    p = base64.urlsafe_b64encode(json.dumps({"sub": uid, "exp": int(time.time()) + ttl}).encode()).decode()
    return f"{p}.{_sign(p)}"

def current_user(authorization: str = Header(default="")) -> int:
    try:
        p, sig = authorization.removeprefix("Bearer ").split(".")
        if not hmac.compare_digest(sig, _sign(p)): raise ValueError
        d = json.loads(base64.urlsafe_b64decode(p))
        if d["exp"] < time.time(): raise ValueError
        return int(d["sub"])
    except Exception:
        raise HTTPException(401, "Please sign in again.")

def deny(): raise HTTPException(403, "Not allowed.")
def own(uid, pid):
    if uid != pid: deny()
def can(uid, pid):  # the person themselves, or their guardian
    if uid != pid and not q("select 1 from profiles where id=%s and guardian_id=%s", (pid, uid), one=True): deny()
def manage(uid, pid):  # the guardian, or an adult managing their own account; kids can never edit their rules
    if not q("select 1 from profiles where id=%s and (guardian_id=%s or (id=%s and role='adult'))", (pid, uid, uid), one=True): deny()

def hash_pw(pw):
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()}"

def check_pw(pw, stored):
    if not stored: return False
    if not stored.startswith("scrypt$"): return hmac.compare_digest(pw, stored)  # legacy demo seed; upgraded on first login
    _, salt, h = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), h)

COLS = "p.id,p.name,p.role,p.guardian_id,p.balance,p.coaching,p.frozen,p.interests,p.allowance,p.allowance_next"  # never select the password hash
_GOALJ = """json_build_object('id',g.id,'name',g.name,'target',g.target,'saved',g.saved,'bonus_pct',g.bonus_pct,'bonus',round(g.target*g.bonus_pct/100,2),'watch',g.watch_product_id,'watch_name',(select name from products where id=g.watch_product_id),'emoji',(select emoji from products where id=g.watch_product_id),'watch_price',g.watch_price,'buy_below',g.buy_below)
         """
GOAL = f"""(select {_GOALJ} from goals g where g.child_id=p.id and g.status in ('active','paying') order by g.id limit 1) as goal"""  # the oldest open goal
GOALS = f"""coalesce((select json_agg({_GOALJ} order by g.id) from goals g where g.child_id=p.id and g.status in ('active','paying')), '[]'::json) as goals"""

# ---------- API ----------
class Login(BaseModel): name: str; secret: str
class Reg(BaseModel):
    name: str = Field(min_length=1, max_length=40); secret: str = Field(min_length=4, max_length=100); guardian_id: int | None = None
    per_purchase_cap: float | None = Field(default=None, ge=0, le=10000); weekly_budget: float | None = Field(default=None, ge=0, le=10000)
    approval_threshold: float | None = Field(default=None, ge=0, le=10000); allowed_stores: list[str] | None = Field(default=None, max_length=20)
class Buy(BaseModel): profile_id: int; product_id: int
class Turn(BaseModel): role: Literal["user", "assistant"]; content: str = Field(max_length=1000)
class Chat(BaseModel): profile_id: int; message: str = Field(default="", max_length=500); history: list[Turn] = Field(default=[], max_length=10); image_b64: str | None = Field(default=None, max_length=7_000_000)
class Snap(BaseModel): profile_id: int; image_b64: str = Field(max_length=7_000_000)
class Link(BaseModel): profile_id: int; url: str
class Interests(BaseModel): interests: list[str] = Field(max_length=10)
class Rules(BaseModel): per_purchase_cap: float = Field(ge=0, le=10000); weekly_budget: float = Field(ge=0, le=10000); approval_threshold: float = Field(ge=0, le=10000); allowed_stores: list[str] = Field(max_length=20)
class Topup(BaseModel): guardian_id: int; child_id: int; amount: float = Field(gt=0, le=500)
class GoalIn(BaseModel): child_id: int; name: str = Field(min_length=1, max_length=60); target: float = Field(gt=0, le=10000); percent: int = Field(default=0, ge=0, le=100)
class Deposit(BaseModel): profile_id: int; amount: float = Field(gt=0, le=10000); goal_id: int | None = None
class Bonus(BaseModel): percent: int = Field(ge=0, le=100)
class Funds(BaseModel): amount: float = Field(gt=0, le=1000)
class Capture(BaseModel): order_id: str = Field(min_length=3, max_length=64)
class Allowance(BaseModel): amount: float = Field(ge=0, le=500); first_now: bool = False

@app.post("/login")
def login(l: Login):
    throttle(f"login:{l.name.lower()}", limit=10, window=300)
    p = q("select id, secret from profiles where lower(name)=lower(%s)", (l.name,), one=True)
    if not p or not check_pw(l.secret, p["secret"]): raise HTTPException(401, "That name or password didn't match.")
    if not p["secret"].startswith("scrypt$"): q("update profiles set secret=%s where id=%s", (hash_pw(l.secret), p["id"]))
    return {"id": p["id"], "token": make_token(p["id"])}

@app.post("/register")
def register(r: Reg, authorization: str = Header(default="")):
    if r.guardian_id:  # adding a child needs a signed-in adult
        uid = current_user(authorization)
        if uid != r.guardian_id or not q("select 1 from profiles where id=%s and role='adult'", (uid,), one=True): raise HTTPException(403, "Only a guardian can add a child.")
    role = "child" if r.guardian_id else "adult"
    try:
        p = q("insert into profiles(name,role,guardian_id,balance,coaching,secret,interests) values(%s,%s,%s,0,%s,%s,'{}') returning id",
              (r.name, role, r.guardian_id, role == "child", hash_pw(r.secret)), one=True)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "That name is taken.")
    base = (500, 1000, 200) if role == "adult" else (25, 40, 15)  # a guardian can set a child's limits while creating the profile
    caps = [D(v if v is not None else d) for v, d in zip((r.per_purchase_cap, r.weekly_budget, r.approval_threshold), base)]
    stores = r.allowed_stores if r.allowed_stores is not None else ["Pixel Games", "Toy Barn"] + (["Gadget Hub"] if role == "adult" else [])
    q("insert into rules values(%s,%s,%s,%s,%s)", (p["id"], *caps, stores))
    return {"id": p["id"]} if r.guardian_id else {"id": p["id"], "token": make_token(p["id"])}

@app.get("/profiles/{pid}")
def profile(pid: int, uid: int = Depends(current_user)):
    can(uid, pid)
    return {"profile": q(f"select {COLS}, {GOAL}, {GOALS} from profiles p where p.id=%s", (pid,), one=True), "rules": q("select * from rules where profile_id=%s", (pid,), one=True)}

@app.get("/children/{gid}")
def children(gid: int, uid: int = Depends(current_user)):
    own(uid, gid)
    return q(f"select {COLS}, {GOAL}, {GOALS} from profiles p where p.guardian_id=%s order by p.id", (gid,))

@app.get("/products")
def products(uid: int = Depends(current_user)):
    rows = q("select * from products order by id limit 100")
    likes = [x.lower() for x in (q("select interests from profiles where id=%s", (uid,), one=True)["interests"] or [])]
    for r in rows: r["picked"] = r["category"] in likes
    return rows

@app.post("/interests/{pid}")
def set_interests(pid: int, i: Interests, uid: int = Depends(current_user)):
    can(uid, pid); q("update profiles set interests=%s where id=%s", ([x.lower() for x in i.interests], pid)); return {"ok": True}

@app.post("/purchase")
def purchase(b: Buy, uid: int = Depends(current_user)):
    own(uid, b.profile_id); return attempt(b.profile_id, b.product_id)

@app.post("/chat")
def chat(c: Chat, bg: BackgroundTasks, uid: int = Depends(current_user)):
    own(uid, c.profile_id); throttle(uid)
    if not c.message.strip() and not c.image_b64: raise HTTPException(422, "Say something or add a photo.")
    text, picks, order, missions = run_agent(uid, c.message, None, c.image_b64)  # c.history is ignored: Penny's memory is kept here, so the browser can't rewrite it
    for m in missions: bg.add_task(plan_mission, m)  # runs right after the reply is sent
    return {"reply": text, "picks": picks, "order": order, "missions": len(missions)}

@app.get("/chat/history")
def chat_history(uid: int = Depends(current_user)):
    """The last day of this person's chat with Penny, so the screen looks the same after a reload."""
    rows = q("""select role, content, picks, order_id, created_at > now() - interval '2 hours' as fresh from chat_messages
                where profile_id=%s and created_at > now() - interval '24 hours' order by id desc limit 20""", (uid,))[::-1]
    out = []
    for r in rows:
        if r["fresh"]:  # store cards shown recently can be opened again
            for o in r["picks"] or []:
                if isinstance(o, dict) and o.get("url"):
                    with _lock: OFFERS[(uid, o["url"])] = {"title": o.get("title"), "store": o.get("store"), "price": o.get("price")}
        order = q(ORDER_SQL + " where pu.id=%s", (r["order_id"],), one=True) if r["order_id"] else None
        out.append({"who": "me" if r["role"] == "user" else "penny", "text": r["content"], "picks": r["picks"] or [], "order": order})
    return out

@app.post("/chat/clear")
def chat_clear(uid: int = Depends(current_user)):
    q("delete from chat_messages where profile_id=%s", (uid,)); return {"ok": True}

@app.get("/search")
def search(profile_id: int, query: str, uid: int = Depends(current_user)):
    own(uid, profile_id); throttle(uid)
    return web_search(uid, query=query)

@app.post("/snap")
def snap(s: Snap, uid: int = Depends(current_user)):
    own(uid, s.profile_id); throttle(uid, limit=10)
    return web_search(uid, image_b64=s.image_b64)

@app.post("/open-link")
def open_link(l: Link, uid: int = Depends(current_user)):
    own(uid, l.profile_id)
    with _lock: o = OFFERS.get((uid, l.url))
    if not o: raise HTTPException(404, "Search for this item first.")
    if o.get("price") is None:  # a link to a store's own search page: there is no price to check and nothing is bought here
        log(uid, "link", f"Opened {o['store']} to look for {o['title']}")
        return {"status": "open", "url": l.url}
    verdict, why = check_rules(uid, o, partner=False)
    if verdict == "ok":
        log(uid, "link", f"Opened {o['title']} at {o['store']} (${o['price']:.2f})")
        return {"status": "open", "url": l.url}
    steps = "Pick something cheaper, ask your guardian to change your limit, or save it to your wish list."
    notify(uid, "Penny stopped that purchase", f"{o['title']} costs ${o['price']:.2f}. {why}", steps, silent=True)
    log(uid, "refused", f"{o['title']}: {why}")
    return {"status": "refused", "reason": why, "next_steps": steps}

ALERT_SQL = """select a.id, a.title, a.body, a.next_steps, a.created_at, a.needs_action, a.handled_at, a.goal_id, g.status as goal_status, g.bonus_pct as goal_pct, g.target as goal_target,
              coalesce(g.paid_bonus, round(g.target*g.bonus_pct/100,2)) as goal_bonus
              from alerts a left join goals g on g.id=a.goal_id
              where a.profile_id=%s and (a.handled_at is null or a.handled_at > now() - interval '1 day') order by a.id desc limit 30"""  # dealt with = gone a day later

@app.get("/alerts/{pid}")
def alerts(pid: int, uid: int = Depends(current_user)):
    can(uid, pid)
    return q(ALERT_SQL, (pid,))

@app.get("/inbox")
def inbox(uid: int = Depends(current_user)):
    """Everything on the Alerts tab in one call. Things that need a person come first; once dealt with they stay for a day, then leave."""
    adult = q("select role from profiles where id=%s", (uid,), one=True)["role"] == "adult"
    props = q("select id, child_id, kind, title, body, data, trace, created_at, status, decided_at from proposals where profile_id=%s and (status='open' or decided_at > now() - interval '1 day') order by id desc limit 30", (uid,))
    approvals = activity = []
    if adult:
        approvals = q("""select pu.id, pr.name as who, p.name as item, pu.amount, pu.status, pu.decided_at, pu.created_at from purchases pu join profiles pr on pr.id=pu.profile_id join products p on p.id=pu.product_id
                         where pr.guardian_id=%s and (pu.status='pending' or (pu.decided_at > now() - interval '1 day' and (pu.status='declined' or (pu.status='completed' and pu.reason like 'Approved by guardian%%')))) order by pu.id desc limit 30""", (uid,))
        activity = q("""select a.id, a.event, a.detail, a.created_at, pr.name as who from audit a join profiles pr on pr.id=a.profile_id
                        where pr.guardian_id=%s and a.event = any(%s) and a.created_at > now() - interval '1 day' order by a.id desc limit 30""", (uid, list(ACTIVITY)))
    return {"alerts": q(ALERT_SQL, (uid,)), "proposals": props, "approvals": approvals, "activity": activity, "labels": ACTIVITY}

class Seen(BaseModel): upto: int

@app.post("/alerts/seen")
def alerts_seen(s: Seen, uid: int = Depends(current_user)):
    """The person has looked at the plain notes. They leave a day from now. Notes that ask for something wait until it's done."""
    q("update alerts set handled_at=now() where profile_id=%s and id <= %s and handled_at is null and not needs_action", (uid, s.upto))
    return {"ok": True}

# ---------- Push notifications (Web Push: phones and computers) ----------
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")  # only the browsers' own push services: the server never posts to an arbitrary address
class Sub(BaseModel): endpoint: str = Field(max_length=1000); p256dh: str = Field(max_length=200); auth: str = Field(max_length=100)
class Unsub(BaseModel): endpoint: str = Field(max_length=1000)

@app.get("/push/key")
def push_key():
    return {"enabled": push.configured(), "key": push.public_key()}

@app.post("/push/subscribe")
def push_subscribe(s: Sub, uid: int = Depends(current_user)):
    u = urlparse(s.endpoint); host = u.hostname or ""
    if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS): raise HTTPException(422, "That device can't receive notifications.")
    q("""insert into push_subs(profile_id,endpoint,p256dh,auth) values(%s,%s,%s,%s)
         on conflict (endpoint) do update set profile_id=excluded.profile_id, p256dh=excluded.p256dh, auth=excluded.auth""", (uid, s.endpoint, s.p256dh, s.auth))  # a shared device follows whoever signed in last
    q("delete from push_subs where profile_id=%s and id not in (select id from push_subs where profile_id=%s order by id desc limit 10)", (uid, uid))
    return {"ok": True}

@app.post("/push/unsubscribe")
def push_unsubscribe(s: Unsub, uid: int = Depends(current_user)):
    q("delete from push_subs where endpoint=%s and profile_id=%s", (s.endpoint, uid)); return {"ok": True}

@app.post("/push/test")
def push_test(uid: int = Depends(current_user)):
    throttle(f"pushtest:{uid}", limit=5, window=300)
    if not push.configured(): raise HTTPException(503, "Notifications aren't set up on the server yet.")
    deliver(uid, "Notifications are on", "This is how Stash will let you know.", link("Alerts"), tag="test")
    return {"ok": True}

@app.get("/audit/{pid}")
def audit(pid: int, uid: int = Depends(current_user)):
    can(uid, pid); return q("select event, detail, created_at from audit where profile_id=%s order by id desc limit 15", (pid,))

@app.get("/approvals/{gid}")
def approvals(gid: int, uid: int = Depends(current_user)):
    own(uid, gid)
    return q("""select pu.id, pr.name as who, p.name as item, pu.amount from purchases pu join profiles pr on pr.id=pu.profile_id
                join products p on p.id=pu.product_id where pu.status='pending' and pr.guardian_id=%s order by pu.id""", (gid,))

@app.post("/approvals/{pid}/{action}")
def decide(pid: int, action: str, uid: int = Depends(current_user)):
    if action not in ("approve", "decline"): raise HTTPException(422, "Unknown action.")
    # claim atomically so a double tap can never charge twice
    pu = q("""update purchases set status='processing', decided_at=now() where id=%s and status='pending'
              and profile_id in (select id from profiles where guardian_id=%s) returning *""", (pid, uid), one=True)
    if not pu: raise HTTPException(404, "No pending request.")
    prod = q("select * from products where id=%s", (pu["product_id"],), one=True)
    if action == "approve":
        try: settle(pu["profile_id"], prod, "Approved by guardian.", pid)
        except HTTPException:
            q("update purchases set status='pending', decided_at=null where id=%s", (pid,)); raise
        notify(pu["profile_id"], "Approved", f"A grown-up approved {prod['name']}. It's paid and on its way."); log(pu["profile_id"], "approved", prod["name"])
    else:
        q("update purchases set status='declined' where id=%s", (pid,)); notify(pu["profile_id"], "Declined", f"A grown-up declined {prod['name']}. Nothing was charged."); log(pu["profile_id"], "declined", prod["name"])
    return {"ok": True}

@app.post("/rules/{pid}")
def set_rules(pid: int, r: Rules, uid: int = Depends(current_user)):
    manage(uid, pid)
    q("update rules set per_purchase_cap=%s, weekly_budget=%s, approval_threshold=%s, allowed_stores=%s where profile_id=%s",
      (D(r.per_purchase_cap), D(r.weekly_budget), D(r.approval_threshold), r.allowed_stores, pid))
    log(pid, "rules", "Rules updated"); return {"ok": True}

@app.post("/freeze/{pid}")
def freeze(pid: int, uid: int = Depends(current_user)):
    manage(uid, pid); q("update profiles set frozen = not frozen where id=%s", (pid,)); log(pid, "freeze", "Freeze toggled"); return {"ok": True}

@app.post("/topup")
def topup(t: Topup, uid: int = Depends(current_user)):
    if t.guardian_id != uid or not q("select 1 from profiles where id=%s and guardian_id=%s", (t.child_id, uid), one=True): deny()
    if not debit(uid, D(t.amount)): raise HTTPException(409, "Not enough in your balance. Add money first.")
    q("update profiles set balance=balance+%s where id=%s", (D(t.amount), t.child_id)); log(t.child_id, "topup", f"${t.amount:.2f} sent from a grown-up")
    return {"ok": True}


@app.post("/allowance/{pid}")
def set_allowance(pid: int, a: Allowance, uid: int = Depends(current_user)):
    if not q("select 1 from profiles where id=%s and guardian_id=%s", (pid, uid), one=True): deny()
    if a.amount == 0:
        q("update profiles set allowance=0, allowance_next=null, allowance_failed=false where id=%s", (pid,)); resolve(uid, f"allowance:{pid}"); log(pid, "allowance", "Weekly allowance stopped")
    else:
        q("update profiles set allowance=%s, allowance_next=now() + %s::interval, allowance_failed=false where id=%s", (D(a.amount), "0 days" if a.first_now else "7 days", pid))
        log(pid, "allowance", f"Weekly allowance set to ${a.amount:.2f}")
        if a.first_now: pay_allowances()
    return {"ok": True}

@app.post("/goals")
def new_goal(g: GoalIn, uid: int = Depends(current_user)):
    can(uid, g.child_id)  # the child, or their guardian
    if not q("select 1 from profiles where id=%s and role='child'", (g.child_id,), one=True): raise HTTPException(422, "Goals are for kids.")
    if uid != g.child_id:  # a grown-up (can() let them through): a name the child already has only sets that goal's bonus; a new name starts another goal
        same = bonus_on_existing(g.child_id, g.name, g.percent)
        if same: return same
    return create_goal(g.child_id, g.name, g.target, uid, g.percent)

@app.post("/savings")
def save_money(d: Deposit, uid: int = Depends(current_user)):
    own(uid, d.profile_id)  # savings are the child's own choice: nobody else moves this money
    return deposit(uid, d.amount, d.goal_id)

@app.post("/goals/{gid}/bonus")
def edit_bonus(gid: int, b: Bonus, uid: int = Depends(current_user)):
    if not q("update goals set bonus_pct=%s where id=%s and status='active' and child_id in (select id from profiles where guardian_id=%s) returning id", (b.percent, gid, uid), one=True):
        raise HTTPException(404, "That bonus can't be changed any more.")
    return {"ok": True}

@app.post("/savings/withdraw")
def withdraw_savings(d: Deposit, uid: int = Depends(current_user)):
    own(uid, d.profile_id)
    return take_back(uid, d.amount, d.goal_id)

def credit_order(oid, uid=None):
    """Capture an approved PayPal order exactly once and add its money to the guardian's balance. Safe to call twice."""
    row = q("update pay_orders set status='capturing' where order_id=%s and status='created' and (%s::int is null or profile_id=%s) returning profile_id, amount", (oid, uid, uid), one=True)
    if not row:
        done = q("select status from pay_orders where order_id=%s and (%s::int is null or profile_id=%s)", (oid, uid, uid), one=True)
        if done and done["status"] == "captured": return "captured"
        raise HTTPException(404, "That payment isn't waiting to be confirmed.")
    try:
        res = paypal.capture_order(oid, request_id=f"capture-{oid}")
    except paypal.PayPalError:
        q("update pay_orders set status='created' where order_id=%s", (oid,))  # not approved yet, or PayPal is busy: try again
        raise HTTPException(502, "We couldn't confirm the payment yet. If you approved it, try again in a moment.")
    if not res["ok"] or (res["amount"] is not None and res["amount"] != row["amount"]):
        q("update pay_orders set status='failed' where order_id=%s", (oid,))
        raise HTTPException(402, "PayPal didn't complete the payment. Nothing was added.")
    q("update profiles set balance=balance+%s where id=%s", (row["amount"], row["profile_id"]))
    q("update pay_orders set status='captured' where order_id=%s", (oid,))
    log(row["profile_id"], "funds", f"${row['amount']:.2f} added with PayPal")
    sweep_all()  # pay anything that was waiting for funds
    return "captured"

ORDER_ID = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

def topup_list(pid):
    """A grown-up's own recent PayPal top-ups, from our records."""
    rows = q("select order_id, amount, status, created_at from pay_orders where profile_id=%s order by created_at desc limit 5", (pid,))
    return {"topups": [{"order_id": r["order_id"], "amount": r["amount"], "app_status": r["status"], "started": r["created_at"].strftime("%Y-%m-%d"), **refund_facts(r["order_id"])} for r in rows]} if rows \
        else {"topups": [], "note": "No PayPal top-ups yet."}

def topup_lookup(pid, order_id=None):
    """Check one of THIS person's own top-ups with PayPal. The order must be in their pay_orders rows before PayPal is asked,
    because the PayPal account is shared by every family. Asks PayPal's MCP server first, then the PayPal API, then falls back to our own record."""
    oid = str(order_id or "").strip()
    if oid and not ORDER_ID.match(oid): raise HTTPException(422, "That doesn't look like a PayPal order id.")
    cols = "select order_id, amount, status, created_at from pay_orders where profile_id=%s"
    row = q(cols + " and order_id=%s", (pid, oid), one=True) if oid else q(cols + " order by created_at desc limit 1", (pid,), one=True)
    if not row: raise HTTPException(404, "I can't find that top-up on your account." if oid else "You haven't added money with PayPal yet.")
    out = {"order_id": row["order_id"], "amount": row["amount"], "app_status": row["status"], "started": row["created_at"].strftime("%Y-%m-%d"), **refund_facts(row["order_id"])}
    if not paypal.configured():
        out.update(checked_via="demo mode (no PayPal keys, so nothing real to check)", paypal_status="SIMULATED")
        return out
    info, via = None, None
    try:
        info, via = paypal_mcp.get_order(row["order_id"]), "PayPal's MCP server"
    except paypal.PayPalError:
        try:
            info, via = paypal.order_summary(paypal.get_order(row["order_id"])), "the PayPal API"
        except paypal.PayPalError:
            pass
    if not info:
        out.update(checked_via="the app's own records", note="Couldn't reach PayPal just now, so this is only what the app has recorded.")
        return out
    out.update(info, checked_via=via)
    if info["paypal_status"] == "COMPLETED" and row["status"] != "captured":
        out["note"] = "PayPal shows this as completed but the app hasn't added the money yet. Suggest checking again in a minute."
    return out

# ---------- Refunds: unspent top-up money goes back to the PayPal account it came from ----------
# Penny can only FILE a request (a proposal). Nothing moves until the grown-up taps the button, and then plain code does the rest:
# check the rules, take the money out of the balance first, ask PayPal with a retry-safe request id, and put the money back if PayPal says no.
REFUND_DAYS = 180
CENT = Decimal("0.01")

def refund_facts(oid):
    """What has already gone back for one top-up, for the lookup tools. Only non-zero figures are included."""
    rows = {r["status"]: r["t"] for r in q("select status, sum(amount) as t from refunds where order_id=%s group by status", (oid,))}
    out = {}
    if rows.get("refunded"): out["refunded"] = rows["refunded"]
    pend = (rows.get("refunding") or 0) + (rows.get("review") or 0)
    if pend: out["refund_in_progress"] = pend
    return out

def refundable(pid, oid):
    """How much of one of THIS person's top-ups can still go back. Raises HTTPException with a plain reason when none can."""
    row = q("select order_id, amount, status, created_at, created_at < now() - %s::interval as too_old from pay_orders where order_id=%s and profile_id=%s",
            (f"{REFUND_DAYS} days", oid, pid), one=True)
    if not row: raise HTTPException(404, "I can't find that top-up on your account.")
    if row["status"] != "captured": raise HTTPException(409, "That top-up never went through, so there is nothing to send back.")
    if row["too_old"]: raise HTTPException(409, f"PayPal only allows refunds within {REFUND_DAYS} days of the payment.")
    used = q("select coalesce(sum(amount),0) as t from refunds where order_id=%s and status in ('refunding','refunded','review')", (oid,), one=True)["t"]
    left = row["amount"] - used
    if left <= 0: raise HTTPException(409, "That top-up has already been refunded in full.")
    bal = q("select balance from profiles where id=%s", (pid,), one=True)["balance"]
    mx = min(left, bal).quantize(CENT, rounding=ROUND_DOWN)
    if mx < CENT: raise HTTPException(409, "There is no money left in your balance to send back.")
    return {**row, "left": left, "balance": bal, "max": mx}

def capture_info(oid):
    """The payment behind a top-up, as PayPal sees it. Asks PayPal's MCP server first, then the PayPal API. Returns (capture, how we asked)."""
    if not paypal.configured():
        if not oid.startswith("SIM-"): raise HTTPException(409, "PayPal isn't connected right now, so I can't refund a real payment.")  # never pretend to refund real money
        return {"capture_id": "SIMCAP-" + oid[4:], "status": "COMPLETED", "amount": None, "refunded": Decimal(0)}, "demo mode"
    if oid.startswith("SIM-"): raise HTTPException(409, "That top-up was made in demo mode, so there is no real payment to refund.")
    raw, via = None, None
    try:
        raw, via = paypal_mcp.get_order_json(oid), "PayPal's MCP server"
    except paypal.PayPalError:
        try:
            raw, via = paypal.get_order(oid), "the PayPal API"
        except paypal.PayPalError:
            pass
    if not raw: raise HTTPException(502, "I can't reach PayPal just now, so I can't send a refund. Try again in a little while.")
    cap = paypal.order_capture(raw)
    if not cap: raise HTTPException(409, "PayPal doesn't show a completed payment for that top-up.")
    return cap, via

def check_capture(cap, amt):
    if cap["status"] not in ("COMPLETED", "PARTIALLY_REFUNDED"): raise HTTPException(409, "PayPal doesn't show that payment as refundable right now.")
    if cap["amount"] is not None and cap["amount"] - cap["refunded"] < amt:
        raise HTTPException(409, f"PayPal only has ${max(cap['amount'] - cap['refunded'], 0):.2f} left to refund on that payment.")

def refund_amount(raw, mx):
    """The amount asked for, or the most that can go back. Anything odd is refused rather than guessed."""
    if raw in (None, ""): return mx
    try: amt = Decimal(str(raw)).quantize(CENT, rounding=ROUND_DOWN)
    except Exception: raise HTTPException(422, "That isn't an amount I can refund.")
    if not amt.is_finite() or amt < CENT: raise HTTPException(422, "That isn't an amount I can refund.")
    if amt > mx: raise HTTPException(409, f"The most that can go back right now is ${mx:.2f}: it is the smaller of what is left on that top-up and what is in your balance.")
    return amt

def refund_request(pid, order_id=None, amount=None):
    """Penny's tool: file a refund request for the grown-up to approve. Moves no money."""
    oid = str(order_id or "").strip()
    if oid and not ORDER_ID.match(oid): raise HTTPException(422, "That doesn't look like a PayPal order id.")
    if not oid:
        last = q("select order_id from pay_orders where profile_id=%s and status='captured' order by created_at desc limit 1", (pid,), one=True)
        if not last: raise HTTPException(404, "You haven't added money with PayPal yet.")
        oid = last["order_id"]
    r = refundable(pid, oid)
    amt = refund_amount(amount, r["max"])
    cap, via = capture_info(oid)
    check_capture(cap, amt)
    after = r["balance"] - amt
    title = f"Send ${amt:.2f} back to your PayPal?"
    body = (f"Your ${r['amount']:.2f} top-up from {r['created_at']:%b %d} can send ${amt:.2f} back to the PayPal account you paid with. "
            f"It comes out of your Stash balance: ${r['balance']:.2f} now, ${after:.2f} after. It can take a moment to show up in PayPal.")
    trace = ["You asked for money from a top-up to go back to PayPal", f"Checked the payment with {via}" if via else "Checked the payment",
             f"Only unspent money can go back: ${r['max']:.2f} is the most right now", "Nothing is sent until you tap the button"]
    if propose(pid, None, "refund", title, body, {"order_id": oid, "amount": str(amt)}, trace):
        return {"status": "waiting_for_your_approval", "refund_amount": amt, "balance_after": after, "where": "the Notifications tab", "note": "Nothing has been sent yet."}
    old = q("select data from proposals where profile_id=%s and kind='refund' and status='open' and data->>'order_id'=%s", (pid, oid), one=True)
    return {"status": "already_waiting", "refund_amount": D(old["data"]["amount"]) if old else amt, "where": "the Notifications tab",
            "note": "A refund request for this top-up is already waiting. Nothing has been sent yet."}

def refund_applied(r):
    """After PayPal said 'no' to a RETRIED refund, work out whether an earlier try actually went through. True, False (certainly not) or None (can't tell)."""
    try:
        raw = None
        try: raw = paypal_mcp.get_order_json(r["order_id"])
        except paypal.PayPalError: raw = paypal.get_order(r["order_id"])
        mine = f"stash-refund-{r['id']}"
        if any(x["invoice_id"] == mine and x["status"] in ("COMPLETED", "PENDING") for x in paypal.order_refunds(raw)): return True
        cap = paypal.order_capture(raw)
        if not cap: return None
        done = q("select coalesce(sum(amount),0) as t from refunds where order_id=%s and status='refunded' and id<>%s", (r["order_id"], r["id"]), one=True)["t"]
        return False if cap["refunded"] < done + r["amount"] else None  # PayPal's total can't contain this refund yet
    except paypal.PayPalError:
        return None

def fail_refund(rid, why):
    """PayPal said no: give the money back in the app, once. Both steps happen together or not at all."""
    with pool.connection() as c, c.transaction():
        row = c.execute("update refunds set status='failed', reason=%s, done_at=now() where id=%s and status='refunding' returning profile_id, amount", (why, rid)).fetchone()
        if not row: return False
        c.execute("update profiles set balance=balance+%s where id=%s", (row["amount"], row["profile_id"]))
    return True

def settle_refund(rid, res, first):
    """Record what PayPal answered. Never raises. Returns {"refund": "refunded"|"failed"|"processing", "message": ...}."""
    r = q("select * from refunds where id=%s", (rid,), one=True)
    state, why = res["state"], res.get("why")
    if state == "failed" and not first:  # an earlier try may have gone through even though this one was refused
        applied = refund_applied(r)
        if applied: state, res = "refunded", {**res, "ref": None}
        elif applied is None: state = "unknown"
    if state == "refunded":
        if q("update refunds set status='refunded', paypal_ref=%s, done_at=now() where id=%s and status='refunding' returning id", (res.get("ref"), rid), one=True):
            log(r["profile_id"], "funds", f"${r['amount']:.2f} refunded to PayPal")
        return {"refund": "refunded", "message": f"Done. ${r['amount']:.2f} is on its way back to your PayPal."}
    if state == "failed":
        fail_refund(rid, why or "PayPal didn't accept the refund.")
        return {"refund": "failed", "message": f"{why or 'PayPal did not accept the refund.'} Your balance is unchanged."}
    return {"refund": "processing", "message": f"PayPal hasn't confirmed the ${r['amount']:.2f} refund yet. It's safe: I'll keep trying and tell you when it's done."}

def execute_refund(uid, oid, amount):
    """The grown-up tapped the button. Re-check everything (time has passed), take the money out first, then ask PayPal."""
    amt = D(amount)
    r = refundable(uid, oid)
    if amt > r["max"]: raise HTTPException(409, f"Things have changed: the most that can go back now is ${r['max']:.2f}.")
    cap, _ = capture_info(oid)
    check_capture(cap, amt)
    with pool.connection() as c, c.transaction():  # lock the top-up, so two taps at once can't refund more than was paid
        o = c.execute("select amount, status from pay_orders where order_id=%s and profile_id=%s for update", (oid, uid)).fetchone()
        used = c.execute("select coalesce(sum(amount),0) as t from refunds where order_id=%s and status in ('refunding','refunded','review')", (oid,)).fetchone()["t"]
        if not o or o["status"] != "captured" or o["amount"] - used < amt: raise HTTPException(409, "That top-up has already been refunded.")
        if not c.execute("update profiles set balance=balance-%s where id=%s and balance>=%s returning id", (amt, uid, amt)).fetchone():
            raise HTTPException(409, "Your balance no longer covers that.")
        rid = c.execute("insert into refunds(profile_id, order_id, capture_id, amount) values(%s,%s,%s,%s) returning id", (uid, oid, cap["capture_id"], amt)).fetchone()["id"]
    out = settle_refund(rid, paypal.refund_capture(cap["capture_id"], amt, "Refund of your Stash top-up", f"stash-refund-{rid}", f"stash-refund-{rid}"), first=True)
    if out["refund"] == "failed": raise HTTPException(422, out["message"])  # the proposal reopens, and nothing was taken
    return out

def sweep_refunds():
    """Finish refunds PayPal never answered: ask again with the same request id (PayPal never refunds twice for it). After a day, hand it to a person."""
    n = 0
    for r in q("update refunds set tried_at=now(), attempts=attempts+1 where status='refunding' and tried_at < now() - interval '2 minutes' returning *, created_at < now() - interval '1 day' as stale"):
        n += 1
        if r["stale"]:
            if q("update refunds set status='review', reason='PayPal never confirmed it' where id=%s and status='refunding' returning id", (r["id"],), one=True):
                notify(r["profile_id"], "A refund needs a check", f"PayPal hasn't confirmed your ${r['amount']:.2f} refund. Your balance was reduced, so please check your PayPal activity.",
                       "If the refund isn't in your PayPal activity, contact support and mention this top-up.")
            continue
        out = settle_refund(r["id"], paypal.refund_capture(r["capture_id"], r["amount"], "Refund of your Stash top-up", f"stash-refund-{r['id']}", f"stash-refund-{r['id']}"), first=False)
        if out["refund"] == "refunded": notify(r["profile_id"], "Refund sent", f"${r['amount']:.2f} is on its way back to your PayPal.")
        elif out["refund"] == "failed": notify(r["profile_id"], "Refund didn't go through", out["message"])
    return n

@app.post("/funds/create")
def funds_create(f: Funds, uid: int = Depends(current_user)):
    if q("select role from profiles where id=%s", (uid,), one=True)["role"] != "adult": deny()
    try:
        oid, url = paypal.create_order(D(f.amount), "Add money to Stash", f"{APP_URL}/?paypal=return", f"{APP_URL}/?paypal=cancel", request_id=uuid.uuid4().hex)
    except paypal.PayPalError:
        raise HTTPException(502, "PayPal isn't available right now. Nothing was charged.")
    q("insert into pay_orders(order_id, profile_id, amount) values(%s,%s,%s)", (oid, uid, D(f.amount)))
    return {"order_id": oid, "approve_url": url}

@app.post("/funds/capture")
def funds_capture(c: Capture, uid: int = Depends(current_user)):
    return {"ok": True, "status": credit_order(c.order_id, uid)}

@app.post("/webhooks/paypal")  # PayPal calls this when a buyer approves, so the money still arrives if they close the tab
async def paypal_webhook(request: Request):
    event = await request.json()
    ok = await asyncio.to_thread(paypal.verify_webhook, {k.lower(): v for k, v in request.headers.items()}, event)
    if not ok: raise HTTPException(400, "Bad signature")
    if event.get("event_type") == "CHECKOUT.ORDER.APPROVED":
        try: await asyncio.to_thread(credit_order, (event.get("resource") or {}).get("id"))
        except HTTPException: pass  # unknown or already handled: acknowledge anyway
    return {"ok": True}

@app.post("/orders/{oid}/cancel")
def cancel_order_route(oid: int, uid: int = Depends(current_user)):
    """The Cancel button on an order card: the person who bought it, or their guardian."""
    throttle(f"cancel:{uid}", limit=10, window=300)
    pu = q("select profile_id from purchases where id=%s", (oid,), one=True)
    if not pu: raise HTTPException(404, "I can't find that order.")
    can(uid, pu["profile_id"])
    return cancel_my_order(uid, oid, owner=pu["profile_id"])

@app.get("/orders/{pid}")
def orders(pid: int, uid: int = Depends(current_user)):
    can(uid, pid)
    return q(ORDER_SQL + " where pu.profile_id=%s and pu.status in ('completed','pending','processing','declined','cancelled') order by pu.id desc limit 10", (pid,))

@app.get("/health")
def health():
    return {"ok": True, "paypal": paypal.configured(), "paypal_env": os.getenv("PAYPAL_ENV", "sandbox"), "ai": gemini.has_key(),
            "product_search": bool(os.getenv("CHANNEL3_API_KEY")), "merchant_payouts": bool(MERCHANT_EMAIL), "push": push.configured(), "webhook": bool(os.getenv("PAYPAL_WEBHOOK_ID")), "paypal_mcp": paypal_mcp.available()}

@app.post("/goals/{gid}/cancel")
def cancel_goal(gid: int, uid: int = Depends(current_user)):
    with plock(uid):  # only the child can cancel; the saved money goes back to the wallet, with no bonus
        g = q("update goals set status='cancelled' where id=%s and child_id=%s and status='active' returning *", (gid, uid), one=True)
        if not g: raise HTTPException(404, "No goal to cancel.")
        q("update profiles set balance=balance+%s where id=%s", (g["saved"], uid))
    log(uid, "goal", f"Cancelled {g['name']}; ${g['saved']:.2f} back in the wallet")
    return {"ok": True}

@app.post("/internal/settle-all")  # cron: pays any full goal whose bonus charge failed earlier
def settle_all(x_cron_key: str = Header(default="")):
    if not os.getenv("CRON_KEY") or not hmac.compare_digest(x_cron_key, os.environ["CRON_KEY"]): raise HTTPException(403, "Forbidden")
    return {"paid": sweep_all()}

def cron_ok(key): return bool(os.getenv("CRON_KEY")) and hmac.compare_digest(key, os.environ["CRON_KEY"])

class Watch(BaseModel): buy_below: float | None = Field(default=None, gt=0, le=10000)
class PartnerEvent(BaseModel): purchase_id: int; event: Literal["cancelled", "delayed"]
class PriceSet(BaseModel): product_id: int; price: float = Field(gt=0, le=10000)

@app.get("/proposals")
def my_proposals(uid: int = Depends(current_user)):
    return q("select id, child_id, kind, title, body, data, trace, created_at from proposals where profile_id=%s and status='open' order by id desc limit 20", (uid,))

@app.post("/proposals/{pid}/{action}")
def decide_proposal(pid: int, action: str, uid: int = Depends(current_user)):
    if action not in ("accept", "decline"): raise HTTPException(422, "Unknown action.")
    pr = q("update proposals set status=%s, decided_at=now() where id=%s and profile_id=%s and status='open' returning *", ("accepted" if action == "accept" else "declined", pid, uid), one=True)
    if not pr: raise HTTPException(404, "That suggestion isn't waiting for you.")
    if action == "decline":
        if pr["kind"] == "mission": q("update missions set status='done' where id=%s", (pr["data"]["mission_id"],))
        return {"ok": True}
    try:
        return {"ok": True, **execute_proposal(pr, uid)}
    except HTTPException:
        q("update proposals set status='open', decided_at=null where id=%s", (pid,)); raise

@app.post("/goals/{gid}/watch")
def set_watch(gid: int, w: Watch, uid: int = Depends(current_user)):
    if not q("update goals set buy_below=%s where id=%s and status='active' and watch_product_id is not null and child_id in (select id from profiles where guardian_id=%s) returning id",
             (D(w.buy_below) if w.buy_below else None, gid, uid), one=True):
        raise HTTPException(404, "There's no item being watched for this goal.")
    return {"ok": True}

@app.post("/internal/partner-event")  # what a partner store's webhook would call (secured with the cron key in this MVP)
def partner_event(e: PartnerEvent, x_cron_key: str = Header(default="")):
    if not cron_ok(x_cron_key): raise HTTPException(403, "Forbidden")
    return {"proposal": handle_partner_event(e.purchase_id, e.event)}

@app.post("/internal/price")  # a partner price feed would call this
def set_price(p: PriceSet, x_cron_key: str = Header(default="")):
    if not cron_ok(x_cron_key): raise HTTPException(403, "Forbidden")
    q("update products set price=%s where id=%s", (D(p.price), p.product_id))
    return {"watches_checked": check_watches()}
