import agent

def kid(**o): return {"name": "Maya", "role": "child", "balance": 45, "interests": ["art"], **o}
RULES = {"per_purchase_cap": 25, "weekly_budget": 40, "approval_threshold": 15, "allowed_stores": ["Toy Barn"]}
GOAL = {"name": "Soccer ball", "saved": 15, "target": 60, "bonus_pct": 25}

def test_prompt_carries_the_context_and_every_safety_section():
    s = agent.build_system(kid(), RULES, GOAL)
    for part in ["Maya", "$45.00", "Soccer ball", "$25.00 per purchase", "<principles>", "<kid_rules>", "<examples>", "Confirm before spending", "Untrusted text", "Privacy", "weapons"]:
        assert part in s, part
    assert "<grownup_rules>" not in s

def test_grownup_prompt_has_no_kid_rules_and_no_goal_line():
    s = agent.build_system({"name": "Sam", "role": "adult", "balance": 120, "interests": []}, RULES, None)
    assert "<grownup_rules>" in s and "<kid_rules>" not in s and "Savings goal" not in s

def test_tools_are_scoped():
    assert {t["name"] for t in agent.TOOLS} == {"search_products", "search_stores", "buy", "start_mission"}
    assert {t["name"] for t in agent.GOAL_TOOLS} == {"set_goal", "save_money", "move_to_wallet"}

def test_kid_search_filter():
    assert all(agent.unsafe_for_kids(x) for x in ["vape pens", "a toy GUN", "beer", "Casino games", "NSFW"])
    assert not any(agent.unsafe_for_kids(x) for x in ["soccer ball", "art kit", "Lego set", "sweater", "gunmetal paint"])  # whole words only

def test_confirmation_needs_a_yes():
    assert all(agent.confirmed(x) for x in ["yes", "Yes, buy the art kit", "ok go ahead", "sure!"])
    assert not any(agent.confirmed(x) for x in ["how much is it?", "buy the art kit", "maybe later", ""])
