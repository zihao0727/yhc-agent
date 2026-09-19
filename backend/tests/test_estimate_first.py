import json

import pytest

from app import agent_loop
from test_agent_loop import call, prepared, provider, seed, start
from test_estimate_pricing import plan
from test_requirements import save_lines


def test_model_quotes_missing_information_without_catalog_match(client, monkeypatch):
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [
        call("complete_estimate", line_id="line1", estimate=plan()),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["result"]["saved"]
    assert len(result["quotes"]) == 1
    quote = result["quotes"][0]
    assert quote["payload"]["total"] == "141.60"
    assert quote["payload"]["review_items"]
    assert quote["status"] == "draft"
    assert result["requirements"][0]["quantity"] is None
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409


def test_large_order_completes_all_lines_with_review_list(client, monkeypatch):
    job = prepared(client)
    lines = []
    for i in range(29):
        lines.append({**job["requirements"][0], "id": f"line{i}", "quantity": None,
                      "evidence": [{"file_id": job["files"][0]["id"], "page": 1,
                                    "text": "source " * 200}],
                      "dimension_evidence": [], "components": []})
    job = save_lines(client, job, lines)
    seen = provider(monkeypatch, [
        *[call("complete_estimate", line_id=line["id"], estimate=plan()) for line in lines],
        call("finish_quote"),
    ])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    payload = result["quotes"][0]["payload"]
    assert payload["priced_count"] == 29 and payload["pending_count"] == 0
    assert payload["total"] == "4106.40"
    assert len(payload["review_items"]) == 29 * 5
    assert all(line["quantity"] is None for line in result["requirements"])
    assert run["usage"]["context_compactions"] < agent_loop.MAX_SEGMENTS
    assert max(len(json.dumps(messages, ensure_ascii=False)) for messages in seen) < 60000
    # The audit keeps full plans; the model sees concise saved-state receipts.
    assert run["steps"][0]["result"]["estimate_costs"]
    checkpoint = json.loads(seen[1][1]["content"])["checkpoint"]
    assert "estimate_costs" not in checkpoint[0]["result"]
    assert "estimate" not in checkpoint[0]["arguments"]


def test_model_contract_has_no_business_defaults_or_pause_classification():
    assert set(agent_loop.AskInput.model_fields) == {"question"}
    assert set(agent_loop.PendingInput.model_fields) == {"reason"}
    assert "0.25" not in agent_loop.SYSTEM and "0.13" not in agent_loop.SYSTEM
    assert "暂按1件" not in agent_loop.SYSTEM
    tools = {tool["function"]["name"] for tool in agent_loop.TOOLS}
    assert "complete_estimate" in tools and "read_requirement" in tools
    assert "select_price" not in tools


def test_model_can_read_full_saved_plan_after_compaction(client, monkeypatch):
    job = prepared(client)
    provider(monkeypatch, [
        call("complete_estimate", line_id="line1", estimate=plan()),
        call("read_requirement", line_id="line1"),
        call("read_requirement", line_id="missing"),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    steps = result["agent_runs"][0]["steps"]
    assert steps[1]["result"]["requirement"]["estimate"]["costs"] == result["requirements"][0]["estimate"]["costs"]
    assert steps[1]["result"]["requirement"]["evidence"]
    assert steps[2]["result"]["error"] == "需求不存在"


@pytest.mark.parametrize("quantity,rate,expected", [(7, "700", "176.40"), (3, "510", "55.08")])
def test_model_chooses_candidate_and_quantity_without_match_policy(
        client, price_input, monkeypatch, quantity, rate, expected):
    seed(client, price_input, material="钢", process="参考工艺", amount="400")
    chosen = seed(client, price_input, material="钢", process="参考工艺", amount=rate)
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    job = save_lines(client, job, job["requirements"])
    proposal = {
        "assumptions": [{"field": "quantity", "value": str(quantity),
                         "reason": "模型按布置语境提出暂定数量，供人工审核"}],
        "costs": [{"label": "成品方案", "rate": rate, "quantity_formula": "=area_m2",
                   "source": "catalog", "reference_id": chosen["id"],
                   "reference_revision": chosen["revision"],
                   "reason": "模型选择参考制作方案；原材料和工艺不同，适用性待审核"}],
        "scope": "模型暂估制作价；材质工艺差异、数量及税运安装范围待人工审核",
    }
    provider(monkeypatch, [
        call("search_prices"),
        call("complete_estimate", line_id="line1", estimate=proposal),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    payload = result["quotes"][0]["payload"]
    assert payload["total"] == expected
    assert payload["lines"][0]["effective_requirement"]["quantity"] == quantity
    assert payload["review_items"] and payload["requires_review"]
    assert result["requirements"][0]["quantity"] is None


def test_component_reference_can_be_used_without_redundant_search(client, price_input, monkeypatch):
    price = seed(client, price_input, amount="300")
    job = prepared(client)
    monkeypatch.setattr(agent_loop, "analyze_components", lambda *args: {
        "components": [{"references": [{"id": price["id"], "revision": price["revision"]}]}]})
    estimate = plan()
    estimate["costs"][0].update(source="catalog", reference_id=price["id"],
                                reference_revision=price["revision"])
    provider(monkeypatch, [
        call("analyze_components", line_id="line1"),
        call("complete_estimate", line_id="line1", estimate=estimate),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    assert result["quotes"][0]["payload"]["total"] == "141.60"
