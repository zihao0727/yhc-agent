import io
import json
import re
import threading
from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pypdfium2 as pdfium
import pytest
from PIL import Image
from sqlalchemy import func, select

from app import agent_worker, deepseek
from app.config import settings
from app.customer_files import (
    ensure_pdf_preview, pdf_region_content, pdf_regions, prepare_file, render_pdf_batch, storage_path,
)
from app.extraction_checkpoints import ExtractionCheckpoint, ExtractionYield
from app.models import AgentRun, ExtractionBatch, RequirementJob
from app.pdf_extraction import merge_regions
from app.requirements_schemas import ExtractionResult, RequirementsUpdate
from test_deepseek import configure_transport
from test_redis_agent import redis_worker


def document_bytes(pages=1, width=600, height=800, rotation=0, text=False):
    content_id = 4 + pages
    stream = (f"BT /F1 14 Tf 50 {height - 50} Td (101 TOP LEFT) Tj ET\n"
              f"BT /F1 14 Tf {width - 170} {height - 50} Td (102 TOP RIGHT) Tj ET\n"
              "BT /F1 14 Tf 50 50 Td (103 BOTTOM LEFT) Tj ET\n"
              f"BT /F1 14 Tf {width - 170} 50 Td (104 BOTTOM RIGHT) Tj ET\n").encode()
    if not text:
        stream = b"0 0 0 rg 20 20 80 80 re f\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {pages} /Kids [{' '.join(f'{4 + p} 0 R' for p in range(pages))}] >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *[f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Rotate {rotation} "
          f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode() for _ in range(pages)],
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
    ]
    data, offsets = b"%PDF-1.4\n", [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


def fixture_file(**kwargs):
    return SimpleNamespace(id=41, **prepare_file(document_bytes(**kwargs), "large.pdf"))


def model_result(file_id, page, item, **kwargs):
    return {"lines": [{"source_item": str(item), "product": "Sign", "evidence": [
        {"file_id": file_id, "page": page, "text": f"Item {item}"}], **kwargs}]}


def response(result, tokens=10, finish="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {
        "content": json.dumps(result)}}], "usage": {"total_tokens": tokens}})


def test_240_page_upload_is_streamed_without_rendering(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Upload must not rasterize PDF pages")
    monkeypatch.setattr(pdfium.PdfPage, "render", forbidden)
    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024
            return super().read(size)
    monkeypatch.setattr(settings(), "max_upload_bytes", 10)
    result = prepare_file(BoundedStream(document_bytes(pages=240)), "large.pdf")
    assert result["page_count"] == 240
    assert [p.name for p in storage_path(result["storage_key"]).iterdir()] == ["original"]


def test_background_render_batch_and_lazy_preview_are_bounded():
    file = fixture_file(pages=240)
    assert render_pdf_batch(file, 1) == settings().pdf_render_batch_pages
    directory = storage_path(file.storage_key)
    assert len(list(directory.glob("page-*.png"))) == 4
    target = ensure_pdf_preview(file, 240)
    before = target.stat().st_mtime_ns
    assert ensure_pdf_preview(file, 240).stat().st_mtime_ns == before
    with Image.open(target) as image:
        assert image.width <= 1800 and image.height <= 1800
        assert image.getextrema()[0][0] < 255


@pytest.mark.parametrize("rotation,expected", [
    (0, {"101", "102"}), (90, {"101", "103"}),
    (180, {"103", "104"}), (270, {"102", "104"}),
])
def test_region_text_matches_rotated_pixels(rotation, expected):
    file = fixture_file(rotation=rotation, text=True)
    text, data = pdf_region_content(file, 1, (0, 0, 1, .5))
    assert {number for number in ("101", "102", "103", "104") if number in text} == expected
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1800
        assert image.getextrema()[0][0] < 255


def test_large_drawing_renders_only_bounded_region():
    file = fixture_file(width=2400, height=3600, text=True)
    regions = pdf_regions(file, 1)
    assert len(regions) > 2
    text, data = pdf_region_content(file, 1, regions[0])
    assert "TOP LEFT" in text
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1800


def test_pdf_regions_resume_without_repeating_paid_requests(monkeypatch):
    file = fixture_file()
    calls, saved = [], []
    def handler(request):
        calls.append(json.loads(request.content))
        images = [part for part in calls[-1]["messages"][1]["content"] if part["type"] == "image_url"]
        assert len(images) == 1
        if len(calls) == 2:
            return httpx.Response(503)
        return response(model_result(41, 1, len(calls), quantity=1))
    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("pdf", {}, lambda value, usage: saved.append(deepcopy(value)), lambda: None)
    with pytest.raises(deepseek.ModelFailure):
        deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    restored = ExtractionCheckpoint("pdf", saved[-1], lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert len(calls) == 3
    assert attempts == 1 and usage["total_tokens"] == 10
    assert len(result.lines) == 2
    assert all(line.evidence[0].region for line in result.lines)


def test_truncated_region_subdivides_and_remembers_split(monkeypatch):
    file = fixture_file()
    calls, saved = [], []
    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return response({}, finish="length")
        return response(model_result(41, 1, len(calls)))
    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("split", {}, lambda value, usage: saved.append(deepcopy(value)), lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    assert attempts == len(calls) == 4
    assert usage["total_tokens"] == 40 and len(result.lines) == 3
    assert any("pdf_split" in key for key in saved[-1]["splits"])
    restored = ExtractionCheckpoint("split", saved[-1], lambda *args: None, lambda: None)
    again, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert len(calls) == 4 and attempts == 0 and usage == {}
    assert again == result


def test_overlap_merges_same_source_without_adding_quantities():
    first = ExtractionResult.model_validate(model_result(41, 1, 1, quantity=2, width_mm=300))
    second = ExtractionResult.model_validate(model_result(41, 1, 1, quantity=2, width_mm=320))
    third = ExtractionResult.model_validate(model_result(41, 1, 1, quantity=2, width_mm=300))
    result = merge_regions([first, second, third])
    assert len(result.lines) == 1 and result.lines[0].quantity == 2
    assert result.lines[0].width_mm is None and result.lines[0].uncertainties


def test_same_name_without_item_number_and_repeated_items_in_one_region_are_preserved():
    raw = model_result(41, 1, 1)
    raw["lines"][0]["source_item"] = ""
    first = ExtractionResult.model_validate(raw)
    second = ExtractionResult.model_validate(raw)
    result = merge_regions([first, second])
    assert len(result.lines) == 2 and all(line.uncertainties for line in result.lines)
    raw = model_result(41, 1, 1)
    raw["lines"].append(deepcopy(raw["lines"][0]))
    result = merge_regions([ExtractionResult.model_validate(raw)])
    assert len(result.lines) == 2 and all(line.uncertainties for line in result.lines)


def test_large_quote_tool_result_keeps_totals_without_overflowing_model_context():
    from app.agent_context import model_result
    result = model_result("finish_quote", {"complete": False, "known_subtotal": "1200",
        "lines": [{"line_id": str(i), "product": f"Sign {i}", "amount": None, "blockers": ["Review"]}
                  for i in range(10000)]})
    assert result["line_count"] == 10000 and result["known_subtotal"] == "1200"
    assert result["windowed"] and len(result["lines"]) == 25


def test_more_than_100_lines_survive_aggregation_and_edit_schema(monkeypatch):
    file = fixture_file(pages=3)
    def handler(request):
        payload = json.loads(request.content)
        text = "\n".join(part.get("text", "") for part in payload["messages"][1]["content"])
        page = next(page for page in range(1, 4) if f"page={page}" in text)
        return response({"lines": [{"source_item": str(i), "product": f"Sign {i}",
            "evidence": [{"file_id": 41, "page": page, "text": f"Item {i}"}]} for i in range(1, 61)]})
    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([file], "", [], "")
    assert len(result.lines) == 180
    from app.requirements_schemas import RequirementLine
    rows = [RequirementLine(id=str(i), **line.model_dump()) for i, line in enumerate(result.lines)]
    assert len(RequirementsUpdate(revision=1, lines=rows, reason="Review source").lines) == 180


def enqueue_pdf(client, monkeypatch, pages=3):
    from test_requirements import create_job
    job = create_job(client, "large.pdf", document_bytes(pages=pages))
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_slice_batches", 3)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only")
    result = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert result.status_code == 200, result.text
    return result.json()


def test_worker_saves_pages_yields_and_finishes_extraction_only(client, monkeypatch, redis_worker, test_db):
    calls = []
    def handler(request):
        calls.append(1)
        text = "\n".join(p.get("text", "") for p in json.loads(request.content)["messages"][1]["content"])
        page = next(p for p in range(1, 4) if f"page={p}" in text)
        return response(model_result(1, page, 1, quantity=1))
    job = enqueue_pdf(client, monkeypatch)
    # File IDs are not assumed globally sequential in PostgreSQL test schemas.
    file_id = job["files"][0]["id"]
    def source_handler(request):
        value = handler(request)
        body = json.loads(value.content)
        result = json.loads(body["choices"][0]["message"]["content"])
        result["lines"][0]["evidence"][0]["file_id"] = file_id
        return response(result)
    configure_transport(monkeypatch, source_handler)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "processing" and len(current["requirements"]) == 1
    assert current["extraction"]["incomplete"]
    first_id = current["requirements"][0]["id"]
    assert current["agent_runs"][0]["usage"]["extraction_progress"]["completed_pages"] == 1
    with test_db() as db:
        run = db.get(AgentRun, current["agent_runs"][0]["id"])
        assert run.usage["extraction_checkpoint"]["batches"] == {}
        assert db.scalar(select(func.count()).select_from(ExtractionBatch)) == 3
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "needs_review"
    assert len(current["requirements"]) == 3 and len(calls) == 6
    assert current["requirements"][0]["id"] == first_id
    assert current["agent_runs"][0]["status"] == "succeeded" and not current["quotes"]
    assert current["agent_runs"][0]["usage"]["total_tokens"] == 60
    assert not current["extraction"].get("incomplete")


def test_stop_partial_then_resume_reuses_sql_batches_and_blocks_partial_quote(
        client, monkeypatch, redis_worker, test_db):
    job = enqueue_pdf(client, monkeypatch, pages=2)
    file_id = job["files"][0]["id"]
    calls = []
    def handler(request):
        calls.append(1)
        text = "\n".join(p.get("text", "") for p in json.loads(request.content)["messages"][1]["content"])
        page = 1 if "page=1" in text else 2
        return response(model_result(file_id, page, 1))
    configure_transport(monkeypatch, handler)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    stopped = client.post(f'/api/jobs/{job["id"]}/agent/stop', json={
        "revision": current["revision"], "reason": "Stop partial extraction"})
    assert stopped.status_code == 200
    current = stopped.json()
    blocked = client.post(f'/api/jobs/{job["id"]}/quotes', json={"revision": current["revision"], "terms": "Review terms"})
    assert blocked.status_code == 409
    resumed = client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": current["revision"], "intent": "resume", "allow_external_processing": True})
    assert resumed.status_code == 200
    with test_db.begin() as db:
        run = db.get(AgentRun, resumed.json()["agent_runs"][0]["id"])
        run.usage = {**run.usage, "extraction_only": True}
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(calls) == 4 and len(current["requirements"]) == 2
    assert current["status"] == "needs_review"
    assert current["agent_runs"][0]["usage"]["total_tokens"] == 20
    assert current["agent_runs"][1]["usage"]["total_tokens"] == 20


def test_240_page_document_completes_all_regions_without_100_line_cap(monkeypatch):
    file = fixture_file(pages=240)
    calls = []
    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        text = "\n".join(part.get("text", "") for part in parts)
        page = int(re.search(r"file_id=41, page=(\d+)", text)[1])
        calls.append(page)
        return response(model_result(41, page, (len(calls) - 1) % 2 + 1))
    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("240-pages", {}, lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    assert len(calls) == attempts == 480 and usage["total_tokens"] == 4800
    assert len(result.lines) == 480
    assert {line.evidence[0].page for line in result.lines} == set(range(1, 241))
    assert all(line.evidence[0].region is not None for line in result.lines)
