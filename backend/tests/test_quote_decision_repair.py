from types import SimpleNamespace
import time

from app import agent_work_state as work
from app.agent_tools import TOOLS, tools_for_stage
from app.source_coverage import row_kind
from app.requirements_schemas import ExtractionResult, ExtractedLine, Evidence
from app.customer_excel import validate_excel_rows
from test_redis_agent import redis_worker, enqueue  # noqa: F401
from test_agent_routing import fake_model_key, run_workers  # noqa: F401
from test_agent_loop import call
from test_estimate_pricing import plan


def state():
    return SimpleNamespace(requirements=[{"id": "one"}]), SimpleNamespace(
        usage={"active_line_id": "one"}, steps=[])


def test_zero_line_token_limit_preserves_usage_and_other_guards(monkeypatch):
    from app.config import settings, Settings
    assert Settings.model_fields["agent_line_max_tokens"].default == 0
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 0)
    job, run = state()
    work.update_state(job, run, "one", started_at=time.time(), line_tokens=9000000)
    assert work.exhausted_reason(job, run, "one") is None
    work.charge_line(job, run, 1000)
    assert work.line_state(job, run, "one")["line_tokens"] == 9001000
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 60000)
    assert work.exhausted_reason(job, run, "one") == "line_token_budget"
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 0)
    work.update_state(job, run, "one", repair_failures={"same": settings().agent_repair_attempts})
    assert work.exhausted_reason(job, run, "one") == "repeated_failure"


def test_disabled_token_budget_reopens_only_matching_budget_deferrals_once(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 0)
    job, run = state()
    work.update_state(job, run, "one", deferred=True, reason="budget", line_tokens=90000,
                      failed_estimate={"scope": "keep"}, started_at=1)
    run.steps = [{"tool": "defer_requirement", "result": {
        "line_id": "one", "deferred": True, "reason": "budget", "pause_trigger": "line_token_budget"}}]
    assert work.reopen_disabled_token_deferrals(job, run) == ["one"]
    current = work.line_state(job, run, "one")
    assert current["line_tokens"] == 90000 and current["failed_estimate"] == {"scope": "keep"}
    assert not current["deferred"] and current["started_at"] > 1
    work.update_state(job, run, "one", deferred=True)
    assert work.reopen_disabled_token_deferrals(job, run) == []
    work.update_state(job, run, "one", token_budget_reopened=False, reason="customer input missing")
    assert work.reopen_disabled_token_deferrals(job, run) == []


def test_decision_turn_keeps_targeted_research_and_scripts_available():
    names = {tool["function"]["name"] for tool in tools_for_stage(TOOLS, "compose", 3, True)}
    assert {"submit_pricing_plan", "repair_pricing_plan", "select_price", "ask_user",
            "defer_requirement", "read_rules", "read_source", "view_source_region", "run_file_script"} <= names
    assert "search_prices" not in names


def test_blocked_candidate_keeps_research_open_and_usable_candidate_prompts_decision():
    job, run = state()
    work.remember(job, run, "evaluate_price", {"line_id": "one", "price_id": 42},
                  {"price": {"id": 42, "revision": 2}, "blockers": ["power count missing"]})
    assert not work.line_state(job, run, "one")["decision_due"]
    work.remember(job, run, "evaluate_price", {"line_id": "one", "price_id": 42},
                  {"price": {"id": 42, "revision": 2}, "amount": "100.00", "blockers": []})
    assert work.line_state(job, run, "one")["decision_due"]
    work.remember(job, run, "preview_estimate", {"line_id": "one", "estimate": {}},
                  {"preview": True, "amount": None, "blockers": ["missing width"]})
    assert not work.line_state(job, run, "one")["decision_due"]


def test_repeated_source_reading_reserves_decision_before_exhaustion():
    job, run = state()
    for _ in range(6):
        work.remember(job, run, "read_rules", {}, {"rules": []})
    assert work.line_state(job, run, "one")["decision_due"]
    details = work.pause_details(job, run, "one", "line_token_budget")
    assert details["cause"] == "execution_error"
    assert "模型用量" in details["reason"] and "尚未提交" in details["reason"]


def source():
    def row(n, values):
        return {"row": n, "cells": [{"cell": f"{col}{n}", "value": value} for col, value in values.items()]}
    data = {"sheet": "清单", "images": [], "rows": [
        row(1, {"A": "序号", "C": "项目名称", "D": "项目特征描述", "E": "计量单位", "F": "工程量"}),
        row(2, {"C": "户外"}),
        row(3, {"A": "1", "C": "入口标识", "D": "不锈钢制作", "E": "套", "F": "2"}),
        row(4, {"A": "本页小计"}),
        row(5, {"C": "室内", "D": "标识", "E": "件", "F": "1"}),
    ]}
    data["header_rows"] = [data["rows"][0]]
    return data


def test_section_and_summary_never_readded_by_coverage_fallback():
    data = source()
    result = ExtractionResult(lines=[
        ExtractedLine(product="户外", evidence=[Evidence(file_id=1, page=1, sheet="清单", cell="C2", text="户外")]),
        ExtractedLine(product="本页小计", evidence=[Evidence(file_id=1, page=1, sheet="清单", cell="A4", text="本页小计")]),
    ])
    result = validate_excel_rows(result, {(1, 1): data})
    assert [line.product for line in result.lines] == ["入口标识", "室内"]
    coverage = {item.row: item.status for item in result.source_coverage}
    assert coverage[2] == coverage[4] == "non_product"
    assert not any("户外" in question or "小计" in question for question in result.questions)


def test_real_product_named_like_section_is_not_removed():
    columns = {"product": 3, "item": 1, "quantity": 6, "unit": 5}
    assert row_kind(source()["rows"][-1], 1, columns) is None
    assert row_kind(source()["rows"][1], 1, columns) == "non_product"


def test_research_loop_forces_preview_and_completes(client, monkeypatch, redis_worker):
    from app import deepseek
    job = enqueue(client, monkeypatch, 1)
    turns = []

    def turn(messages, tools):
        names = {tool["function"]["name"] for tool in tools}
        turns.append(names)
        if len(turns) <= 6:
            return call("read_rules", query=f"query{len(turns)}"), {"total_tokens": 20}
        if len(turns) == 7:
            assert {"read_rules", "submit_pricing_plan", "run_file_script"} <= names
            assert "search_prices" not in names
            return call("preview_estimate", line_id="line0", estimate=plan()), {"total_tokens": 20}
        return call("finish_quote"), {"total_tokens": 20}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["quotes"][0]["payload"]["total"] == "141.60"
    assert len(turns) == 8


def test_cleanup_protects_human_input_and_preserves_original(test_db, monkeypatch):
    from app.models import CustomerFile, RequirementJob
    from app import non_product_rows
    from app.requirements_schemas import RequirementLine
    # Only page reads are substituted; the archival transaction uses the real database.
    monkeypatch.setattr(non_product_rows, "page_source", lambda *args: source())
    with test_db.begin() as db:
        job = RequirementJob(title="Headings", brief="", requirements=[])
        db.add(job)
        db.flush()
        file = CustomerFile(job_id=job.id, filename="table.xlsx", sha256="0" * 64,
                            media_type=non_product_rows.MEDIA_TYPE, storage_key="test",
                            byte_size=1, page_count=1)
        db.add(file)
        db.flush()
        raw = RequirementLine(id="heading", product="户外", evidence=[
            Evidence(file_id=file.id, page=1, sheet="清单", cell="C2", text="户外")]).model_dump(mode="json")
        job.requirements = [raw, {**raw, "id": "human", "confirmed": True}]
        excluded = non_product_rows.exclude_non_products(db, job)
        assert len(excluded) == 1
        assert [r["id"] for r in job.requirements] == ["human"]
        assert job.extraction["excluded_non_products"][0]["requirement"] == raw


def test_budget_reserves_bounded_repairs_after_first_preview(client, monkeypatch, redis_worker):
    from app import deepseek
    from app.config import settings
    from copy import deepcopy
    monkeypatch.setattr(settings(), "agent_line_max_tokens", 1000)
    job = enqueue(client, monkeypatch, 1)
    wrong = deepcopy(plan())
    wrong["costs"][0]["quantity_formula"] = "=unknown_field"
    sequence = iter([
        call("read_rules", query="material"),
        call("preview_estimate", line_id="line0", estimate=wrong),
        call("preview_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    def turn(messages, tools):
        return next(sequence), {"total_tokens": 1200}
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["quotes"][0]["payload"]["total"] == "141.60"
