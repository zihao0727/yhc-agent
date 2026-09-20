import threading
from copy import deepcopy

from sqlalchemy import select

from app import agent_loop, agent_worker, deepseek
from app.config import settings
from app.models import AgentRun
from test_agent_loop import call, prepared, provider, seed, start
from test_estimate_pricing import estimated_job, plan
from test_redis_agent import enqueue, redis_worker
from test_requirements import save_lines


def test_auth_error_requires_intervention_without_retry(client, monkeypatch, redis_worker):
    provider(monkeypatch, [])
    job = enqueue(client, monkeypatch, 1)

    def denied(*args):
        raise deepseek.ModelFailure("认证失败", diagnostics={"status_code": 401, "retryable": False})

    monkeypatch.setattr(deepseek, "agent_turn", denied)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "needs_review"
    assert current["agent_runs"][0]["status"] == "waiting"
    assert current["agent_runs"][0]["usage"]["retry_at"] == 0
    assert not current["quotes"]


def test_response_failure_budget_survives_cycles(client, monkeypatch, redis_worker):
    provider(monkeypatch, [])
    job = enqueue(client, monkeypatch, 1)

    def truncated(*args):
        raise deepseek.AgentResponseFailure("truncated_response", {}, {"total_tokens": 1})

    monkeypatch.setattr(deepseek, "agent_turn", truncated)
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["usage"]["response_failures"] == 6
    assert run["status"] == "waiting"
    assert run["usage"]["total_tokens"] == 6
    assert not current["quotes"]


def test_transient_errors_backoff_and_stop(client, monkeypatch, redis_worker, test_db):
    provider(monkeypatch, [])
    monkeypatch.setattr(settings(), "agent_retry_seconds", 10)
    monkeypatch.setattr(settings(), "agent_max_retries", 3)
    job = enqueue(client, monkeypatch, 1)
    monkeypatch.setattr(settings(), "agent_retry_seconds", 10)

    def unavailable(*args):
        raise deepseek.ModelFailure("网络故障", diagnostics={"retryable": True})

    monkeypatch.setattr(deepseek, "agent_turn", unavailable)
    for expected in (1, 2, 3):
        agent_worker.run_pending_once(threading.Event())
        with test_db.begin() as db:
            run = db.scalar(select(AgentRun).where(AgentRun.job_id == job["id"]))
            if expected < 3:
                assert run.usage["retry_count"] == expected
                assert run.usage["retry_at"] > agent_loop.time.time()
                assert run.usage["phase"] == "retrying"
                run.usage = {**run.usage, "retry_at": 0}
            else:
                assert run.status == "waiting"


def test_worker_yields_and_schedules_another_job(client, monkeypatch, redis_worker):
    provider(monkeypatch, [])
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    first = enqueue(client, monkeypatch, 1)
    second = enqueue(client, monkeypatch, 1)
    seen = provider(monkeypatch, [
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    for job in (first, second):
        current = client.get(f'/api/jobs/{job["id"]}').json()
        assert current["requirements"][0]["estimate"]
        assert current["status"] == "processing"
        assert current["agent_runs"][0]["usage"]["phase"] == "queued"
    agent_worker.run_pending_once(threading.Event())
    assert client.get(f'/api/jobs/{first["id"]}').json()["status"] == "quoted"
    assert len(seen) == 3


def test_no_progress_guard_survives_yield(client, monkeypatch, redis_worker):
    seen = provider(monkeypatch, [call("read_requirement", line_id="line0") for _ in range(2)])
    monkeypatch.setattr(settings(), "agent_max_idle_rounds", 2)
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    job = enqueue(client, monkeypatch, 1)
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(seen) == 2
    assert current["agent_runs"][0]["status"] == "waiting"
    assert current["agent_runs"][0]["usage"]["no_progress_rounds"] == 2
    assert not current["quotes"]


def test_price_discovery_survives_worker_yield(client, price_input, monkeypatch, redis_worker):
    price = seed(client, price_input)
    provider(monkeypatch, [
        call("search_prices", query="测试"),
        call("evaluate_price", line_id="line0", price_id=price["id"], revision=price["revision"]),
    ])
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    job = enqueue(client, monkeypatch, 1)
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    steps = client.get(f'/api/jobs/{job["id"]}').json()["agent_runs"][0]["steps"]
    assert steps[1]["result"]["price"]["id"] == price["id"]
    assert "error" not in steps[1]["result"]


def test_progress_is_incremental_and_has_no_quote_history(client, monkeypatch):
    job = estimated_job(client, monkeypatch)
    run = job["agent_runs"][0]
    response = client.get(f'/api/jobs/{job["id"]}/progress?run_id={run["id"]}&after=1')
    assert response.status_code == 200
    progress = response.json()
    assert progress["run"]["offset"] == 1
    assert len(progress["run"]["steps"]) == 1
    assert progress["run"]["step_count"] == 2
    assert "quotes" not in progress and "requirements" not in progress
    assert progress["pricing_progress"]["priced_count"] == 1
    assert progress["pricing_progress"]["known_subtotal"] == "141.60"
    full = client.get(f'/api/jobs/{job["id"]}/agent-runs/{run["id"]}/steps/0')
    assert full.status_code == 200
    assert full.json()["arguments"]["estimate"]["scope"]
    assert client.get(f'/api/jobs/{job["id"] + 1}/agent-runs/{run["id"]}/steps/0').status_code == 404


def test_schema_error_returns_field_path_not_input(client, monkeypatch):
    job = prepared(client)
    # Explicit invalid extra field is rejected by StrictModel.
    provider(monkeypatch, [
        call("read_requirement", line_id="line1", forbidden="secret-value"),
        call("finish_pending_quote", reason="无法形成方案"),
    ])
    current = start(client, job).json()
    error = current["agent_runs"][0]["steps"][0]["result"]
    assert error["fields"][0]["path"] == "forbidden"
    assert "secret-value" not in str(error)


def test_explanation_does_not_change_requirements_or_stale_quote(client, monkeypatch):
    job = estimated_job(client, monkeypatch)
    before = deepcopy(job["requirements"])
    provider(monkeypatch, [call("respond", answer="当前总额为141.60元，包含材料和制作等暂估费用。", changes=[])])
    response = client.post(f'/api/jobs/{job["id"]}/conversation', json={
        "revision": job["revision"], "supplementary_text": "为什么这么贵？", "allow_external_processing": True})
    assert response.status_code == 200, response.text
    current = response.json()["job"]
    assert current["requirements"] == before
    assert current["revision"] == job["revision"]
    assert current["quotes"][0]["outdated"] is False
    assert len(current["messages"]) == len(job["messages"]) + 2
    assert not response.json()["changes"]


def test_patch_requires_confirmation_and_preserves_other_lines(client, monkeypatch):
    job = estimated_job(client, monkeypatch)
    rows = [*job["requirements"], {**deepcopy(job["requirements"][0]), "id": "untouched", "confirmed": True,
                                 "manual_unit_price": "99.00", "manual_price_note": "人工确认单价"}]
    job = save_lines(client, job, rows)
    change = {"line_id": "line1", "field": "quantity", "value": "5", "reason": "用户明确修改数量"}
    provider(monkeypatch, [call("respond", answer="拟将第一项数量改为5件，请确认。", changes=[change])])
    response = client.post(f'/api/jobs/{job["id"]}/conversation', json={
        "revision": job["revision"], "supplementary_text": "第一项改成5件", "allow_external_processing": True})
    assert response.status_code == 200, response.text
    proposed = response.json()
    assert proposed["job"]["requirements"] == job["requirements"]
    response = client.post(f'/api/jobs/{job["id"]}/changes', json={
        "revision": job["revision"], "changes": proposed["changes"]})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["requirements"][0]["quantity"] == 5
    assert current["requirements"][0]["estimate"]["costs"] == job["requirements"][0]["estimate"]["costs"]
    assert current["requirements"][1] == job["requirements"][1]
    assert current["quotes"][0]["outdated"]
    assert client.post(f'/api/jobs/{job["id"]}/changes', json={
        "revision": job["revision"], "changes": [change]}).status_code == 409


def test_patch_rejects_invalid_values_and_price_fields(client, monkeypatch):
    job = estimated_job(client, monkeypatch)
    base = {"line_id": "line1", "field": "quantity", "value": "-1", "reason": "测试无效修改"}
    for change in (base, {**base, "field": "manual_unit_price", "value": "1"},
                   {**base, "line_id": "nonexistent", "value": "5"}):
        assert client.post(f'/api/jobs/{job["id"]}/changes', json={
            "revision": job["revision"], "changes": [change]}).status_code == 422
    assert client.get(f'/api/jobs/{job["id"]}').json()["requirements"] == job["requirements"]
