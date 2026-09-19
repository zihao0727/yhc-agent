import json
from copy import deepcopy

import httpx
import pytest

from app import deepseek
from app.agent_loop import TOOLS
from test_agent_loop import call, prepared, start
from test_deepseek import configure_transport
from test_estimate_pricing import plan
from test_requirements import save_lines


def envelope(message=None, finish_reason="tool_calls"):
    return {"choices": [{"finish_reason": finish_reason, "message": message or call("calculate_quote")}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}}


@pytest.mark.parametrize("body,reason", [
    (envelope(finish_reason="length"), "truncated_response"),
    ({"choices": []}, "missing_choices"),
    (envelope({"tool_calls": "not-a-list"}), "invalid_tool_calls"),
    (envelope({"tool_calls": []}), "missing_tool_calls"),
    (envelope({"tool_calls": [None]}), "invalid_call"),
    (envelope({"tool_calls": [call("calculate_quote")["tool_calls"][0]] * 9}), "too_many_tool_calls"),
    (envelope({"tool_calls": [call("calculate_quote")["tool_calls"][0]] * 2}), "invalid_or_duplicate_call_id"),
    (envelope({"tool_calls": [{"id": "bad", "type": "function", "function": {
        "name": "complete_estimate", "arguments": '{"line_id":"x","estimate":'}}]}), "invalid_arguments_json"),
    (envelope({"tool_calls": [{"id": "bad", "type": "function", "function": {
        "name": "calculate_quote", "arguments": "[]"}}]}), "arguments_not_object"),
    (envelope({"tool_calls": [{"id": "bad", "type": "function", "function": {
        "name": "calculate_quote", "arguments": '{"quantity": NaN}'}}]}), "invalid_arguments_json"),
])
def test_response_failures_have_bounded_diagnostics(body, reason):
    with pytest.raises(deepseek.AgentResponseFailure) as exc:
        deepseek.parse_agent_response(body)
    assert exc.value.diagnostics["reason"] == reason
    assert "arguments" not in exc.value.diagnostics
    if "usage" in body:
        assert exc.value.usage["total_tokens"] == 17


def test_compatible_object_arguments_are_serialized_without_guessing():
    message = call("complete_estimate", line_id="line1", estimate=plan())
    original = json.loads(message["tool_calls"][0]["function"]["arguments"])
    message["tool_calls"][0]["function"]["arguments"] = original
    normalized, _ = deepseek.parse_agent_response(envelope(message))
    assert json.loads(normalized["tool_calls"][0]["function"]["arguments"]) == original


def test_wire_truncation_is_recoverable_but_http_auth_failure_is_not(monkeypatch):
    configure_transport(monkeypatch, lambda request: httpx.Response(200, json=envelope(finish_reason="length")))
    with pytest.raises(deepseek.AgentResponseFailure) as exc:
        deepseek.agent_turn([], TOOLS)
    assert exc.value.diagnostics["finish_reason"] == "length"
    assert exc.value.usage["total_tokens"] == 17


def test_http_failure_is_not_a_protocol_retry(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"error": "secret"})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as exc:
        deepseek.agent_turn([], TOOLS)
    assert not isinstance(exc.value, deepseek.AgentResponseFailure)
    assert len(requests) == 1
    assert "secret" not in str(exc.value)


def recovery_job(client):
    job = prepared(client)
    job["requirements"][0]["quantity"] = None
    return save_lines(client, job, job["requirements"])


def test_recovery_preserves_saved_estimate_and_counts_failed_usage(client, monkeypatch):
    job = recovery_job(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-test-key")
    seen = []

    def turn(messages, tools):
        seen.append(deepcopy(messages))
        if len(seen) == 1:
            return call("complete_estimate", line_id="line1", estimate=plan()), {"total_tokens": 5}
        if len(seen) == 2:
            return deepseek.parse_agent_response(envelope(finish_reason="length"))
        assert "本轮只调用一个工具" in messages[-1]["content"]
        assert json.loads(messages[1]["content"])["requirements"][0]["estimate"]
        return call("finish_quote"), {"total_tokens": 5}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert result["quotes"][0]["payload"]["total"] == "141.60"
    assert [step["tool"] for step in run["steps"]] == ["complete_estimate", "response_recovery", "finish_quote"]
    assert run["usage"]["total_tokens"] == 27 and run["usage"]["agent_tokens"] == 27
    assert run["usage"]["response_failures"] == 1
    assert run["steps"][1]["result"]["executed_calls"] == 0


@pytest.mark.parametrize("extra_line", [True, False])
def test_repeated_failure_keeps_progress_without_finishing_for_model(client, monkeypatch, extra_line):
    job = recovery_job(client)
    if extra_line:
        job = save_lines(client, job, [*job["requirements"], {**job["requirements"][0], "id": "line2"}])
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-test-key")
    count = 0

    def turn(messages, tools):
        nonlocal count
        count += 1
        if count == 1:
            return call("complete_estimate", line_id="line1", estimate=plan()), {}
        # Even a complete call must not run when the provider marks the response truncated.
        bad = call("complete_estimate", line_id="line1", estimate={
            **plan(), "assumptions": [{"field": "quantity", "value": "999", "reason": "must not execute"}]})
        return deepseek.parse_agent_response(envelope(bad, finish_reason="length"))

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert count == 4
    assert run["status"] == "waiting", run
    assert run["usage"]["agent_tokens"] == 51
    assert result["requirements"][0]["estimate"]["assumptions"][0]["value"] == "2"
    assert result["quotes"][0]["payload"]["known_subtotal"] == "141.60"
    assert result["quotes"][0]["payload"]["total"] is None
    assert all(step["tool"] != "finish_quote" for step in run["steps"])
