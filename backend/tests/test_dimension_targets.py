import json

import httpx
import pytest
from pydantic import ValidationError

from app import deepseek
from app.requirements_schemas import ExtractedLine
from test_deepseek import configure_transport, file_fixture


def dimension(**changes):
    return {"file_id": 1, "page": 1, "field": "height_mm",
            "value": 80, "text": "80mm", **changes}


@pytest.mark.parametrize("explicit", [False, True])
def test_model_item_zero_index_is_normalized_without_changing_dimensions(explicit):
    evidence = dimension(target_index=0)
    if explicit:
        evidence["target"] = "item"
    raw = {"lines": [{"dimension_evidence": [evidence]}]}
    deepseek.normalize_dimension_targets(raw)
    assert evidence["target_index"] is None
    assert evidence["value"] == 80
    assert ExtractedLine.model_validate(raw["lines"][0]).dimension_evidence[0].target == "item"


@pytest.mark.parametrize("target,index", [
    ("item", 1), ("item", -1), ("item", "0"), ("item", False),
    ("item", 0.0), ("letter_group", None), ("component", None),
])
def test_adapter_never_guesses_other_target_indices(target, index):
    evidence = dimension(target=target, target_index=index)
    raw = {"lines": [{"dimension_evidence": [evidence]}]}
    deepseek.normalize_dimension_targets(raw)
    assert evidence["target_index"] is index
    with pytest.raises(ValidationError):
        ExtractedLine.model_validate(raw["lines"][0])


@pytest.mark.parametrize("target", ["letter_group", "component"])
def test_group_or_component_zero_index_is_preserved(target):
    evidence = dimension(target=target, target_index=0)
    raw = {"lines": [{"dimension_evidence": [evidence]}]}
    deepseek.normalize_dimension_targets(raw)
    assert evidence["target_index"] == 0
    assert ExtractedLine.model_validate(raw["lines"][0]).dimension_evidence[0].target == target


def test_api_schema_still_rejects_item_index_and_reports_specific_code():
    with pytest.raises(ValidationError) as error:
        ExtractedLine(dimension_evidence=[dimension(target="item", target_index=0)])
    assert error.value.errors()[0]["type"] == "dimension_target_index"


def test_model_item_index_compatibility_still_validates_dimensions(monkeypatch):
    def handler(request):
        result = {"lines": [{"product": "panel",
            "evidence": [{"file_id": 1, "page": 1, "text": "panel"}],
            "dimension_evidence": [dimension(target="item", target_index=0)]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(result)}}]})

    configure_transport(monkeypatch, handler)
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert attempts == 1
    assert result.lines[0].height_mm == 80
    assert result.lines[0].dimension_evidence[0].target_index is None


def test_missing_group_index_repair_is_actionable_without_leaking_output(monkeypatch):
    calls = []
    secret = "sensitive-model-output"

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        evidence = dimension(target="letter_group", role="character")
        if len(calls) == 2:
            evidence["target_index"] = 0
        result = {"lines": [{"product": secret,
            "evidence": [{"file_id": 1, "page": 1, "text": "panel"}],
            "letter_groups": [{"count": 1}], "dimension_evidence": [evidence]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(result)}}]})

    configure_transport(monkeypatch, handler)
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert attempts == 2
    repair = calls[1]["messages"][-1]["content"]
    assert "dimension_target_index" in repair
    assert "target=item" in repair and "target=letter_group/component" in repair
    assert secret not in repair
    assert result.lines[0].letter_groups[0].height_mm == 80
    assert result.lines[0].height_mm is None


def test_compatibility_cannot_bypass_provenance_checks(monkeypatch):
    def handler(request):
        result = {"lines": [{"evidence": [{"file_id": 1, "page": 1, "text": "panel"}],
            "dimension_evidence": [dimension(file_id=99, target="item", target_index=0)]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(result)}}]})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file_fixture()], "", [], "")
    assert failure.value.attempts == 2
    assert failure.value.diagnostics["failures"][-1]["stage"] == "file_source"
