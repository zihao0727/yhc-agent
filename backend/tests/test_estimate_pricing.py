from copy import deepcopy

import pytest
from sqlalchemy import select

from app.models import PriceItem
from app.quote_engine import build_quote
from test_agent_loop import prepared, provider, call, start, seed
from test_requirements import save_lines


def plan():
    return {
        "assumptions": [{"field": "quantity", "value": "2", "reason": "按门店两处暂定2件，待审核"}],
        "costs": [
            {"label": "材料", "rate": "300", "quantity_formula": "=area_m2",
             "source": "agent_estimate", "reason": "测试估算板材费，非供应商确认价"},
            {"label": "加工组装及利润", "rate": "50", "quantity_formula": "=1",
             "source": "agent_estimate", "reason": "按简单门牌暂估每件制作及利润"},
            {"label": "运输", "rate": "20", "quantity_formula": "=1", "basis": "total",
             "source": "agent_estimate", "reason": "市内整单运输暂估"},
        ],
        "scope": "含材料、制作、组装、利润和运输，不含税及现场安装；有效期7天，全部暂估待审核",
    }


def estimated_job(client, monkeypatch):
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [call("complete_estimate", line_id="line1", estimate=plan()),
                           call("finish_quote")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded", result
    return result


def test_full_estimate_review_modify_reprice_and_approve(client, monkeypatch):
    job = estimated_job(client, monkeypatch)
    quote = job["quotes"][0]
    payload = quote["payload"]
    assert quote["status"] == "draft" and not quote["outdated"]
    assert payload["total"] == "141.60" and payload["has_estimates"]
    assert job["requirements"][0]["quantity"] is None
    assert payload["lines"][0]["effective_requirement"]["quantity"] == 2
    assert payload["lines"][0]["requirement"]["quantity"] is None
    assert len(payload["review_items"]) == 5
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409
    approval = {"note": "核实估算参数和费用", "confirmed_commercial_terms": True,
                "terms": "按审核方案制作，未含税及安装，有效期7天"}
    assert client.post(f'/api/quotes/{quote["id"]}/approve', json=approval).status_code == 422
    # Revise the assumption and the actual dimensions, without calling the model.
    job["requirements"][0]["estimate"]["assumptions"][0]["value"] = "3"
    job["requirements"][0]["width_mm"] = "600"
    job = save_lines(client, job, job["requirements"])
    response = client.post(f'/api/jobs/{job["id"]}/quotes', json={
        "revision": job["revision"], "terms": approval["terms"]})
    assert response.status_code == 201, response.text
    revised = response.json()
    assert revised["payload"]["total"] == "234.80"
    assert revised["version"] == quote["version"] + 1
    assert client.post(f'/api/quotes/{quote["id"]}/approve', json={
        **approval, "confirmed_estimates": True}).status_code == 409
    assert client.post(f'/api/quotes/{revised["id"]}/approve', json={
        **approval, "confirmed_estimates": True}).status_code == 200
    exported = client.get(f'/api/quotes/{revised["id"]}/export')
    assert exported.status_code == 200
    text = exported.content.decode("utf-8-sig")
    assert "已审核的补全与估价清单" in text and "234.80" in text
    assert "600 x 120,3," in text


def test_explicit_facts_and_manual_prices_take_precedence(client, monkeypatch):
    job = prepared(client)
    job["requirements"][0].update(manual_unit_price="100", manual_price_note="人工确认制作价")
    job = save_lines(client, job, job["requirements"])
    p = plan()
    p["assumptions"][0]["value"] = "9"
    provider(monkeypatch, [call("complete_estimate", line_id="line1", estimate=p), call("finish_quote")])
    result = start(client, job).json()["quotes"][0]["payload"]
    assert result["total"] == "200.00"
    assert result["lines"][0]["effective_requirement"]["quantity"] == 2
    assert result["review_items"][0]["applied"] is False
    assert result["lines"][0]["manual_price"]


@pytest.mark.parametrize("formula", ["=quantity*2", "=__import__('os')", "=1/0", "=-1", "=unknown"])
def test_invalid_cost_formula_cannot_create_total(test_db, formula):
    p = plan()
    p["costs"][0]["quantity_formula"] = formula
    with test_db() as db:
        payload = build_quote(db, [{"id": "x", "product": "牌", "width_mm": "300", "height_mm": "120",
                                   "estimate": p}], automatic=True)
    assert payload["total"] is None and payload["lines"][0]["blockers"]


def test_catalog_provenance_and_revision_are_revalidated(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    p = plan()
    p["costs"][0].update(source="catalog", reference_id=price["id"], reference_revision=price["revision"])
    job = prepared(client)
    provider(monkeypatch, [call("complete_estimate", line_id="line1", estimate=p),
                           call("search_prices"), call("complete_estimate", line_id="line1", estimate=p),
                           call("finish_quote")])
    result = start(client, job).json()
    assert "必须先查询" in result["agent_runs"][0]["steps"][0]["result"]["error"]
    quote = result["quotes"][0]
    assert not quote["outdated"]
    with test_db.begin() as db:
        price_row = db.scalar(select(PriceItem).where(PriceItem.id == price["id"]))
        price_row.revision += 1
    refreshed = client.get(f'/api/jobs/{job["id"]}').json()["quotes"][0]
    assert refreshed["outdated"]
    bad_plan = deepcopy(p)
    bad_plan["costs"][0]["rate"] = "1"
    with test_db() as db:
        payload = build_quote(db, [{**result["requirements"][0], "estimate": bad_plan}], automatic=True)
    assert payload["total"] is None


def test_dependent_profit_and_tax_recalculate_after_rate_edit(test_db):
    p = plan()
    p["costs"] = [
        {"label": "制作", "rate": "100", "reason": "暂估基础费用"},
        {"label": "利润", "rate": ".25", "quantity_formula": "=unit_subtotal", "reason": "暂估成本加成"},
        {"label": "运费", "rate": "20", "basis": "total", "reason": "暂估整单运费"},
        {"label": "税费", "rate": ".13", "quantity_formula": "=order_subtotal",
         "basis": "total", "reason": "测试暂定口径，待审核"},
    ]
    raw = {"id": "x", "product": "牌", "estimate": p}
    with test_db() as db:
        first = build_quote(db, [raw], automatic=True)
        assert first["total"] == "305.10"
        p["costs"][0]["rate"] = "200"
        second = build_quote(db, [raw], automatic=True)
        assert second["total"] == "587.60"
