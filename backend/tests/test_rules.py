import sys, pathlib
from decimal import Decimal as D
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from rules import evaluate
R = {"per_purchase_cap": D(25), "weekly_budget": D(40), "approval_threshold": D(15), "allowed_stores": ["Pixel Games"]}
def ev(**k):
    a = dict(frozen=False, balance=D(45), rules=R, spent=D(0), price=D(10), store="Pixel Games"); a.update(k)
    return evaluate(**a)[0]
def test_ok(): assert ev() == "ok"
def test_frozen(): assert ev(frozen=True) == "blocked"
def test_store_blocked(): assert ev(store="Other") == "blocked"
def test_web_ignores_store_list(): assert ev(store="Other", partner=False) == "ok"
def test_balance(): assert ev(price=D(50)) == "blocked"
def test_cap(): assert ev(price=D(30)) == "blocked"
def test_cap_boundary_allowed(): assert ev(price=D(25)) == "needs_approval"
def test_weekly(): assert ev(spent=D(35), price=D(10)) == "blocked"
def test_approval(): assert ev(price=D(20)) == "needs_approval"
def test_penny_exact(): assert ev(price=D("15.00")) == "ok"
