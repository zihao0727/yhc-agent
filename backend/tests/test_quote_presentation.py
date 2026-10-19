from copy import deepcopy

import pytest
import redis

from app import agent_checkpoints
from app.config import settings
from app.models import AgentRun
from app.quote_presentation import compact_progress, pending_inputs
from test_estimate_pricing import estimated_job


def test_current_audited_questions_are_not_rebuilt_by_legacy_enrichment():
    from types import SimpleNamespace
    from app.quote_presentation import enrich_legacy_issues
    payload = {"lines": [{
        "line_id": "one", "amount": None,
        "deferred_issue": {"cause": "catalog_gap", "reason": "LED unit basis missing"},
    }], "confirmation_items": [
        {"owner": "需求方", "cause": "customer_input", "question": "Provide panel geometry", "lines": []},
        {"owner": "报价管理员", "cause": "catalog_gap", "question": "Provide LED unit rate", "lines": []},
    ]}
    before = deepcopy(payload)
    result = enrich_legacy_issues(payload, SimpleNamespace(usage={}))
    assert result == before
    assert payload == before
    assert result is not payload


def test_missing_fields_resolve_actual_component_name_and_companion_values():
    raw = {"id": "one", "product": "入口标识", "components": [], "letter_groups": []}
    state = {
        "missing_fields": ["/parameters/-"],
        "pricing_proposal": {"plan": {
            "objects": [{"id": "face", "kind": "component", "name": "发光面板"}],
            "parameters": [{"target": "face", "basis": "source", "values": {"width_mm": "2531", "height_mm": "315"}}],
        }},
        "diagnostics": {"diagnostics": [{"paths": ["/parameters/-"],
            "engine_paths": ["components[0].quantity_per_item"]}]},
    }
    before = deepcopy(state)
    result = pending_inputs(raw, state)
    assert result[0]["field"] == "quantity_per_item"
    assert result[0]["target_id"] == "face" and result[0]["target_name"] == "发光面板"
    assert result[0]["missing"] and result[0]["value"] is None
    width = next(value for value in result if value["field"] == "width_mm")
    assert width["value"] == "2531" and width["basis"] == "source" and not width["missing"]
    assert state == before


def test_parameter_paths_preserve_target_scope_and_assumption_provenance():
    raw = {"id": "one", "product": "入口", "components": [], "letter_groups": []}
    state = {"missing_fields": ["/parameters/0/values/width_mm"],
             "pricing_proposal": {"plan": {
                 "objects": [{"id": "chars", "kind": "letter_group", "name": "发光字", "count": 3}],
                 "parameters": [{"target": "chars", "basis": "assumption", "values": {"width_mm": "100"}}]}}}
    result = pending_inputs(raw, state)
    assert result[0]["target"] == "letter_group" and result[0]["target_id"] == "chars"
    assert result[0]["basis"] == "assumption" and result[0]["value"] == "100"
    assert next(value for value in result if value["field"] == "count")["value"] == "3"


@pytest.mark.parametrize("location", ["saved", "failed"])
@pytest.mark.parametrize("target_key,target_value", [("target_id", "face"), ("target_index", 0)])
def test_legacy_proposed_objects_have_scoped_inputs_without_promoting_assumptions(location, target_key, target_value):
    raw = {"id": "one", "product": "入口", "components": [], "letter_groups": []}
    legacy = {"proposed_components": [{"object_id": "face", "name": "面板", "width_mm": "1200"}],
              "proposed_letter_groups": [{"object_id": "chars", "label": "文字", "count": 3}],
              "assumptions": [{"target": "component", target_key: target_value,
                               "field": "height_mm", "value": "500"}]}
    state = {"missing_fields": ["components[0].quantity_per_item", "letter_groups[0].height_mm"]}
    if location == "saved":
        raw["estimate"] = legacy
    else:
        state["failed_estimate"] = legacy
    before = deepcopy((raw, state))
    result = pending_inputs(raw, state)
    assert any(value["target_id"] == "face" and value["field"] == "quantity_per_item" for value in result)
    height = next(value for value in result if value["target_id"] == "face" and value["field"] == "height_mm")
    assert height["value"] == "500" and height["basis"] == "assumption" and not height["missing"]
    assert any(value["target_id"] == "chars" and value["field"] == "height_mm" for value in result)
    assert (raw, state) == before


def test_live_display_has_prices_and_components_without_repeating_original_upload_text():
    line = {"line_id": "one", "amount": "100.00", "unit_price": "50.00", "blockers": [],
            "requirement": {"notes": "Long original text"},
            "effective_requirement": {"quantity": 2, "width_mm": None, "height_mm": None,
                "notes": "Long original text", "evidence": [{"text": "Original"}],
                "components": [{"object_id": "face", "name": "Face", "width_mm": "2531", "height_mm": "315",
                    "evidence": [{"text": "Original"}]}]},
            "estimate_costs": [{"label": "Material", "rate": "100", "amount": "50.00"}]}
    original = deepcopy(line)
    result = compact_progress({"revision": 3, "priced_count": 1, "lines": [line]})
    row = result["lines"][0]
    assert row["amount"] == "100.00" and row["unit_price"] == "50.00"
    assert "requirement" not in row and "notes" not in row["effective_requirement"]
    assert row["effective_requirement"]["components"][0]["width_mm"] == "2531"
    assert "evidence" not in row["effective_requirement"]["components"][0]
    assert row["estimate_costs"][0]["rate"] == "100"
    assert line == original


@pytest.mark.parametrize("cache", ["missing", "older_revision", "older_run", "unavailable"])
def test_saved_prices_remain_visible_when_checkpoint_cannot_be_used(client, monkeypatch, cache):
    job = estimated_job(client, monkeypatch)
    monkeypatch.setattr(settings(), "agent_background", True)

    def checkpoint(*args, **kwargs):
        if cache == "unavailable":
            raise redis.ConnectionError("test unavailable cache")
        if cache == "missing":
            return None
        return {"revision": job["revision"] - (cache == "older_revision"),
                "run_id": job["agent_runs"][0]["id"] - (cache == "older_run"),
                "priced_count": 1, "known_subtotal": "999.00",
                "lines": [{"line_id": "line1", "amount": "999.00"}]}

    monkeypatch.setattr(agent_checkpoints, "read_checkpoint", checkpoint)
    for endpoint in ("", "/progress"):
        response = client.get(f'/api/jobs/{job["id"]}{endpoint}')
        assert response.status_code == 200, response.text
        display = response.json()["pricing_progress"]
        assert display["revision"] == job["revision"]
        assert display["known_subtotal"] == "141.60"
        assert display["lines"][0]["amount"] == "141.60"
        assert "requirement" not in display["lines"][0]


def test_reextracting_does_not_display_previous_prices_without_checkpoint(client, monkeypatch, test_db):
    job = estimated_job(client, monkeypatch)
    with test_db.begin() as db:
        run = db.get(AgentRun, job["agent_runs"][0]["id"])
        run.usage = {**run.usage, "extraction_input": {"supplementary_text": "重新整理"}, "prepared": False}
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(agent_checkpoints, "read_checkpoint", lambda *args, **kwargs: None)
    for endpoint in ("", "/progress"):
        response = client.get(f'/api/jobs/{job["id"]}{endpoint}')
        assert response.status_code == 200, response.text
        display = response.json()["pricing_progress"]
        assert display["priced_count"] == 0 and display["known_subtotal"] == "0"
        assert display["lines"] == []
