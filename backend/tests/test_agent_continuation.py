import json

import pytest

from app import agent_loop, deepseek
from app.requirements_schemas import ExtractionResult
from test_agent_loop import call, prepared, provider, seed, selection, start
from test_requirements import create_job


@pytest.mark.parametrize("budget", ["tokens", "rounds", "tools", "context", "time"])
def test_automatic_continuation_preserves_selection_and_history(client, price_input, monkeypatch, budget):
    price = seed(client, price_input)
    job = prepared(client)
    if budget == "tokens":
        monkeypatch.setattr(agent_loop, "MAX_TOKENS", 10)
    elif budget == "rounds":
        monkeypatch.setattr(agent_loop, "MAX_ROUNDS", 1)
    elif budget == "tools":
        monkeypatch.setattr(agent_loop, "MAX_TOOLS", 1)
    elif budget == "context":
        monkeypatch.setattr(agent_loop, "MAX_CONTEXT_CHARS", 1)
    else:
        monkeypatch.setattr(agent_loop, "MAX_SECONDS", 0)
    seen = provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote")])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert result["quotes"][0]["payload"]["total"] == "21.60"
    assert run["usage"]["context_compactions"] >= 1
    assert run["usage"]["agent_tokens"] == 30
    checkpoint = json.loads(seen[1][1]["content"])["checkpoint"]
    assert checkpoint[0]["tool"] == "search_prices"
    assert checkpoint[0]["result"]["prices"][0]["id"] == price["id"]
    # A fresh segment contains no dangling tool-call or tool-result messages.
    assert [m["role"] for m in seen[1]] == ["system", "user"]
    assert json.loads(seen[-1][1]["content"])["requirements"][0]["selected_price_id"] == price["id"]


def test_large_extraction_does_not_use_agent_budget(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = create_job(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key")
    monkeypatch.setattr(deepseek, "extract", lambda files, *args: (
        ExtractionResult.model_validate({"lines": [{
            "product": "测试产品", "material": "铝", "thickness_mm": 3, "width_mm": 300,
            "height_mm": 120, "quantity": 2, "language": "en",
            "evidence": [{"file_id": files[0].id, "page": 1, "text": "2件"}]}]}),
        1, {"prompt_tokens": 150000, "completion_tokens": 10, "total_tokens": 150010}))
    turns = []

    def turn(messages, tools):
        turns.append(1)
        line = json.loads(messages[1]["content"])["requirements"][0]
        sequence = [call("search_prices"), call("select_price", line_id=line["id"],
                    price_id=price["id"], revision=price["revision"], reason="已核对"),
                    call("finish_quote")]
        return sequence[len(turns) - 1], {"total_tokens": 10}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded"
    assert run["usage"]["total_tokens"] == 150040
    assert run["usage"]["extraction_tokens"] == 150010
    assert run["usage"]["agent_tokens"] == 30
    assert run["usage"]["context_compactions"] == 0


def test_search_process_notes_terms_and_compact_results(client, price_input, monkeypatch):
    price = seed(client, price_input, process="静电喷塑", notes="电镀 LED " + "业务备注" * 500)
    job = prepared(client)
    seen = provider(monkeypatch, [
        call("search_prices", query="铝 静电喷塑"), call("search_prices", query="led"),
        call("ask_user", question="请确认工艺")])
    result = start(client, job).json()
    steps = result["agent_runs"][0]["steps"]
    assert steps[0]["result"]["total"] == steps[1]["result"]["total"] == 1
    audit_price = steps[0]["result"]["prices"][0]
    assert len(audit_price["notes"]) > 1000
    model_price = json.loads(seen[1][-1]["content"])["prices"][0]
    assert "notes" not in model_price and "attributes" not in model_price
    assert model_price["has_conditions"] and model_price["id"] == price["id"]


def test_total_guard_still_stops_without_infinite_billing(client, monkeypatch):
    job = prepared(client)
    monkeypatch.setattr(agent_loop, "MAX_ROUNDS", 1)
    monkeypatch.setattr(agent_loop, "MAX_SEGMENTS", 2)
    provider(monkeypatch, [call("search_prices", query="a"), call("search_prices", query="b")])
    run = start(client, job).json()["agent_runs"][0]
    assert run["status"] == "waiting"
    assert run["usage"]["context_compactions"] == 1


def test_multi_call_batch_finished_before_compaction(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = prepared(client)
    monkeypatch.setattr(agent_loop, "MAX_TOOLS", 1)
    batch = call("search_prices", query="missing")
    second = call("search_prices", query="测试")["tool_calls"][0]
    second["id"] = "second"
    batch["tool_calls"].append(second)
    seen = provider(monkeypatch, [batch, selection(price), call("finish_quote")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    checkpoint = json.loads(seen[1][1]["content"])["checkpoint"]
    assert len(checkpoint) == 2
    assert checkpoint[0]["result"]["total"] == 0
    assert checkpoint[1]["result"]["total"] == 1


def test_failed_standard_matches_do_not_prevent_model_estimate(client, price_input, monkeypatch):
    from test_estimate_pricing import plan
    prices = [seed(client, price_input, process=f"未确认工艺{i}") for i in range(12)]
    job = prepared(client)
    provider(monkeypatch, [call("search_prices"),
                           *[selection(price, "evaluate_price") for price in prices],
                           call("complete_estimate", line_id="line1", estimate=plan()),
                           call("finish_quote")])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded"
    assert result["quotes"][0]["status"] == "draft"
    assert result["quotes"][0]["payload"]["total"] == "141.60"
    assert result["quotes"][0]["payload"]["review_items"]


def test_compacted_checkpoint_is_bounded(client, monkeypatch):
    from types import SimpleNamespace
    job = SimpleNamespace(requirements=[], brief="", messages=[], extraction={})
    steps = [{"tool": "evaluate_price", "arguments": {"line_id": "same", "price_id": i},
              "result": {"line_id": "same", "blockers": ["规则未确认"] * 20}} for i in range(500)]
    messages = agent_loop.context_messages(job, SimpleNamespace(steps=steps))
    data = json.loads(messages[1]["content"])
    assert len(data["checkpoint"]) == 1
    assert data["checkpoint"][0]["arguments"]["price_id"] == 499
    assert len(messages[1]["content"]) < 12000
