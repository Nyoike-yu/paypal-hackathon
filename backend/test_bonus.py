def login(client, name):
    r = client.post("/login", json={"name": name, "secret": "demo"}).json()
    return {"Authorization": f"Bearer {r['token']}"}

def goals(client, h):
    return client.get("/profiles/2", headers=h).json()["profile"]["goals"]

def test_same_name_updates_bonus_only(client):
    sam = login(client, "Sam")
    before = goals(client, sam)
    assert len(before) == 1
    r = client.post("/goals", headers=sam, json={"child_id": 2, "name": "  soccer   BALL ", "target": 999, "percent": 20})
    assert r.status_code == 200 and r.json()["updated"] is True
    after = goals(client, sam)
    assert len(after) == 1 and after[0]["id"] == before[0]["id"]
    assert float(after[0]["target"]) == float(before[0]["target"]) and float(after[0]["saved"]) == float(before[0]["saved"])
    assert after[0]["bonus_pct"] == 20

def test_new_name_starts_another_goal_as_many_as_wanted(client):
    sam = login(client, "Sam")
    for name, pct in (("Bike", 10), ("Guitar", 0), ("Skates", 50)):
        r = client.post("/goals", headers=sam, json={"child_id": 2, "name": name, "target": 100, "percent": pct})
        assert r.status_code == 200 and not r.json().get("updated")
    gs = goals(client, sam)
    assert [g["name"] for g in gs] == ["Soccer ball", "Bike", "Guitar", "Skates"]
    assert [g["bonus_pct"] for g in gs] == [20, 10, 0, 50]
    assert client.get("/profiles/2", headers=sam).json()["profile"]["goal"]["name"] == "Soccer ball"  # `goal` is still the oldest one

def test_child_cannot_duplicate_a_name_or_set_a_bonus(client):
    maya, sam = login(client, "Maya"), login(client, "Sam")
    assert client.post("/goals", headers=maya, json={"child_id": 2, "name": "bike", "target": 5, "percent": 90}).status_code == 409
    assert [g["bonus_pct"] for g in goals(client, sam)] == [20, 10, 0, 50]
    r = client.post("/goals", headers=maya, json={"child_id": 2, "name": "Kite", "target": 20, "percent": 90})
    assert r.status_code == 200
    assert goals(client, sam)[-1]["bonus_pct"] == 0  # kids never add a bonus

def test_saving_needs_a_goal_when_there_are_several(client):
    maya, sam = login(client, "Maya"), login(client, "Sam")
    r = client.post("/savings", headers=maya, json={"profile_id": 2, "amount": 5})
    assert r.status_code == 409 and "Which goal" in r.json()["detail"]
    bike = next(g for g in goals(client, sam) if g["name"] == "Bike")
    assert client.post("/savings", headers=maya, json={"profile_id": 2, "amount": 5, "goal_id": bike["id"]}).status_code == 200
    now = {g["name"]: g["saved"] for g in goals(client, sam)}
    assert now["Bike"] == 5 and now["Soccer ball"] == 15
    assert client.post("/savings/withdraw", headers=maya, json={"profile_id": 2, "amount": 5, "goal_id": bike["id"]}).status_code == 200
    assert {g["name"]: g["saved"] for g in goals(client, sam)}["Bike"] == 0

def test_cannot_save_into_someone_elses_goal(client):
    sam = login(client, "Sam")
    other = client.post("/register", headers=sam, json={"name": "Kid2", "secret": "pin1234", "guardian_id": 1, "per_purchase_cap": 25, "weekly_budget": 40, "approval_threshold": 15, "allowed_stores": ["Toy Barn"]})
    assert other.status_code == 200, other.text
    kid2 = login_as(client, "Kid2", "pin1234")
    mine = goals(client, sam)[0]
    assert client.post("/savings", headers=kid2, json={"profile_id": 2, "amount": 1, "goal_id": mine["id"]}).status_code in (403, 404)

def login_as(client, name, secret):
    r = client.post("/login", json={"name": name, "secret": secret}).json()
    return {"Authorization": f"Bearer {r['token']}"}
