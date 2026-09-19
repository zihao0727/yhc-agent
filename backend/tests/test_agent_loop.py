import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import agent_loop, deepseek
from app.models import AgentRun, PriceItem, PricingRule, RequirementJob, now
from app.requirements_schemas import ExtractionResult
from test_requirements import create_job, line_input, save_lines


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call-" + name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)}}]}


def provider(monkeypatch, sequence):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key-not-real")
    iterator = iter(sequence)
    seen = []

    def turn(messages, tools):
        seen.append(json.loads(json.dumps(messages)))
        return next(iterator), {"total_tokens": 10}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    return seen


def seed(client, price_input, **overrides):
    data = {**price_input, "spec": "", "quality": "", "notes": "", "process": "",
            "unit": "m2", "amount": "300", "status": "active", **overrides}
    response = client.post("/api/prices", json=data)
    assert response.status_code == 201, response.text
    return response.json()


def prepared(client):
    job = create_job(client)
    line = line_input()
    line["confirmed"] = False
    line["evidence"] = [{"file_id": job["files"][0]["id"], "page": 1, "text": "300x120mm 2件 铝3mm"}]
    return save_lines(client, job, [line])


def start(client, job, **kwargs):
    return client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": job["revision"], "allow_external_processing": True, **kwargs})


def selection(price, name="select_price"):
    args = {"line_id": "line1", "price_id": price["id"], "revision": price["revision"]}
    if name == "select_price":
        args["reason"] = "客户标注与报价库一致"
    return call(name, **args)


def test_multi_search_to_quote_without_human_selection(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = prepared(client)
    seen = provider(monkeypatch, [
        call("search_prices", query="没有这个产品"), call("search_prices", query="测试"),
        selection(price, "evaluate_price"), selection(price), call("calculate_quote"), call("finish_quote"),
    ])
    response = start(client, job)
    assert response.status_code == 200, response.text
    result = response.json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["result"]["total"] == 0
    assert run["steps"][1]["result"]["total"] == 1
    assert any(m["role"] == "tool" for m in seen[1])
    assert run["usage"]["total_tokens"] == 60
    assert result["requirements"][0]["confirmed"] is False
    quote = result["quotes"][0]
    assert quote["payload"]["total"] == "21.60"
    assert quote["payload"]["generated_by"] == "agent"
    assert quote["status"] == "draft" and not quote["outdated"]
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409
    approved = client.post(f'/api/quotes/{quote["id"]}/approve', json={
        "note": "已审核所有计价依据及商业条款", "confirmed_commercial_terms": True,
        "terms": "含税，不含运输安装，有效期7天"})
    assert approved.status_code == 200, approved.text


@pytest.mark.parametrize("override,expected", [
    ({"status": "draft"}, "尚未启用"), ({"spec": "10-20cm"}, "规格档位"),
    ({"process": "烤漆"}, "工艺"), ({"price_kind": "starting", "status": "draft"}, "起价"),
    ({"material": "钢"}, "材质"), ({"unit": "cm"}, "计价长度"),
])
def test_ineligible_price_never_selected(client, price_input, monkeypatch, override, expected):
    price = seed(client, price_input, **override)
    job = prepared(client)
    provider(monkeypatch, [call("search_prices", query="", status="all"), selection(price),
                           call("finish_quote"), call("ask_user", question="请管理员核实价格条件")])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "waiting", run
    assert expected in str(run["steps"][1]["result"]["blockers"])
    assert not result["requirements"][0]["selected_price_id"]
    assert result["quotes"][0]["payload"]["lines"][0]["amount"] is None
    assert result["quotes"][0]["status"] == "blocked"


def test_rules_and_multiple_prices_block_automatic_quote(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    seed(client, price_input, amount="320")
    job = prepared(client)
    provider(monkeypatch, [call("search_prices"), selection(price),
                           call("ask_user", question="请确认报价策略")])
    result = start(client, job).json()
    assert "多个" in str(result["agent_runs"][0]["steps"][1]["result"]["blockers"])
    with test_db.begin() as db:
        db.add(PricingRule(product_id=price["product_id"], name="附加费", content="每件加5元"))
    provider(monkeypatch, [call("search_prices"), selection(price, "evaluate_price"),
                           call("ask_user", question="请核实附加费用规则")])
    result = start(client, result).json()
    assert "相关规则" in str(result["agent_runs"][0]["steps"][1]["result"]["warnings"])
    assert "相关规则" not in str(result["agent_runs"][0]["steps"][1]["result"]["blockers"])


def test_missing_quantity_cannot_finish(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    job = save_lines(client, job, job["requirements"])
    provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote"),
                           call("ask_user", question="请补充制作数量")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "waiting"
    assert result["quotes"][0]["payload"]["lines"][0]["amount"] is None


def test_permissions_key_and_unknown_tools(client, monkeypatch):
    job = prepared(client)
    assert start(client, job).status_code == 503
    provider(monkeypatch, [call("execute_sql", sql="DROP TABLE price_items"),
                           call("search_prices", unexpected="bad"),
                           call("ask_user", question="请管理员启用价格")])
    assert start(client, job, allow_external_processing=False).status_code == 422
    result = start(client, job).json()
    assert "error" in result["agent_runs"][0]["steps"][0]["result"]
    assert "error" in result["agent_runs"][0]["steps"][1]["result"]
    assert result["agent_runs"][0]["status"] == "waiting"


def test_loop_budget_and_repeated_calls(client, monkeypatch):
    job = prepared(client)
    provider(monkeypatch, [*[call("search_prices")] * 5,
                           call("finish_pending_quote", reason="由模型决定暂存进度")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "waiting"
    assert len(result["agent_runs"][0]["steps"]) == 6
    monkeypatch.setattr(agent_loop, "MAX_ROUNDS", 2)
    monkeypatch.setattr(agent_loop, "MAX_SEGMENTS", 1)
    provider(monkeypatch, [{"role": "assistant", "content": "已报价100元"}] * 2)
    result = start(client, result).json()
    assert result["agent_runs"][0]["status"] == "waiting"
    assert result["quotes"][0]["status"] == "blocked"


def test_unseen_and_stale_price_rejected(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    job = prepared(client)
    provider(monkeypatch, [selection(price), call("search_prices"), selection({**price, "revision": 99}),
                           call("ask_user", question="价格版本变化请检查")])
    result = start(client, job).json()
    assert "error" in result["agent_runs"][0]["steps"][0]["result"]
    assert "error" in result["agent_runs"][0]["steps"][2]["result"]
    provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote")])
    result = start(client, result).json()
    quote = result["quotes"][0]
    with test_db.begin() as db:
        db.get(PriceItem, price["id"]).amount = 400
    response = client.post(f'/api/quotes/{quote["id"]}/approve', json={
        "note": "尝试审核旧版报价", "confirmed_commercial_terms": True})
    assert response.status_code == 409


def test_stop_during_provider_and_concurrent_start(client, monkeypatch):
    job = prepared(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key-not-real")

    def turn(messages, tools):
        current = client.get(f'/api/jobs/{job["id"]}').json()
        assert start(client, current).status_code == 409
        assert client.put(f'/api/jobs/{job["id"]}/requirements', json={
            "revision": current["revision"], "lines": [], "reason": "并发修改"}).status_code == 409
        stopped = client.post(f'/api/jobs/{job["id"]}/agent/stop', json={
            "revision": current["revision"], "reason": "测试停止"})
        assert stopped.status_code == 200
        return call("finish_quote"), {}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "cancelled"
    assert not result["quotes"]


def test_extract_and_resume_with_answer(client, price_input, monkeypatch):
    price = seed(client, price_input)
    job = create_job(client)

    def extract(files, brief, previous, supplementary):
        assert supplementary == "制作2件"
        return ExtractionResult.model_validate({"lines": [{
            "product": "测试产品", "material": "铝", "thickness_mm": 3, "width_mm": 300,
            "height_mm": 120, "quantity": 2, "language": "en",
            "evidence": [{"file_id": files[0].id, "page": 1, "text": "铝3mm 300x120"}],
        }]}), 1, {"total_tokens": 20}
    monkeypatch.setattr(deepseek, "extract", extract)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key-not-real")
    steps = []

    def turn(messages, tools):
        if not steps:
            steps.append(1)
            return call("search_prices"), {}
        if len(steps) == 1:
            steps.append(2)
            initial = json.loads(messages[1]["content"])
            return call("select_price", line_id=initial["requirements"][0]["id"],
                        price_id=price["id"], revision=price["revision"], reason="全部字段一致"), {}
        return call("finish_quote"), {}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    result = start(client, job, supplementary_text="制作2件").json()
    assert result["agent_runs"][0]["status"] == "succeeded", result
    assert result["agent_runs"][0]["steps"][0]["tool"] == "extract_requirements"
    # A supplementary answer requires explicit replacement consent.
    assert start(client, result, supplementary_text="改成3件").status_code == 409


def test_recovery_respects_agent_deadline(client, test_db):
    job = prepared(client)
    with test_db.begin() as db:
        record = db.get(RequirementJob, job["id"])
        record.status = "processing"
        run = AgentRun(job_id=record.id, model="test")
        db.add(run)
    job = client.get(f'/api/jobs/{job["id"]}').json()
    payload = {"revision": job["revision"], "reason": "恢复中断任务"}
    assert client.post(f'/api/jobs/{job["id"]}/recover', json=payload).status_code == 409
    with test_db.begin() as db:
        db.scalar(select(AgentRun)).created_at = now() - timedelta(hours=1)
    result = client.post(f'/api/jobs/{job["id"]}/recover', json=payload)
    assert result.status_code == 200
    assert result.json()["agent_runs"][0]["status"] == "failed"


def test_tool_provider_wire_format(monkeypatch):
    monkeypatch.setattr(deepseek, "get_key", lambda: "not-real-test-key")
    real_client = httpx.Client

    def transport(request):
        body = json.loads(request.content)
        assert body["tools"] == agent_loop.TOOLS
        assert body["thinking"]["type"] == "disabled"
        assert body["messages"][-1]["role"] == "tool"
        return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls",
                                                     "message": call("calculate_quote")}], "usage": {"total_tokens": 9}})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(transport), **kwargs))
    result, usage = deepseek.agent_turn([
        call("search_prices"), {"role": "tool", "tool_call_id": "call-search_prices", "content": "{}"}
    ], agent_loop.TOOLS)
    assert result["tool_calls"][0]["function"]["name"] == "calculate_quote"
    assert usage["total_tokens"] == 9


def test_search_pagination_and_draft_visibility(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input, status="draft")
    with test_db.begin() as db:
        for i in range(22):
            db.add(PriceItem(product_id=price["product_id"], material="铝", amount=i + 1,
                             status="draft", unit="piece"))
    job = prepared(client)
    provider(monkeypatch, [call("search_prices"), call("search_prices", status="draft"),
                           call("search_prices", status="draft", offset=20),
                           call("ask_user", question="查到23条草稿价格，请管理员审核启用")])
    result = start(client, job).json()
    steps = result["agent_runs"][0]["steps"]
    assert steps[0]["result"]["total"] == 0
    assert steps[1]["result"]["total"] == 23 and steps[1]["result"]["next_offset"] == 20
    assert len(steps[1]["result"]["prices"]) == 20
    assert len(steps[2]["result"]["prices"]) == 3 and steps[2]["result"]["next_offset"] is None


def test_wait_then_resume_preserves_requirements(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input, status="draft")
    job = prepared(client)
    provider(monkeypatch, [call("search_prices", status="draft"),
                           call("ask_user", question="请管理员启用适用单价")])
    waiting = start(client, job).json()
    with test_db.begin() as db:
        record = db.get(PriceItem, price["id"])
        record.status = "active"
        db.flush()
        price["revision"] = record.revision
    provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote")])
    result = start(client, waiting).json()
    assert [r["status"] for r in result["agent_runs"]] == ["succeeded", "waiting"]
    assert result["requirements"][0]["id"] == "line1"
    assert result["quotes"][0]["payload"]["total"] == "21.60"


def test_global_extraction_questions_are_visible_without_blocking_estimate(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    job = prepared(client)
    with test_db.begin() as db:
        db.get(RequirementJob, job["id"]).extraction = {"questions": ["第二页是否还有其他产品？"]}
    job = client.get(f'/api/jobs/{job["id"]}').json()
    provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote"),
                           call("ask_user", question="第二页是否还有其他产品？")])
    result = start(client, job).json()
    quote = result["quotes"][0]
    assert quote["payload"]["total"] == "21.60"
    assert quote["payload"]["requires_review"]
    assert quote["payload"]["review_questions"] == ["第二页是否还有其他产品？"]
    assert result["agent_runs"][0]["steps"][2]["result"]["complete"]


def test_provider_failure_retains_requirements(client, monkeypatch):
    job = prepared(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only-key")

    def fail(*args):
        raise deepseek.ModelFailure("测试模拟网络异常")
    monkeypatch.setattr(deepseek, "agent_turn", fail)
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "failed"
    assert result["requirements"] == job["requirements"]
    assert not result["quotes"]


def test_new_rule_invalidates_agent_quote(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    job = prepared(client)
    provider(monkeypatch, [call("search_prices"), selection(price), call("finish_quote")])
    result = start(client, job).json()
    with test_db.begin() as db:
        db.add(PricingRule(product_id=price["product_id"], name="新增费用", content="测试规则"))
    result = client.get(f'/api/jobs/{job["id"]}').json()
    assert result["quotes"][0]["outdated"]
