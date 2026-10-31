"""Pure rules engine: no I/O, so it is fast and trivially testable. All money is Decimal."""
def evaluate(frozen, balance, rules, spent, price, store, partner=True):
    if frozen: return "blocked", "The agent is frozen by a guardian."
    if partner and store not in rules["allowed_stores"]: return "blocked", f"{store} isn't on the allowed stores list."
    if price > balance: return "blocked", f"The balance is ${balance:.2f} and this costs ${price:.2f}."
    if price > rules["per_purchase_cap"]: return "blocked", f"The limit for one purchase is ${rules['per_purchase_cap']:.2f}."
    if spent + price > rules["weekly_budget"]: return "blocked", f"This would pass the ${rules['weekly_budget']:.2f} weekly budget (${spent:.2f} already spent)."
    if price > rules["approval_threshold"]: return "needs_approval", f"Purchases over ${rules['approval_threshold']:.2f} need a guardian's OK."
    return "ok", "Within all the rules."

def checks(frozen, balance, rules, spent, price, store, partner=True):
    """The same rules as evaluate(), laid out as a checklist people can read: [{"level": ok|warn|bad, "text"}].
    evaluate() still makes the decision; this only explains it, so the two can never disagree about what is allowed."""
    c = lambda level, text: {"level": level, "text": text}
    out = [c("bad", "Spending is paused by a grown-up") if frozen else c("ok", "Spending isn't paused")]
    if partner: out.append(c("ok", f"{store} is an allowed store") if store in rules["allowed_stores"] else c("bad", f"{store} isn't an allowed store"))
    out.append(c("ok", f"Wallet has ${balance:.2f}") if price <= balance else c("bad", f"Wallet has ${balance:.2f}, this costs ${price:.2f}"))
    out.append(c("ok", f"Under the ${rules['per_purchase_cap']:.2f} limit per buy") if price <= rules["per_purchase_cap"] else c("bad", f"Over the ${rules['per_purchase_cap']:.2f} limit per buy"))
    out.append(c("ok", f"${spent + price:.2f} of ${rules['weekly_budget']:.2f} weekly budget") if spent + price <= rules["weekly_budget"] else c("bad", f"Would pass the ${rules['weekly_budget']:.2f} weekly budget (${spent:.2f} used)"))
    out.append(c("ok", f"Under ${rules['approval_threshold']:.2f}, no approval needed") if price <= rules["approval_threshold"] else c("warn", f"Over ${rules['approval_threshold']:.2f}, so a grown-up must approve"))
    return out
