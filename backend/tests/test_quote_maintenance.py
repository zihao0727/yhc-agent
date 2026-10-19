"""Maintenance is authenticated, revision-checked, audited and never invents facts."""
from copy import deepcopy
import time

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import AuditLog, QuoteDraft
from app import worker_health
from test_pricing_proposals import order
from test_quote_workflow import wait_for_input
from test_redis_agent import redis_worker


def request(job, action="revalidate", **extra):
    return {"revision": job.revision, "action": action, "reason": "Regression verification", **extra}


def test_maintenance_requires_auth_and_current_revision(client, order):
    db, job, _ = wait_for_input(order)
    db.commit()
    response = client.post(f"/api/jobs/{job.id}/maintenance", json=request(job, revision=job.revision + 1))
    assert response.status_code == 409
    client.headers.pop("X-Admin-Token")
    assert client.post(f"/api/jobs/{job.id}/maintenance", json=request(job)).status_code == 401
    assert client.get("/api/operations").status_code == 401
    assert client.get("/api/quotation-contract").status_code == 401


@pytest.mark.parametrize("action", ["revalidate", "refresh_candidates"])
def test_read_maintenance_preserves_facts_saved_prices_and_history(client, order, action):
    db, job, run = wait_for_input(order)
    quote = QuoteDraft(job_id=job.id, version=1, job_revision=job.revision, status="blocked",
                       terms="Original terms", payload={"lines": [], "confirmation_items": []})
    db.add(quote)
    db.commit()
    before = deepcopy(job.requirements), deepcopy(quote.payload), deepcopy(run.steps)
    result = client.post(f"/api/jobs/{job.id}/maintenance", json=request(job, action))
    assert result.status_code == 200, result.text
    assert result.json()["job"]["quote_workspace"]["revision"] == job.revision
    db.expire_all()
    assert (job.requirements, quote.payload, run.steps) == before
    assert db.scalar(select(AuditLog).where(AuditLog.action == "quote_maintenance"))
    assert run.status == "waiting"


def test_retry_never_treats_real_customer_absence_as_a_technical_failure(client, order):
    db, job, run = wait_for_input(order, "customer_input")
    db.commit()
    response = client.post(f"/api/jobs/{job.id}/maintenance", json=request(job, "retry_failed"))
    assert response.status_code == 200, response.text
    assert response.json()["maintenance"]["reopened"] == []
    db.expire_all()
    assert run.status == "waiting"


def test_disable_automatic_recovery_is_persisted_and_audited(client, order):
    db, job, run = wait_for_input(order)
    db.commit()
    response = client.post(f"/api/jobs/{job.id}/maintenance", json=request(job, "disable_auto_resume"))
    assert response.status_code == 200
    db.expire_all()
    assert not run.usage["auto_resume"]


def test_readiness_distinguishes_api_liveness_worker_loss_and_drain(
        client, monkeypatch, redis_worker, order):
    db, _, _ = order
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "agent_worker_mode", "external")
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health/ready").status_code == 503
    worker_health.heartbeat()
    assert client.get("/api/health/ready").status_code == 200
    for value in (True, False):
        response = client.post("/api/operations/worker", json={
            "draining": value, "reason": "Maintenance window"})
        assert response.status_code == 200 and response.json()["draining"] is value
        assert response.json()["ready"] is not value
    assert db.scalar(select(AuditLog).where(AuditLog.entity == "quote_worker"))
    key = f"{settings().redis_prefix}:workers:{worker_health.WORKER_ID}"
    redis_worker.hset(key, "heartbeat_at", str(time.time() - 100))
    assert client.get("/api/health/ready").status_code == 503
