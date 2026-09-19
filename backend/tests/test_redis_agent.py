import json
import threading
import uuid

import pytest
from sqlalchemy import select

from app import agent_checkpoints, agent_loop, agent_worker
from app.config import settings
from app.models import AgentRun, RequirementJob
from test_agent_loop import call, prepared, provider, start
from test_estimate_pricing import plan
from test_requirements import save_lines


@pytest.fixture
def redis_worker(monkeypatch, test_db):
    prefix = "firefly:test:" + uuid.uuid4().hex
    monkeypatch.setattr(settings(), "redis_prefix", prefix)
    monkeypatch.setattr(settings(), "agent_retry_seconds", 0)
    monkeypatch.setattr(agent_worker, "SessionLocal", test_db)
    client = agent_checkpoints.redis_client()
    client.ping()
    yield client
    keys = list(client.scan_iter(match=prefix + ":*"))
    if keys:
        client.delete(*keys)


def enqueue(client, monkeypatch, count=3):
    job = prepared(client)
    lines = [{**job["requirements"][0], "id": f"line{i}", "quantity": None}
             for i in range(count)]
    job = save_lines(client, job, lines)
    # TestClient's lifespan has already started in foreground test mode.
    monkeypatch.setattr(settings(), "agent_background", True)
    return start(client, job, intent="resume").json()


def test_each_item_persisted_in_redis_and_only_full_order_finishes(
        client, monkeypatch, redis_worker, test_db):
    monkeypatch.setattr(agent_loop, "MAX_ROUNDS", 1)
    monkeypatch.setattr(agent_loop, "MAX_SEGMENTS", 1)
    seen = provider(monkeypatch, [
        *[call("complete_estimate", line_id=f"line{i}", estimate=plan()) for i in range(3)],
        call("finish_quote"),
    ])
    job = enqueue(client, monkeypatch)
    assert not seen
    assert job["status"] == "processing" and not job["quotes"]
    run_id = job["agent_runs"][0]["id"]
    for i in range(3):
        agent_worker.run_pending_once(threading.Event())
        progress = agent_checkpoints.read_checkpoint(job["id"])
        assert progress["priced_count"] == i + 1
        key = redis_worker.get(agent_checkpoints.checkpoint_key(job["id"]))
        saved = json.loads(redis_worker.hget(key, f"line:line{i}"))
        assert saved["amount"] == "141.60" and saved["estimate_review"]
        current = client.get(f'/api/jobs/{job["id"]}').json()
        assert current["status"] == "processing" and not current["quotes"]
        assert current["agent_runs"][0]["id"] == run_id
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "quoted"
    assert current["quotes"][0]["payload"]["total"] == "424.80"
    assert current["quotes"][0]["status"] == "draft"
    assert len(current["quotes"][0]["payload"]["review_items"]) == 15
    assert len(current["agent_runs"]) == 1
    assert client.get(f'/api/quotes/{current["quotes"][0]["id"]}/export').status_code == 409
    progress = json.loads(seen[1][1]["content"])["quote_progress"]
    assert json.loads(seen[1][1]["content"])["active_requirement"]["id"] == "line1"
    assert progress["priced_count"] == 1 and progress["pending_count"] == 2
    assert progress["known_subtotal"] == "141.60"


def test_redis_failure_after_sql_commit_resumes_without_repricing_saved_item(
        client, monkeypatch, redis_worker, test_db):
    provider(monkeypatch, [
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    job = enqueue(client, monkeypatch, 1)
    real_save = agent_checkpoints.save_checkpoint

    def unavailable(*args):
        raise ConnectionError("test Redis failure after committed estimate")

    with test_db() as db:
        run = db.scalar(select(AgentRun))
        monkeypatch.setattr(agent_checkpoints, "save_checkpoint", unavailable)
        agent_loop.run_agent(db, job["id"], run.id, intent="resume")
        db.expire_all()
        assert db.get(RequirementJob, job["id"]).requirements[0]["estimate"]
        assert db.get(AgentRun, run.id).status == "processing"
    monkeypatch.setattr(agent_checkpoints, "save_checkpoint", real_save)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "141.60"
    assert agent_checkpoints.read_checkpoint(job["id"])["complete"]
    assert sum(step["tool"] == "complete_estimate" for step in current["agent_runs"][0]["steps"]) == 1


def test_redis_lease_prevents_duplicate_worker_and_manual_stop_is_respected(
        client, monkeypatch, redis_worker):
    seen = provider(monkeypatch, [])
    job = enqueue(client, monkeypatch, 1)
    run_id = job["agent_runs"][0]["id"]
    key = f"{settings().redis_prefix}:lease:{run_id}"
    redis_worker.set(key, "another-worker", ex=30)
    agent_worker.run_pending_once(threading.Event())
    assert not seen
    redis_worker.delete(key)
    stopped = client.post(f'/api/jobs/{job["id"]}/agent/stop',
                          json={"revision": job["revision"], "reason": "停止后台报价"})
    assert stopped.status_code == 200
    agent_worker.run_pending_once(threading.Event())
    assert not seen


def test_context_compactions_do_not_exhaust_execution_segments(client, monkeypatch):
    job = prepared(client)
    monkeypatch.setattr(agent_loop, "MAX_CONTEXT_CHARS", 1)
    monkeypatch.setattr(agent_loop, "MAX_SEGMENTS", 1)
    provider(monkeypatch, [
        *[call("read_requirement", line_id="line1") for _ in range(10)],
        call("complete_estimate", line_id="line1", estimate=plan()),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    assert result["agent_runs"][0]["usage"]["context_compactions"] >= 10


def test_pending_tool_cannot_terminate_durable_quote(client, monkeypatch, redis_worker):
    provider(monkeypatch, [
        call("finish_pending_quote", reason="模型尝试提前结束"),
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    job = enqueue(client, monkeypatch, 1)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "processing" and not current["quotes"]
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["quotes"][0]["payload"]["total"] == "141.60"


def test_older_checkpoint_cannot_replace_newer_run(redis_worker):
    key = f"{settings().redis_prefix}:pointer"
    newer = key + ":new"
    older = key + ":old"
    redis_worker.hset(newer, "metadata", json.dumps({"revision": 5, "run_id": 10}))
    redis_worker.hset(older, "metadata", json.dumps({"revision": 6, "run_id": 9}))
    assert redis_worker.eval(agent_checkpoints.PUBLISH, 2, key, newer, 5, 10) == 1
    assert redis_worker.eval(agent_checkpoints.PUBLISH, 2, key, older, 6, 9) == 0
    assert redis_worker.get(key) == newer
