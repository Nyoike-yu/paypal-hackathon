import os
import main

class R:
    status_code = 200
    def __init__(self, data): self._d = data
    def json(self): return self._d
    text = ""

def login(client):
    r = client.post("/login", json={"name": "Sam", "secret": "demo"}).json()
    return {"Authorization": f"Bearer {r['token']}"}

def run(monkeypatch, data):
    monkeypatch.setenv("CHANNEL3_API_KEY", "k")
    monkeypatch.setattr(main.httpx, "post", lambda *a, **k: R(data))
    return main.web_search(1, query="mug")

OFFER = {"domain": "shop.example", "url": "https://shop.example/mug", "availability": "InStock"}

def test_every_price_shape_channel3_documents_is_read(client, monkeypatch):
    for price in ({"amount": 12.5, "currency": "USD"}, {"price": 12.5, "currency": "USD"}, 12.5):
        out = run(monkeypatch, {"products": [{"title": "Mug", "offers": [{**OFFER, "price": price}]}]})
        assert len(out) == 1 and out[0]["price"] == 12.5 and out[0]["store"] == "shop.example"

def test_merchant_domain_and_a_bare_list_also_work(client, monkeypatch):
    offer = {"merchant_domain": "shop.example", "url": "https://shop.example/mug", "price": 9, "availability": "InStock"}
    assert len(run(monkeypatch, [{"title": "Mug", "offers": [offer]}])) == 1

def test_unreadable_products_give_an_empty_list_not_a_crash(client, monkeypatch):
    assert run(monkeypatch, {"products": [{"title": "Mug", "offers": [{"url": "x", "price": "free"}]}]}) == []

def test_store_links_are_five_safe_links_that_open(client):
    links = main.store_links(1, "white mug & heart")
    assert [l["store"] for l in links] == ["Jumia", "Kilimall", "Amazon", "Best Buy", "eBay"]
    assert all(l["url"].startswith("https://") and " " not in l["url"] and "&heart" not in l["url"] for l in links)
    h = login(client)
    r = client.post("/open-link", headers=h, json={"profile_id": 1, "url": links[0]["url"]})
    assert r.status_code == 200 and r.json() == {"status": "open", "url": links[0]["url"]}
