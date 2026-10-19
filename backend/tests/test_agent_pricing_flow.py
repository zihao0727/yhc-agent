import json
from copy import deepcopy
from types import SimpleNamespace

from app import agent_loop, agent_work_state as work, deepseek
from app.agent_context import context_messages
from app.agent_execution import commit_preview, execute_tool
from app.agent_tools import CompleteEstimateInput, SearchInput, TOOLS, tools_for_stage
from app.config import settings
from app.models import AgentRun, PriceItem, RequirementJob
from app.requirements_schemas import RequirementLine
from test_agent_loop import call, provider, seed, start
from test_agent_routing import fake_model_key, run_workers  # noqa: F401
from test_estimate_pricing import plan
from test_pipeline_v3 import line_v3
from test_redis_agent import enqueue, redis_worker  # noqa: F401


def test_order_fee_questions_stay_visible_during_item_pricing(client, monkeypatch, redis_worker, test_db):
    job = enqueue(client, monkeypatch, 1)
    with test_db() as db:
        current = db.get(RequirementJob, job["id"])
        run = db.get(AgentRun, job["agent_runs"][0]["id"])
        current.extraction = {"questions": ["其他项目尺寸不明确", "A125安装费用待确认，不得默认免费"]}
        payload = json.loads(context_messages(current, run, db)[1]["content"])
        assert payload["extraction_questions"] == ["A125安装费用待确认，不得默认免费"]


def test_successful_preview_is_saved_even_when_that_turn_exhausts_budget(
        client, monkeypatch, redis_worker, test_db):
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 1000)
    job = enqueue(client, monkeypatch, 2)
    sequence = iter([call("preview_estimate", line_id="line0", estimate=plan()),
                     call("preview_estimate", line_id="line1", estimate=plan()),
                     call("finish_quote")])
    seen = []

    def turn(messages, tools):
        seen.append(deepcopy(messages))
        return next(sequence), {"total_tokens": 1200}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["quotes"][0]["payload"]["total"] == "283.20"
    saves = [s for s in current["agent_runs"][0]["steps"] if s["tool"] == "complete_estimate"]
    assert len(saves) == 2 and all(s["result"]["from_validated_preview"] for s in saves)
    assert len(seen) == 3
    assert json.loads(seen[1][1]["content"])["quote_progress"]["priced_count"] == 1
    assert not current["agent_runs"][0]["usage"]["deferred_requirements"]


def test_mismatched_standard_price_routes_to_composition_and_saves(
        client, price_input, monkeypatch, redis_worker):
    price = seed(client, price_input, process="Different catalog process")
    job = enqueue(client, monkeypatch, 1)
    seen = provider(monkeypatch, [
        call("search_prices"),
        call("evaluate_price", line_id="line0", price_id=price["id"], revision=price["revision"]),
        call("preview_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "141.60"
    evaluation = next(s["result"] for s in current["agent_runs"][0]["steps"]
                      if s["tool"] == "evaluate_price")
    assert evaluation["pricing_route"]["stage"] == "compose"
    assert evaluation["pricing_route"]["direct_price_blockers"]
    context = json.loads(seen[2][1]["content"])
    assert context["pricing_stage"] == "compose"
    assert context["work_state"]["evaluation"]["blockers"]
    assert current["requirements"][0]["process"] == ""


def test_plan_stage_requires_specific_cost_for_more_research(
        client, price_input, monkeypatch, redis_worker, test_db):
    price = seed(client, price_input)
    job = enqueue(client, monkeypatch, 1)
    with test_db() as db:
        current = db.get(RequirementJob, job["id"])
        run = db.get(AgentRun, job["agent_runs"][0]["id"])
        run.usage = {**run.usage, "active_line_id": "line0"}
        result = execute_tool(db, current, run, "search_prices", SearchInput(), set())
        work.remember(current, run, "search_prices", {}, result)
        assert work.pricing_stage(current, run, "line0") == "compose"
        blocked = execute_tool(db, current, run, "search_prices", SearchInput(), set())
        assert "missing_cost" in blocked["error"]
        permitted = execute_tool(db, current, run, "search_prices",
                                 SearchInput(missing_cost="Backing panel material rate"), set())
        assert permitted["prices"][0]["id"] == price["id"]


def test_budget_reserves_one_actual_plan_attempt_after_search(
        client, price_input, monkeypatch, redis_worker):
    seed(client, price_input)
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 1000)
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    job = enqueue(client, monkeypatch, 1)
    sequence = iter([call("search_prices"),
                     call("preview_estimate", line_id="line0", estimate=plan()),
                     call("finish_quote")])
    menus = []

    def turn(messages, tools):
        menus.append({t["function"]["name"] for t in tools})
        return next(sequence), {"total_tokens": 1200}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "141.60"
    assert "submit_pricing_plan" in menus[1] and "search_prices" not in menus[1]


def test_preview_is_revalidated_against_changed_catalog_before_commit(
        client, price_input, monkeypatch, redis_worker, test_db):
    price = seed(client, price_input)
    job = enqueue(client, monkeypatch, 1)
    estimate = plan()
    estimate["costs"][0].update(source="catalog", reference_id=price["id"],
                                reference_revision=price["revision"])
    with test_db() as db:
        current = db.get(RequirementJob, job["id"])
        run = db.get(AgentRun, job["agent_runs"][0]["id"])
        args = {"line_id": "line0", "estimate": estimate}
        refs = {(price["id"], price["revision"])}
        preview = execute_tool(db, current, run, "preview_estimate",
                               CompleteEstimateInput.model_validate(args), refs)
        assert preview["amount"] == "141.60"
        work.remember(current, run, "preview_estimate", args, preview)
        db.get(PriceItem, price["id"]).revision += 1
        db.flush()
        assert commit_preview(db, current, run, refs)
        assert not current.requirements[0].get("estimate")
        assert not work.line_state(current, run, "line0").get("validated_plan")
        assert current.requirements[0]["quantity"] is None
        assert run.steps[-1]["result"]["blockers"]
        assert not commit_preview(db, current, run, refs)


def test_legacy_successful_preview_recovered_on_resume_without_model_copy(
        client, monkeypatch, redis_worker, test_db):
    job = enqueue(client, monkeypatch, 1)
    provider(monkeypatch, [
        call("defer_requirement", line_id="line0", cause="plan_repair", reason="Legacy exhausted budget"),
    ])
    run_workers()
    with test_db.begin() as db:
        current = db.get(RequirementJob, job["id"])
        old = db.get(AgentRun, job["agent_runs"][0]["id"])
        args = {"line_id": "line0", "estimate": plan()}
        preview = execute_tool(db, current, old, "preview_estimate",
                               CompleteEstimateInput.model_validate(args), set())
        old.steps = [{"tool": "preview_estimate", "arguments": args, "result": preview}, *old.steps]
    current = client.get(f'/api/jobs/{job["id"]}').json()
    seen = provider(monkeypatch, [call("finish_quote")])
    start(client, current, intent="resume")
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "141.60"
    assert len(seen) == 1
    assert json.loads(seen[0][1]["content"])["quote_progress"]["priced_count"] == 1


def test_changed_facts_invalidate_successful_preview():
    raw = RequirementLine(id="one", product="Panel").model_dump(mode="json")
    job = SimpleNamespace(requirements=[raw])
    run = SimpleNamespace(usage={}, steps=[])
    result = {"preview": True, "amount": "100.00", "blockers": [],
              "requirement": {**raw, "estimate": plan()}}
    step = {"tool": "preview_estimate", "arguments": {"line_id": "one", "estimate": plan()},
            "result": result}
    work.remember(job, run, step["tool"], step["arguments"], result)
    job.requirements = [{**raw, "quantity": 5}]
    assert not work.line_state(job, run, "one")
    work.restore_previews(job, run, [SimpleNamespace(steps=[step])])
    assert not work.line_state(job, run, "one").get("validated_plan")


def test_durable_context_filters_other_lines_and_keeps_v3_repair_identity(test_db):
    current = line_v3()
    raw = current.model_dump(mode="json")
    raw["estimate"] = None
    other = {**raw, "id": "other", "product": "Other"}
    job = SimpleNamespace(requirements=[raw, other], brief="", messages=[], extraction={},
                          business_rules_version="legacy")
    run = SimpleNamespace(usage={"durable": True}, steps=[
        {"tool": "evaluate_price", "arguments": {"line_id": "other"}, "result": {"blockers": ["Other"]}},
        {"tool": "read_requirement", "arguments": {"line_id": "test"}, "result": {}},
    ])
    estimate = current.estimate.model_dump(mode="json")
    work.remember(job, run, "preview_estimate", {"line_id": "test", "estimate": estimate},
                  {"preview": True, "blockers": ["Missing rate"], "amount": None})
    with test_db() as db:
        context = json.loads(context_messages(job, run, db)[1]["content"])
    assert context["pricing_stage"] == "repair"
    assert all(step["arguments"]["line_id"] == "test" for step in context["checkpoint"])
    assert context["work_state"]["failed_estimate"]["costs"][0]["measurement"] == "item_area"


def test_stage_tool_menu_has_one_estimate_schema_and_conditional_search_contract():
    original = deepcopy(TOOLS)
    composed = tools_for_stage(TOOLS, "compose", 3)
    names = {t["function"]["name"] for t in composed}
    assert "submit_pricing_plan" in names and "preview_estimate" not in names and "complete_estimate" not in names
    search = next(t for t in composed if t["function"]["name"] == "search_prices")
    assert "missing_cost" in search["function"]["parameters"]["required"]
    assert TOOLS == original
    assert len(json.dumps(composed)) < len(json.dumps(TOOLS))


def test_v3_failed_preview_repairs_measurement_then_saves(
        client, monkeypatch, redis_worker, test_db):
    job = enqueue(client, monkeypatch, 1)
    with test_db.begin() as db:
        current = db.get(RequirementJob, job["id"])
        current.requirements = [{**current.requirements[0], "area_m2": None,
                                  "width_mm": None, "height_mm": None}]
    estimate = line_v3().estimate.model_dump(mode="json")
    estimate["assumptions"] = [
        {"field": "quantity", "value": "2", "reason": "Two provisional pieces pending review"}]
    fixed = deepcopy(estimate)
    fixed["assumptions"].append(
        {"field": "area_m2", "value": "0.036", "reason": "Provisional panel area pending review"})
    seen = provider(monkeypatch, [
        call("preview_estimate", line_id="line0", estimate=estimate),
        call("preview_estimate", line_id="line0", estimate=fixed),
        call("finish_quote"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "8.64"
    context = json.loads(seen[1][1]["content"])
    assert context["pricing_stage"] == "repair"
    assert context["work_state"]["diagnostics"]["diagnostics"]
    assert current["requirements"][0]["quantity"] is None
    assert current["requirements"][0]["area_m2"] is None
    assert current["requirements"][0]["estimate"]["contract_version"] == 3
    assert client.get(f'/api/quotes/{current["quotes"][0]["id"]}/export').status_code == 409


def test_composite_scope_cannot_hide_missing_component_costs():
    from test_pipeline_v3 import calculate
    line = line_v3(components=[
        {"name": "Front", "width_mm": 100, "height_mm": 100, "quantity_per_item": 1},
        {"name": "Back", "width_mm": 100, "height_mm": 100, "quantity_per_item": 1},
    ])
    line.estimate.pricing_strategy = "composite"
    line.estimate.scope = "Complete assembly including front and back"
    result = calculate(line)
    assert result["amount"] is None
    assert result["diagnostics"][0]["code"] == "missing_component_costs"
    assert "components[1]" in result["diagnostics"][0]["paths"]
    for cost, component in zip(line.estimate.costs, line.components):
        cost.target = "component"
        cost.target_id = component.object_id
        cost.measurement = "component_area"
    assert calculate(line)["amount"] == "2.80"


def test_finished_composite_price_requires_valid_explicit_component_coverage():
    from test_pipeline_v3 import calculate
    line = line_v3(difficulty="C", components=[{"name": "Front"}, {"name": "Back"}])
    line.estimate.pricing_strategy = "composite"
    line.estimate.costs = [line.estimate.costs[0]]
    cost = line.estimate.costs[0]
    cost.measurement, cost.rate_unit, cost.kind = "item_count", "piece", "finished"
    cost.covers_component_ids = [component.object_id for component in line.components]
    assert calculate(line)["amount"] == "200.00"
    cost.kind = "process"
    assert calculate(line)["diagnostics"][0]["code"] == "invalid_component_coverage"
    cost.kind = "finished"
    cost.covers_component_ids = ["fabricated"]
    assert calculate(line)["diagnostics"][0]["code"] == "invalid_component_coverage"
