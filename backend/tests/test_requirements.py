import io
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import select

from app import deepseek
from app.config import settings
from app.customer_files import FileRejected, storage_path
from app.models import CustomerFile, ExtractionRun, RequirementJob
from app.requirements_schemas import ExtractionResult, RequirementLine


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (240, 140), "white").save(out, format="PNG")
    return out.getvalue()


def pdf_bytes():
    # Minimal two-page PDF fixture, kept entirely in memory.
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents 5 0 R >>",
        b"<< /Length 27 >>\nstream\n0 0 0 rg 20 20 80 80 re f\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    return data


def create_job(client, filename="需求.png", data=None):
    response = client.post("/api/jobs", data={"title": "测试门牌", "customer": "测试客户", "brief": "严格按标注"},
                           files=[("files", (filename, data if data is not None else png_bytes(), "application/octet-stream"))])
    assert response.status_code == 201, response.text
    return response.json()


def line_input(**kwargs):
    return RequirementLine(id="line1", product="测试产品", material="铝", thickness_mm=Decimal(3),
                           width_mm=Decimal(300), height_mm=Decimal(120), quantity=2, language="en",
                           confirmed=True, **kwargs).model_dump(mode="json")


def save_lines(client, job, lines):
    response = client.put(f'/api/jobs/{job["id"]}/requirements',
                          json={"revision": job["revision"], "lines": lines, "reason": "核对客户标注"})
    assert response.status_code == 200, response.text
    return response.json()


def test_upload_image_auth_preview_and_no_external_call(client):
    job = create_job(client)
    assert job["status"] == "uploaded" and job["file_count"] == 1
    file = job["files"][0]
    assert "storage_key" not in file
    response = client.get(f'/api/customer-files/{file["id"]}/pages/1')
    assert response.status_code == 200
    image = Image.open(io.BytesIO(response.content))
    assert image.size == (240, 140)
    assert client.get(f'/api/customer-files/{file["id"]}/pages/2').status_code == 404
    assert client.get(f'/api/customer-files/{file["id"]}/pages/1',
                      headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.get("/api/jobs").json()["total"] == 1
    assert job["runs"] == []


def test_pdf_is_rendered_by_page(client):
    job = create_job(client, "门牌.pdf", pdf_bytes())
    file = job["files"][0]
    assert file["page_count"] == 2 and file["media_type"] == "application/pdf"
    first = Image.open(io.BytesIO(client.get(f'/api/customer-files/{file["id"]}/pages/1').content))
    second = Image.open(io.BytesIO(client.get(f'/api/customer-files/{file["id"]}/pages/2').content))
    assert first.width > first.height
    assert second.height > second.width
    assert first.getextrema()[0][0] == 0 and first.getextrema()[0][1] == 255


@pytest.mark.parametrize("data,name", [(b"PK\x03\x04fake", "报价.xlsx"), (b"<script>alert(1)</script>", "fake.png"),
                                     (b"%PDF-broken", "broken.pdf"), (b"", "empty.jpg")])
def test_bad_files_rejected_and_cleaned(client, data, name):
    response = client.post("/api/jobs", data={"title": "损坏文件"}, files={"files": (name, data)})
    assert response.status_code == 422, response.text
    assert client.get("/api/jobs").json()["total"] == 0
    if settings().storage_dir.exists():
        assert not list(settings().storage_dir.iterdir())


def test_duplicate_upload_atomic_rollback(client):
    response = client.post("/api/jobs", data={"title": "重复上传"},
                           files=[("files", ("a.png", png_bytes())), ("files", ("b.png", png_bytes()))])
    assert response.status_code == 422
    assert client.get("/api/jobs").json()["total"] == 0
    assert not list(settings().storage_dir.iterdir())


def test_upload_limits(client, monkeypatch):
    monkeypatch.setattr(settings(), "max_upload_bytes", 10)
    assert client.post("/api/jobs", data={"title": "大图"}, files={"files": ("a.png", png_bytes())}).status_code == 422


def test_page_limit(client, monkeypatch):
    monkeypatch.setattr(settings(), "max_job_pages", 1)
    assert client.post("/api/jobs", data={"title": "多页"}, files={"files": ("a.pdf", pdf_bytes())}).status_code == 422


def test_no_path_traversal():
    with pytest.raises(FileRejected):
        storage_path("../../outside")


def test_model_config_never_discloses_key(client):
    assert not client.get("/api/model-config").json()["configured"]
    result = client.put("/api/model-config", json={"api_key": "test-secret-not-real"})
    assert result.status_code == 200
    assert result.json()["configured"]
    assert "test-secret" not in result.text
    assert deepseek.get_key() == "test-secret-not-real"
    assert not client.delete("/api/model-config").json()["configured"]


def test_missing_key_and_consent_gate(client):
    job = create_job(client)
    path = f'/api/jobs/{job["id"]}/extract'
    assert client.post(path, json={"revision": job["revision"]}).status_code == 422
    assert client.post(path, json={"revision": job["revision"], "allow_external_processing": True}).status_code == 503
    assert client.get(f'/api/jobs/{job["id"]}').json()["runs"] == []


def test_extract_confirm_clarify_and_reset_price_links(client, monkeypatch):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key")
    captured = []

    def fake_extract(files, brief, previous, supplement):
        captured.append((previous, supplement))
        result = ExtractionResult.model_validate({
            "summary": "两个门牌", "lines": [{"product": "门牌", "quantity": 2,
                "evidence": [{"file_id": files[0].id, "page": 1, "text": "2件"}]}],
            "questions": ["请确认材质"],
        })
        return result, 1, {"total_tokens": 123}
    monkeypatch.setattr(deepseek, "extract", fake_extract)
    job = create_job(client)
    path = f'/api/jobs/{job["id"]}/extract'
    response = client.post(path, json={"revision": job["revision"], "allow_external_processing": True})
    assert response.status_code == 200, response.text
    job = response.json()
    assert job["status"] == "needs_review"
    assert not job["requirements"][0]["confirmed"]
    assert job["runs"][0]["usage"]["total_tokens"] == 123
    assert client.post(path, json={"revision": job["revision"], "allow_external_processing": True}).status_code == 409
    line = {**job["requirements"][0], "confirmed": True, "material": "铝",
            "selected_price_id": 1, "selected_price_revision": 1}
    job = save_lines(client, job, [line])
    response = client.post(path, json={"revision": job["revision"], "allow_external_processing": True,
                                     "replace_existing": True, "supplementary_text": "材质为铝"})
    job = response.json()
    assert job["requirements"][0]["selected_price_id"] is None
    assert not job["requirements"][0]["confirmed"]
    assert captured[-1][1] == "材质为铝"
    assert any(m["role"] == "user" for m in job["messages"])


def test_failed_extraction_preserves_requirements(client, monkeypatch):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key")
    def fail(*args):
        raise deepseek.ModelFailure("测试网络失败", 1)
    monkeypatch.setattr(deepseek, "extract", fail)
    job = save_lines(client, create_job(client), [line_input()])
    response = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True, "replace_existing": True})
    assert response.status_code == 502
    result = client.get(f'/api/jobs/{job["id"]}').json()
    assert result["requirements"] == job["requirements"]
    assert result["status"] == "failed" and result["runs"][0]["attempts"] == 1


def test_requirement_version_and_source_validation(client):
    job = create_job(client)
    updated = save_lines(client, job, [line_input()])
    assert updated["status"] == "ready"
    assert client.put(f'/api/jobs/{job["id"]}/requirements', json={
        "revision": job["revision"], "lines": [], "reason": "过期修改"}).status_code == 409
    foreign = create_job(client)
    bad = line_input(evidence=[{"file_id": foreign["files"][0]["id"], "page": 1, "text": "bad"}])
    assert client.put(f'/api/jobs/{job["id"]}/requirements', json={
        "revision": updated["revision"], "lines": [bad], "reason": "错误来源"}).status_code == 422


def test_quote_decimal_approval_export_and_staleness(client, price_input):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": "m2", "amount": "300"}).json()
    job = create_job(client)
    line = line_input(selected_price_id=price["id"], selected_price_revision=price["revision"],
                      price_review_note="确认适用面积及工艺，另加每件UV费",
                      extras=[{"label": "UV", "amount": "5", "basis": "per_piece", "reason": "每件UV费"}])
    job = save_lines(client, job, [line])
    response = client.post(f'/api/jobs/{job["id"]}/quotes', json={"revision": job["revision"], "terms": "含税，不含运输和安装"})
    assert response.status_code == 201, response.text
    quote = response.json()
    assert quote["payload"]["total"] == "31.60"
    assert quote["payload"]["lines"][0]["price"]["amount"] == "300.000000"
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409
    assert client.post(f'/api/quotes/{quote["id"]}/approve', json={"note": "已核对"}).status_code == 422
    approved = client.post(f'/api/quotes/{quote["id"]}/approve',
                           json={"note": "已核对原图及价格", "confirmed_commercial_terms": True})
    assert approved.status_code == 200
    export = client.get(f'/api/quotes/{quote["id"]}/export')
    assert export.status_code == 200
    assert "31.60" in export.content.decode("utf-8-sig")
    assert "gross_margin" not in export.text and "price_review_note" not in export.text
    client.put(f'/api/prices/{price["id"]}', json={**price_input, "unit": "m2", "status": "active",
                                               "revision": price["revision"], "amount": "350"})
    assert client.get(f'/api/jobs/{job["id"]}').json()["quotes"][0]["outdated"]
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409


def test_draft_price_and_partial_quote_never_complete(client, price_input):
    price = client.post("/api/prices", json=price_input).json()
    job = save_lines(client, create_job(client), [
        line_input(selected_price_id=price["id"], selected_price_revision=price["revision"],
                   price_review_note="已确认", billing_length_mm="200")])
    quote = client.post(f'/api/jobs/{job["id"]}/quotes',
                        json={"revision": job["revision"], "terms": "未含运费"}).json()
    assert quote["status"] == "blocked"
    assert quote["payload"]["total"] is None
    assert "所选价格尚未启用" in quote["payload"]["lines"][0]["blockers"]
    assert client.post(f'/api/quotes/{quote["id"]}/approve',
                       json={"note": "测试", "confirmed_commercial_terms": True}).status_code == 409


@pytest.mark.parametrize("unit,measure,expected", [("cm", "200", "120.00"), ("m", "200", "1.20"),
                                                 ("piece", None, "6.00"), ("set", None, "6.00")])
def test_billing_units(client, price_input, unit, measure, expected):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": unit, "amount": "3"}).json()
    job = save_lines(client, create_job(client), [line_input(selected_price_id=price["id"],
        selected_price_revision=price["revision"], price_review_note="确认规格与数量", billing_length_mm=measure)])
    quote = client.post(f'/api/jobs/{job["id"]}/quotes', json={"revision": job["revision"], "terms": "测试报价"}).json()
    assert quote["payload"]["total"] == expected


def test_candidate_prices_show_drafts_but_not_inactive(client, price_input):
    price = client.post("/api/prices", json=price_input).json()
    job = save_lines(client, create_job(client), [line_input()])
    path = f'/api/jobs/{job["id"]}/lines/line1/candidates'
    assert client.get(path).json()["prices"][0]["status"] == "draft"
    client.request("DELETE", f'/api/prices/{price["id"]}', json={"revision": 1, "reason": "测试停用"})
    assert client.get(path).json()["prices"] == []


def test_interrupted_run_recovery(client, test_db):
    from datetime import timedelta
    from app.models import now
    job = create_job(client)
    with test_db.begin() as db:
        record = db.get(RequirementJob, job["id"])
        record.status = "processing"
        db.add(ExtractionRun(job_id=record.id, model="test", input_revision=record.revision,
                             created_at=now() - timedelta(hours=1)))
    current = client.get(f'/api/jobs/{job["id"]}').json()
    response = client.post(f'/api/jobs/{job["id"]}/recover', json={"revision": current["revision"], "reason": "服务中断"})
    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_csv_formula_injection():
    from app.requirements_api import csv_safe
    assert csv_safe("=HYPERLINK(\"http://evil\")").startswith("'")
    assert csv_safe("  +SUM(1)").startswith("'")
    assert csv_safe("正常中文") == "正常中文"


def test_request_envelope_limit(client):
    response = client.post("/api/jobs", content=b"x", headers={"Content-Length": str(60 * 1024 * 1024)})
    assert response.status_code == 413


def test_edited_requirements_invalidate_quote(client, price_input):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": "piece"}).json()
    job = save_lines(client, create_job(client), [line_input(selected_price_id=price["id"],
        selected_price_revision=price["revision"], price_review_note="适用已核对")])
    quote = client.post(f'/api/jobs/{job["id"]}/quotes', json={"revision": job["revision"], "terms": "测试条款"}).json()
    changed = {**job["requirements"][0], "quantity": 3}
    updated = save_lines(client, job, [changed])
    assert updated["quotes"][0]["outdated"]
    assert client.post(f'/api/quotes/{quote["id"]}/approve',
                       json={"note": "已确认", "confirmed_commercial_terms": True}).status_code == 409


def test_missing_length_and_material_conflict_block(client, price_input):
    price = client.post("/api/prices", json={**price_input, "status": "active"}).json()
    line = line_input(selected_price_id=price["id"], selected_price_revision=price["revision"], price_review_note="适用已核对")
    line["material"] = "铜"
    job = save_lines(client, create_job(client), [line])
    quote = client.post(f'/api/jobs/{job["id"]}/quotes', json={"revision": job["revision"], "terms": "测试条款"}).json()
    assert quote["payload"]["total"] is None
    assert "需求材质与价格材质不一致" in quote["payload"]["lines"][0]["blockers"]
    assert "缺少每件计价长度（mm）" in quote["payload"]["lines"][0]["blockers"]
