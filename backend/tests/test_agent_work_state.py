from copy import deepcopy
from types import SimpleNamespace

import pytest

from app import agent_work_state as work
from app.agent_tools import TOOLS, tools_for_stage
from app.requirements_schemas import RequirementLine


def context():
    raw = RequirementLine(id="panel", product="Sign").model_dump(mode="json")
    return SimpleNamespace(requirements=[raw]), SimpleNamespace(usage={"active_line_id": "panel"}, steps=[])


def test_resumed_context_omits_old_pause_but_retains_repair_diagnostics():
    job, run = context()
    work.update_state(job, run, "panel", deferred=False, cause="execution_error",
                      reason="Old timeout", question="Old question", missing_fields=["quantity"],
                      diagnostics={"blockers": ["Current validation failure"]})
    result = work.context_state(job, run, "panel")
    assert not {"cause", "reason", "question", "missing_fields"} & result.keys()
    assert result["diagnostics"]["blockers"] == ["Current validation failure"]
    work.update_state(job, run, "panel", deferred=True)
    assert work.context_state(job, run, "panel")["reason"] == "Old timeout"


def test_verified_catalog_gaps_survive_resume_until_catalog_changes(test_db):
    from app.models import AgentRun, RequirementJob, PricingRule
    with test_db.begin() as db:
        raw = RequirementLine(id="panel", product="Sign").model_dump(mode="json")
        job = RequirementJob(title="Verified gap", requirements=[raw])
        db.add(job)
        db.flush()
        old = AgentRun(job_id=job.id, model="test", steps=[], usage={})
        db.add(old)
        db.flush()
        work.update_state(job, old, "panel", deferred=True, cause="catalog_gap",
                          reason="Missing civil rate", question="Provide rate and unit",
                          gap_reference_signature=work.catalog_signature(db))
        resumed = AgentRun(job_id=job.id, model="test", steps=[], usage={})
        db.add(resumed)
        db.flush()
        work.restore_work_state(db, job, resumed)
        assert work.line_state(job, resumed, "panel")["deferred"]
        db.add(PricingRule(name="New civil rate", content="New basis", status="active"))
        db.flush()
        changed = AgentRun(job_id=job.id, model="test", steps=[], usage={})
        db.add(changed)
        db.flush()
        work.restore_work_state(db, job, changed)
        assert not work.line_state(job, changed, "panel")["deferred"]

def test_parallel_pricing_budget_ignores_queue_time_but_limits_actual_processing(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), "agent_line_max_seconds", 60)
    job, run = context()
    work.update_state(job, run, "panel", started_at=1, pricing_seconds=0)
    assert work.exhausted_reason(job, run, "panel") is None
    work.update_state(job, run, "panel", pricing_seconds=60)
    assert work.exhausted_reason(job, run, "panel") == "line_time_budget"


def test_new_source_evidence_keeps_research_open_after_five_reads():
    job, run = context()
    for page in range(1, 13):
        work.update_state(job, run, "panel", idle_rounds=8)
        work.remember(job, run, "read_source", {"file_id": 1, "page": page},
                      {"text": f"Actual page {page}", "file_id": 1, "page": page})
    state = work.line_state(job, run, "panel")
    assert state["research_calls"] == 12
    assert state["idle_rounds"] == 0 and not state["decision_due"]
    names = {tool["function"]["name"] for tool in tools_for_stage(TOOLS, "research", 3, state["decision_due"])}
    assert {"read_source", "run_file_script", "inspect_source"} <= names


def test_empty_or_repeated_research_is_not_new_evidence():
    job, run = context()
    for index in range(6):
        work.remember(job, run, "search_prices", {"query": str(index)}, {"prices": []})
    assert work.line_state(job, run, "panel")["decision_due"]
    names = {tool["function"]["name"] for tool in tools_for_stage(TOOLS, "compose", 3, True)}
    assert {"read_source", "run_file_script", "inspect_source"} <= names
    assert "search_prices" not in names
    work.remember(job, run, "read_source", {}, {"text": "A new verifiable specification"})
    assert not work.line_state(job, run, "panel")["decision_due"]
    for _ in range(6):
        work.remember(job, run, "read_source", {}, {"text": "A new verifiable specification"})
    assert work.line_state(job, run, "panel")["decision_due"]


def test_source_locations_and_visual_views_count_as_evidence_progress():
    job, run = context()
    for page in range(1, 8):
        work.remember(job, run, "read_source", {"file_id": 1, "page": page}, {"text": "Same header"})
    assert not work.line_state(job, run, "panel")["decision_due"]
    for _ in range(5):
        work.remember(job, run, "read_source", {"file_id": 1, "page": 7}, {"text": "Same header"})
    assert work.line_state(job, run, "panel")["decision_due"]
    work.remember(job, run, "view_source_region", {"file_id": 1, "page": 8},
                  {"file_id": 1, "page": 8, "region": [0, 0, 1, 1], "viewed": True})
    assert not work.line_state(job, run, "panel")["decision_due"]


def test_preserve_legacy_inputs_before_edit_and_restore_into_new_run(test_db):
    from app.models import AgentRun, RequirementJob

    raw = RequirementLine(id="panel", product="Sign").model_dump(mode="json")
    with test_db.begin() as db:
        job = RequirementJob(title="Legacy", requirements=[raw])
        db.add(job)
        db.flush()
        old = AgentRun(job_id=job.id, model="test", steps=[], usage={"work_state": {"panel": {
            "fingerprint": work.fingerprint(raw),
            "failed_estimate": {"costs": [{"label": "Material", "expected_amount": "100"}]},
            "candidates": [{"id": 12, "revision": 2}], "deferred": True,
        }}})
        stale = AgentRun(job_id=job.id, model="test", steps=[], usage={"work_state": {"panel": {
            "fingerprint": "stale", "failed_estimate": {"scope": "Do not restore"},
        }}})
        db.add_all([old, stale])
        db.flush()
        work.preserve_inputs(db, job)
        assert old.usage["work_state"]["panel"]["input_snapshot"] == raw
        assert "input_snapshot" not in stale.usage["work_state"]["panel"]
        job.requirements = [{**raw, "width_mm": "1200"}]
        current = AgentRun(job_id=job.id, model="test", steps=[], usage={})
        db.add(current)
        db.flush()
        work.restore_work_state(db, job, current)
        restored = work.line_state(job, current, "panel")
        assert restored["restored_from_run"] == old.id and restored["requires_revalidation"]
        assert restored["failed_estimate"]["costs"][0]["expected_amount"] is None
        assert restored["candidates"] == [{"id": 12, "revision": 2}]
        assert not restored.get("validated_plan") and not restored["deferred"]


def test_parameter_edit_retains_plan_and_candidates_but_requires_fresh_validation():
    job, run = context()
    proposal = {"version": 3, "plan": {"objects": [], "parameters": [
        {"target": "item", "values": {"width_mm": "1000", "height_mm": "500", "area_m2": "0.5"},
         "basis": "assumption"}], "costs": [{"label": "Material", "rate": 100}]}}
    work.update_state(job, run, "panel", pricing_proposal=proposal,
                      candidates=[{"id": 12, "revision": 2}], validated_plan={"costs": []},
                      deferred=True, diagnostics={"blockers": ["Missing width"]}, line_tokens=500,
                      source_view_pending={"file_id": 1, "page": 1})
    original = deepcopy(run.usage)
    job.requirements = [{**job.requirements[0], "width_mm": "1200"}]
    state = work.line_state(job, run, "panel")
    assert state["pricing_proposal"]["version"] == 4
    assert state["pricing_proposal"]["plan"]["parameters"][0]["values"] == {"height_mm": "500"}
    assert state["pricing_proposal"]["plan"]["costs"] == proposal["plan"]["costs"]
    assert state["candidates"] == [{"id": 12, "revision": 2}]
    assert state["requires_revalidation"] and not state["deferred"]
    assert not state.get("validated_plan") and not state.get("diagnostics")
    assert not state.get("source_view_pending")
    assert work.pricing_stage(job, run, "panel") == "repair"
    assert run.usage == original
    work.update_state(job, run, "panel", idle_rounds=1)
    assert work.line_state(job, run, "panel")["pricing_proposal"]["version"] == 4


@pytest.mark.parametrize("field,value", [
    ("product", "Another sign"), ("notes", "Different original specification"),
    ("evidence", [{"file_id": 2, "page": 1, "text": "Different source"}]),
    ("business_rules_version", "2026-09-23.v1"),
])
def test_identity_or_original_source_changes_discard_old_work(field, value):
    job, run = context()
    work.update_state(job, run, "panel", pricing_proposal={"version": 1, "plan": {}})
    job.requirements = [{**job.requirements[0], field: value}]
    assert not work.line_state(job, run, "panel")


def test_supplemented_object_is_reused_without_duplicate_declaration():
    job, run = context()
    plan = {"objects": [{"id": "face", "kind": "component", "name": "Face"},
                         {"id": "back", "kind": "component", "name": "Back"}],
            "parameters": [{"target": "face", "values": {"width_mm": "1000", "height_mm": "500"}}],
            "costs": [{"target": "face", "label": "Face"}, {"target": "back", "label": "Back"}]}
    work.update_state(job, run, "panel", pricing_proposal={"version": 1, "plan": plan})
    job.requirements = [{**job.requirements[0], "components": [
        {"object_id": "face", "name": "Face", "width_mm": "1200", "height_mm": None}]}]
    rebased = work.line_state(job, run, "panel")["pricing_proposal"]["plan"]
    assert [obj["id"] for obj in rebased["objects"]] == ["back"]
    assert rebased["parameters"][0]["values"] == {"height_mm": "500"}
    assert rebased["costs"] == plan["costs"]
