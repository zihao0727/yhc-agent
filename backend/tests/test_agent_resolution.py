from app import agent_loop
from test_agent_loop import prepared, provider, call, start


def test_search_count_does_not_end_model_loop(client, monkeypatch):
    job = prepared(client)
    monkeypatch.setattr(agent_loop, "MAX_ROUNDS", 2)
    monkeypatch.setattr(agent_loop, "MAX_SEGMENTS", 20)
    provider(monkeypatch, [
        *[call("search_prices", query=f"different-{i}") for i in range(20)],
        call("finish_pending_quote", reason="模型决定保存未完成清单")])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "waiting"
    assert len([s for s in run["steps"] if s["tool"] == "search_prices"]) == 20
    assert run["steps"][-1]["tool"] == "finish_pending_quote"
    quote = result["quotes"][0]
    assert quote["status"] == "blocked"
    assert len(quote["payload"]["lines"]) == len(job["requirements"])
    assert quote["payload"]["total"] is None
    assert quote["payload"]["lines"][0]["amount"] is None
    assert not quote["outdated"]
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409


def test_model_can_finish_with_pending_quote(client, monkeypatch):
    job = prepared(client)
    provider(monkeypatch, [call("finish_pending_quote", reason="该组合成品缺少适用单价及工艺规则")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "waiting"
    assert result["quotes"][0]["payload"]["pending_reason"] == "该组合成品缺少适用单价及工艺规则"


def test_component_tool_returns_missing_basis_and_pending_retains_analysis(client, monkeypatch):
    job = prepared(client)
    provider(monkeypatch, [
        call("analyze_components", line_id="missing"),
        call("analyze_components", line_id="line1"),
        call("finish_pending_quote", reason="缺少部件明细及组合成品计价依据"),
    ])
    result = start(client, job).json()
    steps = result["agent_runs"][0]["steps"]
    assert steps[0]["result"]["error"] == "需求不存在"
    assert steps[1]["result"]["missing"]
    line = result["quotes"][0]["payload"]["lines"][0]
    assert line["component_analysis"]["missing"]
    assert line["amount"] is None


def test_all_tools_remain_available_after_repeated_searches(client, monkeypatch):
    from app import deepseek
    job = prepared(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only")
    count = 0

    def turn(messages, tools):
        nonlocal count
        count += 1
        assert tools == agent_loop.TOOLS
        if count <= 14:
            return call("search_prices", query=f"query-{count}"), {}
        assert "search_prices" in [t["function"]["name"] for t in tools]
        return call("finish_pending_quote", reason="现有报价库缺少对应的成品单价"), {}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    assert start(client, job).json()["quotes"][0]["status"] == "blocked"
