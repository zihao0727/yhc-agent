from app import deepseek
from app.models import PricingRule
from app.quote_engine import build_quote
from test_agent_loop import seed, prepared, provider, call, selection, start
from test_requirements import save_lines


def test_partial_quote_then_manual_supplement_preserves_priced_lines(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input, notes="原价不含安装")
    job = prepared(client)
    first = job["requirements"][0]
    first["uncertainties"] = ["颜色待客户复核"]
    second = {**first, "id": "line2", "product": "特殊组合牌", "quantity": 3}
    job = save_lines(client, job, [first, second])
    with test_db.begin() as db:
        db.add(PricingRule(product_id=price["product_id"], name="安装另计", content="待核实安装"))
    provider(monkeypatch, [call("search_prices"), selection(price),
                           call("ask_user", question="特殊组合牌请人工补充单价")])
    result = start(client, job).json()
    payload = result["quotes"][0]["payload"]
    assert payload["known_subtotal"] == "21.60"
    assert payload["total"] is None
    assert payload["priced_count"] == 1 and payload["pending_count"] == 1
    assert payload["lines"][0]["warnings"] and not payload["lines"][0]["blockers"]
    assert payload["lines"][1]["amount"] is None
    selected = result["requirements"][0].copy()
    result["requirements"][1].update(manual_unit_price="50.00", manual_price_note="人工核定制作价，不含安装")
    result = save_lines(client, result, result["requirements"])
    monkeypatch.setattr(deepseek, "extract", lambda *args: (_ for _ in ()).throw(AssertionError("Must not re-extract")))
    provider(monkeypatch, [call("finish_quote")])
    result = start(client, result).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    assert result["requirements"][0] == selected
    assert result["quotes"][0]["payload"]["total"] == "171.60"
    assert result["quotes"][0]["payload"]["lines"][1]["manual_price"]
    assert result["quotes"][1]["payload"]["total"] is None


def test_manual_price_requires_quantity_and_explanation(client, monkeypatch):
    job = prepared(client)
    job["requirements"][0].update(manual_unit_price="100", manual_price_note="", quantity=None)
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [call("finish_pending_quote", reason="补充计量信息")])
    result = start(client, job).json()
    line = result["quotes"][0]["payload"]["lines"][0]
    assert line["amount"] is None
    assert "单件数量" in line["blockers"]
    assert "人工补价需填写依据及包含范围" in line["blockers"]


def test_missing_quantity_delivers_unit_without_total(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = prepared(client)
    job["requirements"][0].update(quantity=None, uncertainties=["项目名称待复核"])
    job["requirements"][0]["extras"] = [
        {"label": "加工", "amount": "2", "basis": "per_piece", "reason": "已核实每件加工"},
        {"label": "运输", "amount": "50", "basis": "total", "reason": "已核实整单运输"},
    ]
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [call("search_prices"), selection(price),
                           call("finish_pending_quote", reason="请补充数量")])
    result = start(client, job).json()
    payload = result["quotes"][0]["payload"]
    line = payload["lines"][0]
    assert result["requirements"][0]["selected_price_id"] == price["id"]
    assert result["requirements"][0]["quantity"] is None
    assert line["unit_price"] == "12.80" and line["fixed_charges"] == "50"
    assert line["amount"] is None and payload["total"] is None
    assert payload["known_subtotal"] == "0"
    assert payload["priced_count"] == 0 and payload["unit_priced_count"] == 1
    assert [q["question"] for q in payload["confirmation_items"]] == ["单件数量"]
    assert "项目名称待复核" in line["estimate_conditions"]
    assert client.post(f'/api/quotes/{result["quotes"][0]["id"]}/approve', json={
        "note": "测试不能批准单价清单", "confirmed_commercial_terms": True}).status_code == 409
    result["requirements"][0]["quantity"] = 3
    result = save_lines(client, result, result["requirements"])
    provider(monkeypatch, [call("finish_quote")])
    payload = start(client, result).json()["quotes"][0]["payload"]
    assert payload["total"] == "88.40"


def test_quantity_unknown_still_rejects_ambiguous_rates(client, price_input, monkeypatch):
    price = seed(client, price_input)
    seed(client, price_input, amount="320")
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [call("search_prices"), selection(price),
                           call("finish_pending_quote", reason="管理员确认适用价格")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["steps"][1]["result"]["unit_price"] is None
    assert result["requirements"][0]["selected_price_id"] is None
    assert result["quotes"][0]["payload"]["lines"][0]["unit_price"] is None


def test_manual_unit_and_grouped_questions_without_quantity(test_db):
    with test_db() as db:
        payload = build_quote(db, [
            {"id": key, "product": key, "manual_unit_price": "25",
             "manual_price_note": "人工核定成品制作价", "quantity": None}
            for key in ("first", "second")
        ], automatic=True)
    assert all(line["unit_price"] == "25.00" and line["amount"] is None for line in payload["lines"])
    assert len(payload["confirmation_items"]) == 1
    assert len(payload["confirmation_items"][0]["lines"]) == 2
