import pytest


def test_auth_required(client):
    for path in ("/api/prices", "/api/rules", "/api/documents", "/api/audit", "/api/stats"):
        response = client.get(path, headers={"X-Admin-Token": "wrong"})
        assert response.status_code == 401
    assert client.get("/api/health").status_code == 200


def test_price_crud_and_history(client, price_input):
    created = client.post("/api/prices", json=price_input)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["amount"] == "1.234567"
    assert item["revision"] == 1
    assert item["source"] is None
    assert client.get("/api/prices?q=测试产品").json()["total"] == 1
    update = {**price_input, "amount": "20.500001", "revision": 1, "review_reason": "确认新价格", "status": "active"}
    changed = client.put(f'/api/prices/{item["id"]}', json=update)
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert client.put(f'/api/prices/{item["id"]}', json=update).status_code == 409
    deleted = client.request("DELETE", f'/api/prices/{item["id"]}',
                             json={"revision": 2, "reason": "不再使用"})
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "inactive"
    history = client.get(f'/api/prices/{item["id"]}/history').json()
    assert len(history) == 3
    assert history[-1]["snapshot"]["amount"] == "1.234567"
    assert client.get("/api/audit").json()["total"] == 3
    assert client.get("/api/prices?status=active").json()["total"] == 0
    assert client.get("/api/prices?status=inactive").json()["total"] == 1


@pytest.mark.parametrize("change", [
    {"amount": "-1"}, {"amount": "NaN"}, {"amount": "Infinity"},
    {"amount": "1.1234567"}, {"thickness_mm": "0"}, {"review_reason": " "},
    {"product": ""}, {"unit": "invalid"}, {"unknown_key": "x"},
])
def test_invalid_price_rejected(client, price_input, change):
    assert client.post("/api/prices", json={**price_input, **change}).status_code == 422
    assert client.get("/api/stats").json()["prices"] == 0


@pytest.mark.parametrize("change", [
    {"amount": None}, {"unit": "unknown"}, {"language": "unknown"},
    {"price_kind": "starting"}, {"price_kind": "reference"}, {"product": "产品待确认"},
])
def test_activation_preserves_unverified_pricing_facts(client, price_input, change):
    payload = {**price_input, "status": "active", **change}
    response = client.post("/api/prices", json=payload)
    assert response.status_code == 201
    assert response.json()["status"] == "active"
    for key, value in change.items():
        assert response.json()[key] == value


def test_zero_and_null_distinguished(client, price_input):
    assert client.post("/api/prices", json={**price_input, "amount": "0", "status": "active"}).status_code == 201
    item = client.post("/api/prices", json={**price_input, "amount": None}).json()
    assert item["amount"] is None


def test_literal_search_and_pagination(client, price_input):
    for name in ("含%符号", "含_符号", "普通产品"):
        client.post("/api/prices", json={**price_input, "product": name})
    assert client.get("/api/prices?q=%25").json()["total"] == 1
    assert client.get("/api/prices?q=_").json()["total"] == 1
    page = client.get("/api/prices?page_size=2&page=2").json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get("/api/prices?page=0").status_code == 422
    assert client.get("/api/prices?page_size=999").status_code == 422


def test_rules_crud_and_prohibit_execution(client):
    payload = {"name": "防水加价", "content": "原文 +0.2元/cm", "rule_type": "surcharge",
               "status": "draft", "reason": "测试新增"}
    created = client.post("/api/rules", json=payload)
    assert created.status_code == 201
    item = created.json()
    assert client.put(f'/api/rules/{item["id"]}', json={**payload, "revision": 1, "status": "active"}).status_code == 422
    update = {**payload, "revision": 1, "content": "修订原文"}
    assert client.put(f'/api/rules/{item["id"]}', json=update).status_code == 200
    assert client.put(f'/api/rules/{item["id"]}', json=update).status_code == 409
    assert client.request("DELETE", f'/api/rules/{item["id"]}',
                          json={"revision": 2, "reason": "已失效"}).status_code == 200
    assert client.get("/api/rules?status=inactive").json()["total"] == 1
    logs = client.get("/api/audit").json()["items"]
    assert logs[0]["before"]["content"] == "修订原文"


def test_rule_missing_product(client):
    payload = {"name": "限制", "content": "原文", "product_id": 99999, "reason": "测试"}
    assert client.post("/api/rules", json=payload).status_code == 422


def test_missing_price(client, price_input):
    assert client.get("/api/prices/999").status_code == 404
    assert client.put("/api/prices/999", json=price_input).status_code == 404
    assert client.get("/api/sources/999").status_code == 404
