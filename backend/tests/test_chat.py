import json

import httpx
import pytest

from app import deepseek
from app.models import RequirementJob
from app.quote_engine import calculate_line
from app.requirements_schemas import RequirementLine
from test_deepseek import configure_transport
from test_requirements import create_job, pdf_bytes, png_bytes, save_lines, line_input
from test_agent_loop import seed


def test_text_conversation_without_upload(client):
    response = client.post("/api/jobs", data={"title": "文字需求", "brief": "铝牌300x120mm，2件"})
    assert response.status_code == 201
    assert response.json()["files"] == []
    assert response.json()["brief"] == "铝牌300x120mm，2件"
    assert client.post("/api/jobs", data={"title": "空需求", "brief": " "}).status_code == 422


def test_text_extraction_sources_verified(monkeypatch, client, price_input, test_db):
    text = "测试产品 铝3mm 300x120mm 英文 2件"
    price = seed(client, price_input)

    def handler(request):
        body = json.loads(request.content)
        assert len(body["messages"][1]["content"]) == 1
        result = {"lines": [{"product": "测试产品", "material": "铝", "thickness_mm": 3,
                             "width_mm": 300, "height_mm": 120, "language": "en", "quantity": 2,
                             "text_evidence": [text]}]}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(result)}}]})
    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([], text, [], "")
    line = RequirementLine(id="text-line", **result.lines[0].model_dump(),
                           selected_price_id=price["id"], selected_price_revision=price["revision"],
                           price_review_note="Agent 自动核对")
    with test_db() as db:
        assert calculate_line(db, line, automatic=True)["amount"] == "21.60"


def test_fabricated_text_source_rejected(monkeypatch):
    def handler(request):
        result = {"lines": [{"product": "铝牌", "text_evidence": ["客户确认制作1000件"]}]}
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(result)}}]})
    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure):
        deepseek.extract([], "制作2件", [], "")


def test_append_files_resets_old_requirement_and_validates_revision(client):
    job = save_lines(client, create_job(client), [line_input()])
    response = client.post(f'/api/jobs/{job["id"]}/files',
                           data={"revision": job["revision"]},
                           files={"files": ("补充.pdf", pdf_bytes())})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["files"]) == 2 and result["requirements"] == []
    assert result["revision"] > job["revision"]
    assert result["messages"][-1]["file_ids"] == [result["files"][-1]["id"]]
    stale = client.post(f'/api/jobs/{job["id"]}/files', data={"revision": job["revision"]},
                        files={"files": ("旧请求.png", png_bytes())})
    assert stale.status_code == 409
    duplicate = client.post(f'/api/jobs/{job["id"]}/files', data={"revision": result["revision"]},
                            files={"files": ("重复.png", png_bytes())})
    assert duplicate.status_code == 422
    assert len(client.get(f'/api/jobs/{job["id"]}').json()["files"]) == 2


def test_append_limits_auth_and_processing_guard(client, test_db):
    job = create_job(client)
    response = client.post(f'/api/jobs/{job["id"]}/files', data={"revision": job["revision"]},
                           files=[("files", (f"{i}.png", png_bytes())) for i in range(6)])
    assert response.status_code == 422
    assert client.post(f'/api/jobs/{job["id"]}/files', data={"revision": job["revision"]},
                       files={"files": ("a.pdf", pdf_bytes())},
                       headers={"X-Admin-Token": "invalid"}).status_code == 401
    with test_db.begin() as db:
        db.get(RequirementJob, job["id"]).status = "processing"
    job = client.get(f'/api/jobs/{job["id"]}').json()
    assert client.post(f'/api/jobs/{job["id"]}/files', data={"revision": job["revision"]},
                       files={"files": ("a.pdf", pdf_bytes())}).status_code == 409
