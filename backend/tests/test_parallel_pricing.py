import json
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import agent_checkpoints, agent_loop, agent_worker, deepseek
from app.config import settings
from app.models import AgentRun, RequirementJob
from app import agent_work_state as work, parallel_pricing
from test_agent_loop import call, start
from test_redis_agent import enqueue, redis_worker  # noqa: F401
from test_pricing_proposals import order  # noqa: F401


def proposal(quantity=2):
    return {"strategy": "area", "scope": "Manufacturing, provisional rates for review",
            "parameters": [{"target": "item", "values": {"quantity": quantity},
                            "basis": "assumption", "reason": "Provisional quantity for review"}],
            "costs": [
                {"label": "Material", "kind": "material", "measurement": "item_area",
                 "rate": 100, "unit": "m2", "reason": "Provisional material rate for review"},
                {"label": "Processing", "kind": "process", "measurement": "item_area",
                 "rate": 20, "unit": "m2", "reason": "Provisional processing rate for review"}]}


def active(messages):
    return json.loads(messages[1]["content"])["active_requirement"]

def test_inference_time_is_accounted_once_even_when_provider_fails(monkeypatch):
    from types import SimpleNamespace
    from app.requirements_schemas import RequirementLine
    job = SimpleNamespace(requirements=[RequirementLine(id="one", product="Panel").model_dump(mode="json")])
    run = SimpleNamespace(usage={})
    turn = parallel_pricing.Turn("one", "", 0, [], [], {}, False)
    clock = iter([10, 17])
    monkeypatch.setattr(parallel_pricing.time, "monotonic", lambda: next(clock))
    def fail(*args):
        raise RuntimeError("Provider failure")
    monkeypatch.setattr(deepseek, "agent_turn", fail)
    with pytest.raises(RuntimeError):
        parallel_pricing.model_turn(turn)
    parallel_pricing.account_time(job, run, turn)
    parallel_pricing.account_time(job, run, turn)
    assert work.line_state(job, run, "one")["pricing_seconds"] == 7


def test_prepared_source_packet_avoids_redundant_reads_without_blocking_original_access(order):
    db, job, run = order
    requests = parallel_pricing.PricingRequests(4)
    try:
        turn = requests.prepare(db, job, run, job.requirements[0], set(), None)
        assert turn.context["pricing_packet"]["sources"]
        names = {tool["function"]["name"] for tool in turn.tools}
        assert "read_requirement" not in names and "list_sources" not in names
        assert {"read_source", "inspect_source", "submit_pricing_plan"} <= names
    finally:
        requests.pool.shutdown()


def run_workers(count=8):
    for _ in range(count):
        agent_worker.run_pending_once(threading.Event())


def test_deterministic_request_preparation_failure_pauses_with_diagnostics(
        client, monkeypatch, redis_worker):
    calls = []
    monkeypatch.setattr(deepseek, "agent_turn", lambda *args: calls.append(args))

    def invalid_evidence(*args, **kwargs):
        raise ValueError("Invalid source contract")

    monkeypatch.setattr(parallel_pricing.PricingRequests, "prepare", invalid_evidence)
    job = enqueue(client, monkeypatch, 2)
    run_workers(3)
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting"
    assert not calls
    failures = [step for step in run["steps"] if step["tool"] == "execution_failure"]
    assert len(failures) == 1
    assert failures[0]["result"]["exception_type"] == "ValueError"
    assert failures[0]["result"]["diagnostic_id"] in run["message"]
    assert run["usage"]["retry_at"] == 0


@pytest.fixture(autouse=True)
def enable_parallel(monkeypatch):
    monkeypatch.setattr(settings(), "agent_pricing_concurrency", 2)
    monkeypatch.setattr(settings(), "agent_slice_rounds", 20)
    monkeypatch.setattr(settings(), "agent_slice_seconds", 600)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only-key")


def test_requests_overlap_results_persist_and_usage_is_per_item(
        client, monkeypatch, redis_worker, test_db):
    barrier = threading.Barrier(2)
    counts = Counter()
    lock = threading.Lock()
    coordinator = threading.get_ident()
    provider_threads = []

    def turn(messages, tools):
        row = active(messages)
        if row is None:
            return call("finish_quote"), {"total_tokens": 5}
        with lock:
            counts[row["id"]] += 1
            provider_threads.append(threading.get_ident())
        assert all(t["function"]["name"] != "finish_quote" for t in tools)
        barrier.wait(timeout=5)
        # Force the second item's result to be saved first.
        if row["id"] == "line0":
            time.sleep(0.1)
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 13}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert counts == {"line0": 1, "line1": 1}
    assert all(thread != coordinator for thread in provider_threads)
    assert current["quotes"][0]["status"] == "draft"
    assert all(raw["estimate"] for raw in current["requirements"])
    assert run["usage"]["agent_tokens"] == 31
    assert run["usage"]["inflight_line_ids"] == []
    assert agent_checkpoints.read_checkpoint(job["id"])["priced_count"] == 2
    saved = [s for s in run["steps"] if s["tool"] == "submit_pricing_plan"]
    assert [s["line_id"] for s in saved] == ["line1", "line0"]
    with test_db() as db:
        usage = db.get(AgentRun, run["id"]).usage
        assert usage["work_state"]["line0"]["line_tokens"] == 13
        assert usage["work_state"]["line1"]["line_tokens"] == 13


def test_free_slot_starts_next_item_while_slow_peer_is_still_running(client, monkeypatch, redis_worker):
    next_started = threading.Event()
    seen = Counter()

    def turn(messages, tools):
        row = active(messages)
        if row is None:
            return call("finish_quote"), {}
        seen[row["id"]] += 1
        if row["id"] == "line0":
            assert next_started.wait(timeout=5), "A free slot waited for the slow peer"
        elif row["id"] == "line2":
            next_started.set()
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 3)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert seen == {"line0": 1, "line1": 1, "line2": 1}
    assert current["agent_runs"][0]["usage"]["agent_tokens"] == 21


@pytest.mark.parametrize("concurrency", [2, 4])
def test_concurrency_cap_and_multiple_waves_preserve_every_item(
        client, monkeypatch, redis_worker, concurrency):
    monkeypatch.setattr(settings(), "agent_pricing_concurrency", concurrency)
    in_progress, maximum = 0, 0
    lock = threading.Lock()
    seen = Counter()

    def turn(messages, tools):
        nonlocal in_progress, maximum
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        with lock:
            in_progress += 1
            maximum = max(maximum, in_progress)
            seen[row["id"]] += 1
        time.sleep(0.05)
        with lock:
            in_progress -= 1
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 5)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert maximum == concurrency
    assert seen == {f"line{i}": 1 for i in range(5)}
    assert all(raw["estimate"] for raw in current["requirements"])


def test_failed_early_items_do_not_starve_unvisited_projects(client, monkeypatch, redis_worker):
    seen = []
    counts = Counter()
    lock = threading.Lock()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        with lock:
            counts[row["id"]] += 1
            seen.append(row["id"])
            count = counts[row["id"]]
        plan = proposal()
        if row["id"] in ("line0", "line1") and count == 1:
            plan["costs"][0]["measurement"] = "component_area"
        return call("submit_pricing_plan", line_id=row["id"], plan=plan), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 5)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    # The final unvisited item shares a wave with a repair; either thread can start first.
    assert set(seen[:6]) == {f"line{i}" for i in range(5)}
    assert counts == {"line0": 2, "line1": 2, "line2": 1, "line3": 1, "line4": 1}


def test_research_can_finish_before_every_item_takes_its_first_turn(client, monkeypatch, redis_worker):
    counts = Counter()
    seen = []
    lock = threading.Lock()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        with lock:
            counts[row["id"]] += 1
            seen.append(row["id"])
            count = counts[row["id"]]
        if count == 1:
            return call("read_requirement", line_id=row["id"]), {}
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 8)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert len(set(seen[:4])) < 4
    assert counts == {f"line{i}": 2 for i in range(8)}


@pytest.mark.parametrize("wrong_tool", ["other_line", "finish_quote"])
def test_cross_item_mutation_and_early_finalization_are_rejected_as_a_batch(
        client, monkeypatch, redis_worker, wrong_tool):
    attempts = Counter()
    barrier = threading.Barrier(2)

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        line_id = row["id"]
        attempts[line_id] += 1
        if attempts[line_id] == 1:
            barrier.wait(timeout=5)
        if line_id == "line0" and attempts[line_id] == 1:
            valid = call("submit_pricing_plan", line_id=line_id, plan=proposal())
            wrong = (call("submit_pricing_plan", line_id="line1", plan=proposal(quantity=999))
                     if wrong_tool == "other_line" else call("finish_quote"))
            wrong["tool_calls"][0]["id"] = "wrong-call"
            valid["tool_calls"].extend(wrong["tool_calls"])
            return valid, {}
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded", run
    rejected = [s for s in run["steps"] if s["tool"] == "parallel_scope_rejected"]
    assert len(rejected) == 1 and rejected[0]["result"]["executed_calls"] == 0
    saves = [s for s in run["steps"] if s["tool"] == "submit_pricing_plan"]
    assert len(saves) == 2
    assert attempts == {"line0": 2, "line1": 1}
    assert all(raw["estimate"]["assumptions"][0]["value"] == "2"
               for raw in current["requirements"])


def test_network_retry_is_isolated_and_completed_peer_is_not_repriced(
        client, monkeypatch, redis_worker):
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {"total_tokens": 5}
        line_id = row["id"]
        counts[line_id] += 1
        if line_id == "line0" and counts[line_id] == 1:
            raise deepseek.ModelFailure("test timeout", usage={"total_tokens": 3},
                                        diagnostics={"retryable": True})
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert counts == {"line0": 2, "line1": 1}
    assert run["usage"]["agent_tokens"] == 22
    assert any(s["tool"] == "request_retry" and s["line_id"] == "line0" for s in run["steps"])


def test_malformed_responses_defer_only_the_affected_item(
        client, monkeypatch, redis_worker):
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        assert row
        counts[row["id"]] += 1
        if row["id"] == "line0":
            raise deepseek.AgentResponseFailure("truncated_response", {}, {"total_tokens": 11})
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting", run
    assert counts == {"line0": 3, "line1": 1}
    assert run["usage"]["agent_tokens"] == 40
    assert current["quotes"][0]["payload"]["total"] is None
    assert current["quotes"][0]["payload"]["priced_count"] == 1
    assert run["usage"]["deferred_requirements"][0]["line_id"] == "line0"


def test_user_changes_during_inference_discard_stale_result_and_preserve_other_item(
        client, monkeypatch, redis_worker, test_db):
    started, release = threading.Event(), threading.Event()
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {"total_tokens": 5}
        line_id = row["id"]
        counts[line_id] += 1
        if line_id == "line0" and counts[line_id] == 1:
            started.set()
            assert release.wait(timeout=10)
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(agent_worker.run_pending_once, threading.Event())
        assert started.wait(timeout=10)
        with test_db.begin() as db:
            current = db.get(RequirementJob, job["id"])
            current.requirements = [
                {**raw, "width_mm": "600"} if raw["id"] == "line0" else raw
                for raw in current.requirements]
        release.set()
        worker.result(timeout=20)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert counts == {"line0": 2, "line1": 1}
    assert current["requirements"][0]["width_mm"] == "600"
    assert run["usage"]["agent_tokens"] == 26
    discarded = [s for s in run["steps"] if s["tool"] == "discard_inflight_response"]
    assert len(discarded) == 1 and discarded[0]["line_id"] == "line0"


def test_stop_retains_saved_peer_accounts_inflight_usage_and_resume_skips_saved_item(
        client, monkeypatch, redis_worker, test_db):
    started, release = threading.Event(), threading.Event()
    counts = Counter()
    job = enqueue(client, monkeypatch, 2)
    checkpoint_saved = threading.Event()
    original_save = agent_checkpoints.save_checkpoint

    def save(db, current, run):
        result = original_save(db, current, run)
        if result["priced_count"] == 1:
            checkpoint_saved.set()
        return result

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {"total_tokens": 5}
        line_id = row["id"]
        counts[line_id] += 1
        if line_id == "line0" and counts[line_id] == 1:
            started.set()
            assert release.wait(timeout=10)
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(agent_checkpoints, "save_checkpoint", save)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(agent_worker.run_pending_once, threading.Event())
        assert started.wait(timeout=10) and checkpoint_saved.wait(timeout=10)
        current = client.get(f'/api/jobs/{job["id"]}').json()
        response = client.post(f'/api/jobs/{job["id"]}/agent/stop', json={
            "revision": current["revision"], "reason": "Stop test"})
        assert response.status_code == 200
        release.set()
        worker.result(timeout=20)
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "cancelled"
    assert current["agent_runs"][0]["usage"]["agent_tokens"] == 14
    assert not current["requirements"][0]["estimate"]
    assert current["requirements"][1]["estimate"]
    start(client, current, intent="resume")
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert counts == {"line0": 2, "line1": 1}


def test_settings_bound_concurrency():
    from app.config import Settings
    from pydantic import ValidationError
    assert Settings(_env_file=None).agent_pricing_concurrency == 4
    for value in (0, 17):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, agent_pricing_concurrency=value)


def test_rate_limit_backs_off_only_failed_item_and_uses_saved_peer(
        client, monkeypatch, redis_worker, test_db):
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        line_id = row["id"]
        counts[line_id] += 1
        if line_id == "line0" and counts[line_id] == 1:
            raise deepseek.ModelFailure("limited", diagnostics={"retryable": True, "status_code": 429})
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "processing"
    assert counts == {"line0": 1, "line1": 1}
    assert current["requirements"][1]["estimate"]
    retry = next(s["result"] for s in run["steps"] if s["tool"] == "request_retry")
    assert retry["retry_seconds"] >= 1
    assert run["usage"]["retry_at"] > time.time()
    # Advance the persisted clock instead of sleeping in an integration test.
    with test_db.begin() as db:
        saved_job, saved_run = db.get(RequirementJob, job["id"]), db.get(AgentRun, run["id"])
        work.update_state(saved_job, saved_run, "line0", retry_at=0)
        saved_run.usage = {**saved_run.usage, "retry_at": 0}
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert counts == {"line0": 2, "line1": 1}


def test_slice_drains_the_current_batch_and_resumes_without_repricing(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        counts[row["id"]] += 1
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 4)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "processing"
    assert sum(bool(raw["estimate"]) for raw in current["requirements"]) == 2
    assert current["agent_runs"][0]["usage"]["inflight_line_ids"] == []
    assert not current["quotes"]
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert counts == {f"line{i}": 1 for i in range(4)}


def test_total_token_cap_stops_the_next_batch_and_accounts_sent_requests(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "agent_max_total_tokens", 10)
    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        assert row
        counts[row["id"]] += 1
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 12}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 4)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting"
    assert counts == {"line0": 1, "line1": 1}
    assert run["usage"]["agent_tokens"] == 24
    assert sum(bool(raw["estimate"]) for raw in current["requirements"]) == 2
    assert run["steps"][-1]["tool"] == "execution_limit"
    assert not current["quotes"]


def test_source_images_stay_with_their_item_and_survive_malformed_reply(
        client, monkeypatch, redis_worker, test_db):
    job = enqueue(client, monkeypatch, 2)
    run_id = job["agent_runs"][0]["id"]
    with test_db.begin() as db:
        saved_job, run = db.get(RequirementJob, job["id"]), db.get(AgentRun, run_id)
        for line_id in ("line0", "line1"):
            work.update_state(saved_job, run, line_id, source_view_pending=[{
                "file_id": job["files"][0]["id"], "page": 1,
                "region": [0, 0, 1, 1], "purpose": "read"}])

    def inspect(db, current, run, name, args, discovered, interrupted):
        assert name == "view_source_region"
        line_id = run.usage["active_line_id"]
        return {"_source_images": [{"type": "image_url", "image_url": {"url": line_id}}]}

    counts = Counter()

    def turn(messages, tools):
        row = active(messages)
        if not row:
            return call("finish_quote"), {}
        line_id = row["id"]
        counts[line_id] += 1
        images = [part["image_url"]["url"] for message in messages
                  if isinstance(message.get("content"), list)
                  for part in message["content"] if part["type"] == "image_url"]
        assert images and set(images) == {line_id}
        if line_id == "line0" and counts[line_id] == 1:
            raise deepseek.AgentResponseFailure("truncated_response", {})
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {}

    monkeypatch.setattr(parallel_pricing, "execute_tool", inspect)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert counts == {"line0": 2, "line1": 1}


def test_fatal_provider_error_pauses_order_and_keeps_saved_peer(
        client, monkeypatch, redis_worker):
    checkpoint_saved = threading.Event()
    real_save = agent_checkpoints.save_checkpoint

    def save(db, job, run):
        result = real_save(db, job, run)
        if result["priced_count"] == 1:
            checkpoint_saved.set()
        return result

    def turn(messages, tools):
        row = active(messages)
        assert row
        if row["id"] == "line0":
            assert checkpoint_saved.wait(timeout=10)
            raise deepseek.ModelFailure("test credentials", usage={"total_tokens": 3},
                                        diagnostics={"retryable": False, "status_code": 401})
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    monkeypatch.setattr(agent_checkpoints, "save_checkpoint", save)
    job = enqueue(client, monkeypatch, 2)
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting"
    assert current["requirements"][1]["estimate"]
    assert not current["requirements"][0]["estimate"]
    assert run["usage"]["agent_tokens"] == 10
    assert not any(s["tool"] == "request_retry" for s in run["steps"])
    assert not current["quotes"]


def test_lease_loss_never_executes_old_reply_or_clears_new_workers_inflight_state(
        client, monkeypatch, redis_worker, test_db):
    started, release, saved = threading.Event(), threading.Event(), threading.Event()
    real_save = agent_checkpoints.save_checkpoint

    def save(db, current, run):
        result = real_save(db, current, run)
        if result["priced_count"] == 1:
            saved.set()
        return result

    def turn(messages, tools):
        row = active(messages)
        assert row
        if row["id"] == "line0":
            started.set()
            assert release.wait(timeout=10)
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {"total_tokens": 7}

    monkeypatch.setattr(agent_checkpoints, "save_checkpoint", save)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    run_id = job["agent_runs"][0]["id"]
    lease_key = f"{settings().redis_prefix}:lease:{run_id}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(agent_worker.run_pending_once, threading.Event())
        assert started.wait(timeout=10) and saved.wait(timeout=10)
        redis_worker.set(lease_key, "new-worker", ex=30)
        with test_db.begin() as db:
            run = db.get(AgentRun, run_id)
            run.usage = {**run.usage, "pricing_request_owner": "new-worker",
                         "inflight_line_ids": ["new-request"], "active_line_id": "new-request"}
        release.set()
        worker.result(timeout=20)
    with test_db() as db:
        current, run = db.get(RequirementJob, job["id"]), db.get(AgentRun, run_id)
        assert run.status == "processing"
        assert current.requirements[1]["estimate"]
        assert not current.requirements[0]["estimate"]
        assert run.usage["agent_tokens"] == 14
        assert run.usage["inflight_line_ids"] == ["new-request"]
        assert run.usage["active_line_id"] == "new-request"
        assert run.usage["pricing_request_owner"] == "new-worker"
    assert redis_worker.get(lease_key) == "new-worker"
