"""rules.checks lays out the same rules evaluate() applies. Pure: no database. Run with: pytest --noconftest test_checks.py"""
from decimal import Decimal as D
import pytest
from rules import evaluate, checks

R = {"per_purchase_cap": D(25), "weekly_budget": D(40), "approval_threshold": D(15), "allowed_stores": ["Toy Barn"]}
CASES = [(False, D(45), D(0), D(9.99), "Toy Barn"), (False, D(45), D(12), D(18.5), "Toy Barn"), (False, D(45), D(0), D(30), "Toy Barn"),
         (False, D(45), D(35), D(10), "Toy Barn"), (False, D(5), D(0), D(9), "Toy Barn"), (True, D(45), D(0), D(5), "Toy Barn"), (False, D(45), D(0), D(5), "Elsewhere")]

@pytest.mark.parametrize("frozen,bal,spent,price,store", CASES)
def test_checklist_agrees_with_the_decision(frozen, bal, spent, price, store):
    verdict, _ = evaluate(frozen, bal, R, spent, price, store)
    levels = [c["level"] for c in checks(frozen, bal, R, spent, price, store)]
    assert ("bad" in levels) == (verdict == "blocked")
    assert (verdict == "needs_approval") == ("bad" not in levels and "warn" in levels)
    assert (verdict == "ok") == (set(levels) == {"ok"})

def test_the_example_reads_well():
    texts = [c["text"] for c in checks(False, D(45), R, D(12), D("18.50"), "Toy Barn")]
    assert "Toy Barn is an allowed store" in texts and "$30.50 of $40.00 weekly budget" in texts and "Over $15.00, so a grown-up must approve" in texts

def test_other_stores_skip_the_allowed_store_line():
    assert not any("allowed store" in c["text"] for c in checks(False, D(45), R, D(0), D(5), "shop.example", partner=False))
