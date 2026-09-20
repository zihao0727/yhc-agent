from sqlalchemy import select

from app.customer_files import storage_path
from app.models import AgentRun, AuditLog, CustomerFile, ExtractionRun, QuoteDraft, RequirementJob
from test_requirements import create_job


def test_delete_conversation_cleans_related_data(client, test_db):
    job = create_job(client)
    other = create_job(client)
    with test_db.begin() as db:
        file = db.scalar(select(CustomerFile).where(CustomerFile.job_id == job["id"]))
        directory = storage_path(file.storage_key)
        db.add(AgentRun(job_id=job["id"], model="test", status="succeeded"))
        db.add(ExtractionRun(job_id=job["id"], model="test", status="succeeded", input_revision=1))
        db.add(QuoteDraft(job_id=job["id"], version=1, job_revision=1, payload={}, terms="test"))
    assert directory.exists()
    response = client.request("DELETE", f'/api/jobs/{job["id"]}',
                              json={"revision": job["revision"], "reason": "删除测试会话"})
    assert response.status_code == 200, response.text
    assert not directory.exists()
    assert client.get(f'/api/jobs/{job["id"]}').status_code == 404
    assert client.get(f'/api/jobs/{other["id"]}').status_code == 200
    assert client.get("/api/jobs").json()["total"] == 1
    with test_db() as db:
        for model in (CustomerFile, AgentRun, ExtractionRun, QuoteDraft):
            assert db.scalar(select(model).where(model.job_id == job["id"])) is None
        assert db.scalar(select(AuditLog).where(AuditLog.entity == "requirement_jobs",
                                               AuditLog.entity_id == job["id"], AuditLog.action == "delete"))


def test_delete_conversation_guards(client, test_db):
    job = create_job(client)
    url = f'/api/jobs/{job["id"]}'
    payload = {"revision": job["revision"], "reason": "删除测试"}
    assert client.request("DELETE", url, json=payload,
                          headers={"X-Admin-Token": "invalid"}).status_code == 401
    assert client.request("DELETE", url, json={**payload, "revision": 99}).status_code == 409
    with test_db.begin() as db:
        db.get(RequirementJob, job["id"]).status = "processing"
    current = client.get(url).json()
    payload["revision"] = current["revision"]
    assert client.request("DELETE", url, json=payload).status_code == 409
    assert client.get(url).status_code == 200
    assert client.request("DELETE", "/api/jobs/999999", json=payload).status_code == 404
