import copy
import json
import threading

import httpx
import pytest

from app import agent_worker, deepseek
from app.extraction_checkpoints import ExtractionCheckpoint
from app.extraction_recovery import recovery_tools
from test_agent_loop import call
from test_deepseek import configure_transport, file_fixture
from test_extraction_repairs import enqueue_excel
from test_redis_agent import redis_worker  # noqa: F401


def broken_line():
    return {"product": "panel", "evidence": [{"file_id": 1, "page": 1, "text": "panel"}],
            "height_mm": 90, "dimension_evidence": [
                {"file_id": 1, "page": 1, "field": "height_mm", "value": 90, "text": "80mm"}]}


def checkpoint(saved=None):
    snapshots, charges, events = [], [], []
    result = None

    def persist(state, tokens):
        snapshots.append(copy.deepcopy(state))
        charges.append(tokens)
        if result.recovery_event:
            events.append(copy.deepcopy(result.recovery_event))

    result = ExtractionCheckpoint("recovery", saved or {}, persist, lambda: None, recovery_enabled=True)
    return result, snapshots, charges, events


def invalid_transport(monkeypatch, lines=None):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps({"lines": lines or [broken_line()]})}}],
            "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    return requests


def turns(monkeypatch, responses):
    seen = []
    answers = iter(responses)

    def turn(messages, tools):
        seen.append(copy.deepcopy(messages))
        return next(answers), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    return seen


def test_agent_rereads_and_repairs_only_failed_line_without_duplicate_charges(monkeypatch):
    good = {"product": "other", "quantity": 2, "evidence": [{"file_id": 1, "page": 1, "text": "other"}]}
    requests = invalid_transport(monkeypatch, [broken_line(), good])
    fixed = broken_line()
    fixed["dimension_evidence"][0]["value"] = 80
    fixed["height_mm"] = 80
    seen = turns(monkeypatch, [
        call("read_extraction_source", file_id=1, page=1),
        call("repair_extraction_line", line_index=0, line=fixed)])
    cache, snapshots, charges, events = checkpoint()
    result, attempts, usage = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert attempts == 4 and usage["total_tokens"] == 34
    assert sum(t.get("total_tokens", 0) for t in charges) == 34
    assert len(requests) == 2 and result.lines[0].height_mm == 80
    assert result.lines[1].quantity == 2
    assert any(p.get("type") == "image_url" for p in seen[1][-1]["content"])
    assert any(e["tool"] == "extraction_recovery_completed" for e in events)
    assert snapshots[-1]["recoveries"] == {}
    cached, count, tokens = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert count == 0 and tokens == {} and cached.lines[0].height_mm == 80


def test_new_validation_error_is_returned_to_agent_until_every_line_is_fixed(monkeypatch):
    invalid_transport(monkeypatch, [broken_line(), {**broken_line(), "product": "second"}])
    first = broken_line()
    first["height_mm"] = 80
    first["dimension_evidence"][0]["value"] = 80
    seen = turns(monkeypatch, [
        call("repair_extraction_line", line_index=0, line=first),
        call("repair_extraction_line", line_index=1, line={**first, "product": "second"})])
    cache, _, _, _ = checkpoint()
    result, _, _ = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    feedback = json.loads(seen[1][-1]["content"])
    assert feedback["diagnostics"]["line_index"] == 1
    assert feedback["diagnostics"]["stage"] == "dimension_evidence"
    assert [line.height_mm for line in result.lines] == [80, 80]


def test_batch_tool_repairs_top_level_format_and_preserves_projects(monkeypatch):
    requests = []
    line = {"product": "panel", "evidence": [{"file_id": 1, "page": 1, "text": "panel"}]}

    def handler(request):
        requests.append(1)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps({"lines": [line], "unsupported": True})}}]})

    configure_transport(monkeypatch, handler)
    turns(monkeypatch, [call("repair_extraction_batch", result={"lines": [line]})])
    cache, _, _, _ = checkpoint()
    result, count, _ = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert len(requests) == 2 and count == 3 and result.lines[0].product == "panel"


def test_coverage_failure_restores_original_rows_before_agent_repair(monkeypatch):
    requests = []
    original = [{**broken_line(), "unsupported": True},
                {"product": "other", "evidence": [{"file_id": 1, "page": 1, "text": "other"}]}]

    def handler(request):
        requests.append(1)
        lines = original if len(requests) == 1 else [original[1]]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps({"lines": lines})}}]})

    configure_transport(monkeypatch, handler)
    fixed = broken_line()
    fixed["height_mm"] = 80
    fixed["dimension_evidence"][0]["value"] = 80
    seen = turns(monkeypatch, [call("repair_extraction_line", line_index=0, line=fixed)])
    cache, _, _, _ = checkpoint()
    result, _, _ = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert len(json.loads(seen[0][1]["content"])["pending_lines"]) == 2
    assert [line.product for line in result.lines] == ["panel", "other"]


def test_unreadable_annotation_keeps_original_evidence_and_clears_price_input(monkeypatch):
    invalid_transport(monkeypatch)
    seen = turns(monkeypatch, [
        call("mark_dimension_unreadable", line_index=0, evidence_index=0, reason="标注不清晰"),
        call("read_extraction_source", file_id=1, page=1),
        call("mark_dimension_unreadable", line_index=0, evidence_index=0, reason="标注不清晰")])
    cache, _, _, _ = checkpoint()
    result, _, _ = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert "先重读" in json.loads(seen[1][-1]["content"])["error"]
    line = result.lines[0]
    assert line.height_mm is None and line.dimension_evidence[0].value is None
    assert line.dimension_evidence[0].text == "80mm"
    assert any("标注不清晰" in note for note in line.uncertainties)


def test_recovery_cannot_remove_products_or_read_unprovided_sources(monkeypatch):
    invalid_transport(monkeypatch)
    fixed = broken_line()
    fixed["height_mm"] = 80
    fixed["dimension_evidence"][0]["value"] = 80
    seen = turns(monkeypatch, [
        call("read_extraction_source", file_id=99, page=1),
        call("repair_extraction_line", line_index=0, line={**fixed, "product": "replacement"}),
        call("repair_extraction_line", line_index=0, line=fixed)])
    cache, _, _, _ = checkpoint()
    result, _, _ = deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert "未提供" in json.loads(seen[1][-1]["content"])["error"]
    assert "替换" in json.loads(seen[2][-1]["content"])["error"]
    assert result.lines[0].product == "panel"


def test_transport_failure_resumes_saved_repair_without_reextracting(monkeypatch):
    requests = invalid_transport(monkeypatch)
    count = 0
    fixed = broken_line()
    fixed["height_mm"] = 80
    fixed["dimension_evidence"][0]["value"] = 80

    def turn(messages, tools):
        nonlocal count
        count += 1
        if count == 1:
            return call("read_extraction_source", file_id=1, page=1), {"total_tokens": 7}
        if count == 2:
            raise deepseek.ModelFailure("network", diagnostics={"retryable": True})
        return call("repair_extraction_line", line_index=0, line=fixed), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    cache, snapshots, charges, _ = checkpoint()
    file = file_fixture()
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _checkpoint=cache)
    assert failure.value.diagnostics["retryable"] is True
    assert failure.value.usage["total_tokens"] == 27
    restored, _, new_charges, _ = checkpoint(snapshots[-1])
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert len(requests) == 2 and result.lines[0].height_mm == 80
    assert attempts == 1 and usage["total_tokens"] == 7
    assert sum(t.get("total_tokens", 0) for t in [*charges, *new_charges]) == 34


def test_repeated_calls_are_bounded_and_keep_recovery_state(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings(), "extraction_recovery_rounds", 4)
    invalid_transport(monkeypatch)
    turns(monkeypatch, [call("read_extraction_source", file_id=1, page=1)] * 4)
    cache, snapshots, _, events = checkpoint()
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file_fixture()], "", [], "", _checkpoint=cache)
    assert failure.value.diagnostics["recovery_rounds"] == 4
    assert snapshots[-1]["recoveries"]
    assert "相同调用" in events[-1]["result"]["error"]


def test_tool_schema_references_resolve_from_root():
    schema = next(t["function"]["parameters"] for t in recovery_tools()
                  if t["function"]["name"] == "repair_extraction_line")
    assert "DimensionEvidence" in schema["$defs"]
    assert schema["properties"]["line"]["properties"]["dimension_evidence"]["items"]["$ref"] == (
        "#/$defs/DimensionEvidence")


def test_worker_repairs_schema_and_continues_without_manual_restart(client, monkeypatch, redis_worker):
    from app.config import settings
    monkeypatch.setattr(settings(), "excel_native_first", False)
    job = enqueue_excel(client, monkeypatch)
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("tools"):
            line = {"product": "项目1", "source_item": "1", "evidence": [
                {"file_id": job["files"][0]["id"], "page": 1,
                 "sheet": "客户明细", "cell": "B2", "text": "项目1"}]}
            answer = call("repair_extraction_line", line_index=0, line=line)
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls",
                "message": answer}], "usage": {"total_tokens": 7}})
        raw = '{"lines":[{"price":99}]}' if len(seen) <= 2 else '{"lines":[]}'
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": raw}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    original = deepseek.agent_turn

    def turn(messages, tools):
        if tools[0]["function"]["name"] == "read_extraction_source":
            return original(messages, tools)
        raise deepseek.ModelFailure("test quote stop", diagnostics={"retryable": False})

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert len(current["requirements"]) == 4
    assert run["usage"]["prepared"] is True
    assert run["usage"]["total_tokens"] == run["usage"]["extraction_tokens"] == 57
    assert any(s["tool"] == "extraction_recovery_completed" for s in run["steps"])
