"""Regression gates for false gaps, durable recovery, coverage and the current UI projection."""
from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import agent_work_state as work
from app.catalog_search import search_catalog
from app.manufacturing import coverage_report, validate_production, validate_object_measurements
from app.models import AgentRun, PricingWorkItem, QuoteDraft, RequirementJob
from app.pricing_contract import ENGINE_VERSION
from app.pricing_errors import PricingError
from app.pricing_workflow import queue_changed_job, sync_work_items
from app.quote_workspace import build_workspace
from app.requirements_schemas import RequirementLine
from test_pricing_proposals import order
from test_pricing_foundations import add_price


def wait_for_input(order, cause="customer_input"):
    db, job, run = order
    raw = job.requirements[0]
    run.status, job.status = "waiting", "needs_review"
    run.usage = {**run.usage, "auto_resume": True, "external_processing_authorized": True,
                 "durable": True, "prepared": True, "work_state_restored": True}
    work.update_state(job, run, raw["id"], deferred=True, cause=cause, reason="Missing required facts",
                      question="What is the ordered quantity?", missing_fields=["quantity"],
                      repair_failures={"same-error": 3})
    sync_work_items(db, job, run)
    return db, job, run


def test_one_search_contract_is_used_by_list_agent_and_candidate_drawer(client, order):
    db, job, run = order
    price = add_price(db, "304#", Decimal("1.5"), process="丝网印刷", name="不锈钢表面加工")
    db.commit()
    query = "不锈钢 1.5mm 丝印"
    result = search_catalog(db, query)
    assert price.id in {hit.price.id for hit in result.hits}
    assert result.audit()["complete"]
    response = client.get("/api/prices", params={"q": query, "status": "active"}).json()
    assert price.id in {row["id"] for row in response["items"]}
    assert response["search"]["version"] == result.audit()["version"]
    drawer = client.get(f"/api/jobs/{job.id}/lines/{job.requirements[0]['id']}/candidates",
                        params={"q": query}).json()
    assert price.id in {row["id"] for row in drawer["prices"]}


def test_candidate_limit_keeps_every_requested_material_instead_of_smallest_record_ids(order):
    from app.quote_engine import candidate_prices
    db, _, _ = order
    for index in range(45):
        add_price(db, "304#", Decimal("1.2"), name=f"Steel option {index}")
    expected = [
        add_price(db, "304#", Decimal("1.5")),
        add_price(db, "6061铝", Decimal("3")),
        add_price(db, "亚克力", Decimal("5")),
    ]
    line = RequirementLine(id="multi", product="楼层索引牌",
        material="1.2mm不锈钢+1.5mm不锈钢+3mm铝板+5mm亚克力")
    candidates = candidate_prices(db, line, line.product)
    assert len(candidates) == 40
    assert {price.id for price in expected} <= {price["id"] for price in candidates}


def test_configuration_reference_cannot_be_used_as_a_standard_finished_price(order):
    from app.quote_engine import calculate_line
    db, job, _ = order
    price = add_price(db, "铝", Decimal("3"))
    price.attributes = {**price.attributes, "cost_group": "assembly_reference"}
    result = calculate_line(db, RequirementLine.model_validate({
        **job.requirements[0], "selected_price_id": price.id,
        "difficulty": "C",
        "selected_price_revision": price.revision}), automatic=True)
    assert result["amount"] is None
    assert any("不能直接作为整件报价" in text for text in result["blockers"])


def test_fact_edit_reopens_only_the_affected_line_and_keeps_saved_work(order):
    db, job, run = order
    first = job.requirements[0]
    second = {**deepcopy(first), "id": "unaffected"}
    job.requirements = [first, second]
    db, job, run = wait_for_input(order)
    work.update_state(job, run, "unaffected", deferred=True, cause="customer_input",
                      reason="Width is missing", question="What is its width?", missing_fields=["width_mm"])
    sync_work_items(db, job, run)
    job.requirements = [{**first, "quantity": 7}, second]
    reopened = queue_changed_job(db, job)
    assert reopened == [first["id"]]
    assert run.status == "processing" and job.status == "processing"
    assert not work.line_state(job, run, first["id"])["deferred"]
    assert work.line_state(job, run, "unaffected")["deferred"]
    assert job.requirements[1] == second
    assert run.usage["dependency_recovery"][0]["trigger"] == "facts_changed"


def test_unchanged_and_unrelated_catalog_updates_do_not_restart_waiting_work(order):
    db, job, run = wait_for_input(order, "catalog_gap")
    assert queue_changed_job(db, job) == []
    add_price(db, "实心黄铜", Decimal("13"), name="完全无关的独立原料", process="无")
    assert queue_changed_job(db, job) == []
    assert run.status == "waiting"


def test_relevant_catalog_change_reopens_once_and_persists_attempt_identity(order):
    db, job, run = wait_for_input(order, "catalog_gap")
    raw = job.requirements[0]
    add_price(db, raw["material"], Decimal(raw["thickness_mm"] or "3"), name=raw["product"])
    assert queue_changed_job(db, job) == [raw["id"]]
    run.status, job.status = "waiting", "needs_review"
    assert queue_changed_job(db, job, force_line_ids=[raw["id"]]) == []
    item = db.scalar(select(PricingWorkItem).where(PricingWorkItem.job_id == job.id))
    assert max(item.recovery["attempts"].values()) == 1


@pytest.mark.parametrize("guard", ["cancelled", "no_authorization", "disabled", "approved"])
def test_recovery_respects_stop_consent_policy_and_approved_quotes(order, guard):
    db, job, run = wait_for_input(order)
    raw = job.requirements[0]
    job.requirements = [{**raw, "quantity": 9}]
    if guard == "cancelled":
        run.status = "cancelled"
    elif guard == "no_authorization":
        run.usage = {**run.usage, "external_processing_authorized": False}
    elif guard == "disabled":
        run.usage = {**run.usage, "auto_resume": False}
    else:
        db.add(QuoteDraft(job_id=job.id, version=1, job_revision=job.revision,
                          status="approved", payload={"lines": []}, terms="Approved quote"))
        db.flush()
    assert queue_changed_job(db, job) == []


def test_engine_version_change_revalidates_saved_failures_once(order):
    db, job, run = wait_for_input(order, "execution_error")
    item = db.scalar(select(PricingWorkItem).where(PricingWorkItem.job_id == job.id))
    item.dependencies = {**item.dependencies, "engine": "pricing.previous"}
    assert queue_changed_job(db, job) == [job.requirements[0]["id"]]
    assert item.engine_version == ENGINE_VERSION
    assert item.recovery["trigger"] == "engine_changed"


def test_scoped_manual_retry_does_not_reopen_unselected_changed_lines(order):
    db, job, run = order
    first = job.requirements[0]
    job.requirements = [first, {**deepcopy(first), "id": "other"}]
    wait_for_input(order, "plan_repair")
    sync_work_items(db, job, run)
    job.requirements = [{**raw, "quantity": 11} for raw in job.requirements]
    assert queue_changed_job(db, job, force_line_ids=[]) == []
    assert queue_changed_job(db, job, force_line_ids=[first["id"]]) == [first["id"]]
    assert not work.line_state(job, run, "other").get("dependency_recovery")


def test_global_confirmation_is_preserved_without_inventing_a_line_binding(order):
    db, job, run = wait_for_input(order)
    current = build_workspace(db, job, run, [{
        "job_revision": job.revision, "outdated": False,
        "payload": {"confirmation_items": [{
            "cause": "customer_input", "question": "Confirm delivery address",
            "reason": "Delivery scope has not been supplied", "lines": [],
        }]},
    }])
    global_issue = next(row for row in current["pending"] if row["question"] == "Confirm delivery address")
    assert global_issue["products"] == [] and global_issue["inputs"] == []
    assert not current["summary"]["complete"]


def test_legacy_review_questions_remain_system_review_until_source_is_confirmed(order):
    db, job, run = wait_for_input(order)
    workspace = build_workspace(db, job, run, [{
        "job_revision": job.revision, "outdated": False,
        "payload": {"review_questions": ["Check whether the two drawings depict the same product"]},
    }])
    issue = next(row for row in workspace["pending"] if row["products"] == [])
    assert issue["owner"] == "系统核图" and issue["cause"] == "recognition_review"
    assert issue["reason"]


def test_approved_commercial_rounding_and_resolved_questions_remain_consistent(order):
    db, job, run = order
    job.requirements = [{**job.requirements[0], "manual_unit_price": "12.34",
                         "manual_price_note": "Administrator confirmed the complete price"}]
    current = {"job_revision": job.revision, "outdated": False, "status": "approved",
               "payload": {"complete": True, "total": "20.00", "unrounded_total": "24.68",
                           "rounding": "ten", "review_questions": ["Terms reviewed during approval"]}}
    workspace = build_workspace(db, job, run, [current])
    assert workspace["summary"]["amount"] == "20.00"
    assert workspace["pending"] == []
    assert workspace["summary"]["complete"]
    current["payload"]["unrounded_total"] = "999.00"
    assert build_workspace(db, job, run, [current])["summary"]["amount"] == "24.68"


def test_exhaustion_never_certifies_missing_customer_information(order):
    _, job, run = order
    raw = job.requirements[0]
    work.update_state(job, run, raw["id"], diagnostics={
        "blockers": ["No rate selected"], "diagnostics": [
            {"cause": "catalog_gap", "paths": ["estimate.costs"], "code": "missing_rate"}]})
    assert work.pause_details(job, run, raw["id"], "no_progress")["cause"] == "execution_error"
    assert work.pause_details(job, run, raw["id"], "repeated_failure")["cause"] == "plan_repair"


def test_current_projection_preserves_audited_questions_and_overrules_historical_amount(order):
    db, job, run = wait_for_input(order)
    raw = job.requirements[0]
    prior = {"id": 1, "job_revision": job.revision, "outdated": False, "payload": {
        "lines": [{"line_id": raw["id"], "amount": "1299.60"}],
        "confirmation_items": [
            {"cause": "customer_input", "owner": "需求方", "question": "Provide component dimensions",
             "reason": "Outer dimensions are not component material quantities",
             "lines": [{"line_id": raw["id"], "product": raw["product"]}]},
            {"cause": "catalog_gap", "owner": "报价管理员", "question": "Provide LED module rate",
             "reason": "Power supply does not include LEDs",
             "lines": [{"line_id": raw["id"], "product": raw["product"]}]},
        ]}}
    before = deepcopy(prior)
    current = build_workspace(db, job, run, [prior])
    assert current["summary"]["priced"] == 0 and current["summary"]["amount"] is None
    assert all(row["amount"] is None for row in current["lines"])
    assert {row["question"] for row in current["pending"]} >= {
        "Provide component dimensions", "Provide LED module rate"}
    assert all(row["reason"] for row in current["pending"])
    assert prior == before


def test_system_diagnostics_have_a_system_owner_and_no_customer_form(order):
    db, job, run = wait_for_input(order, "plan_repair")
    workspace = build_workspace(db, job, run)
    assert workspace["state_counts"]["system_error"] == 1
    assert workspace["pending"][0]["kind"] == "system"
    assert workspace["pending"][0]["owner"] == "系统核价"


def test_legacy_owner_cannot_route_a_known_technical_failure_to_the_customer(order):
    db, job, run = wait_for_input(order, "plan_repair")
    workspace = build_workspace(db, job, run, [{
        "job_revision": job.revision, "outdated": False, "payload": {"confirmation_items": [{
            "cause": "plan_repair", "owner": "需求方", "question": "Repair malformed formula",
            "lines": [{"line_id": job.requirements[0]["id"]}],
        }]},
    }])
    row = next(row for row in workspace["pending"] if row["question"] == "Repair malformed formula")
    assert row["owner"] == "系统核价" and row["kind"] == "system"


def test_each_object_requires_its_own_production_coverage_and_power_does_not_supply_led():
    line = RequirementLine(id="assembly", product="Sign", components=[
        {"object_id": "front", "name": "Front", "processes": ["丝印"]},
        {"object_id": "back", "name": "Back", "processes": ["丝印"]},
    ])
    costs = [{"label": "Front print", "source": "catalog", "kind": "process", "target_id": "front",
              "reference": {"process_codes": ["screen_print"]}}]
    report = coverage_report(line, costs)
    assert [row["covered"] for row in report["requirements"]] == [True, False]
    with pytest.raises(PricingError, match="back"):
        validate_production(line, costs)
    costs.append({**costs[0], "target_id": "back"})
    assert validate_production(line, costs)["complete"]
    costs[0]["process_codes"] = ["led"]
    with pytest.raises(PricingError) as error:
        validate_production(line, costs)
    assert error.value.diagnostic["code"] == "unsupported_process_coverage"


def test_local_dimension_cannot_be_used_as_overall_shell_depth():
    line = RequirementLine(id="sign", product="Sign", depth_mm=10, rate_evidence_required=True,
        dimension_evidence=[{"file_id": 1, "page": 1, "text": "Local recess 10mm",
                             "field": "depth_mm", "value": 10, "role": "local"}])
    with pytest.raises(PricingError) as error:
        validate_object_measurements(line)
    assert error.value.diagnostic["code"] == "dimension_role_mismatch"


def test_contract_generation_checks_actual_parameter_capacity():
    from scripts.export_quote_contract import render
    output = render()
    assert "compiledAssumptions" in output and ENGINE_VERSION in output
