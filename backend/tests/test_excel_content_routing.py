import io
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import Workbook

from app import agent_worker, customer_excel, deepseek
from app.config import settings
from app.customer_files import prepare_file, storage_path
from app.excel_manifest import ensure_preview
from test_customer_excel import workbook_bytes
from test_deepseek import configure_transport
from test_excel_row_batches import reply, source_from
from test_redis_agent import redis_worker  # noqa: F401
from test_requirements import png_bytes


@pytest.mark.parametrize("rows", [1, 5, 12, 22])
def test_plain_workbook_never_renders_or_sends_images(monkeypatch, rows):
    def forbidden(*args, **kwargs):
        raise AssertionError("Excel extraction must not render previews")

    monkeypatch.setattr(customer_excel, "render", forbidden)
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(rows=rows, drawing=False), "plain.xlsx"))
    calls = []

    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        assert not any(part["type"] == "image_url" for part in parts)
        source = source_from(request)
        calls.append(source)
        return reply(source)

    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([file], "", [], "")
    assert len(result.lines) == rows + 1
    assert calls
    assert not list(storage_path(file.storage_key).glob("page-*.png"))


def test_headerless_text_stays_text_even_when_model_retries(monkeypatch):
    workbook = Workbook()
    workbook.active.append(["入口招牌", "铝板烤漆，现场安装"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    file = SimpleNamespace(id=41, **prepare_file(output.getvalue(), "notes.xlsx"))
    calls = []

    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        assert not any(part["type"] == "image_url" for part in parts)
        source = source_from(request)
        calls.append(source)
        content = "invalid json" if len(calls) == 1 else json.dumps({"lines": []})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                    "message": {"content": content}}]})

    configure_transport(monkeypatch, handler)
    deepseek.extract([file], "", [], "", _excel_plans={})
    assert len(calls) == 2


def test_excel_preview_is_lazy_cached_and_validated():
    file = prepare_file(workbook_bytes(drawing=False), "plain.xlsx")
    directory = storage_path(file["storage_key"])
    assert not list(directory.glob("page-*.png"))
    target = ensure_preview(directory, 1)
    before = target.read_bytes(), target.stat().st_mtime_ns
    assert target.read_bytes().startswith(b"\x89PNG")
    assert ensure_preview(directory, 1) == target
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert not (directory / "page-2.png").exists()
    with pytest.raises(ValueError):
        ensure_preview(directory, 0)
    with pytest.raises(ValueError):
        ensure_preview(directory, file["page_count"] + 1)


def test_excel_preview_endpoint_generates_only_requested_page(client):
    job = client.post("/api/jobs", data={"title": "Lazy preview"},
        files={"files": ("plain.xlsx", workbook_bytes(rows=22, drawing=False))}).json()
    file_id = job["files"][0]["id"]
    url = f"/api/customer-files/{file_id}/pages/2"
    assert client.get(url, headers={"X-Admin-Token": "bad"}).status_code == 401
    response = client.get(url)
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert client.get(url).content == response.content
    assert client.get(f"/api/customer-files/{file_id}/pages/99").status_code == 404


def test_worker_excel_row_progress_resumes_and_publishes_merged_pages(client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "excel_native_first", False)
    job = client.post("/api/jobs", data={"title": "Row progress"},
        files={"files": ("plain.xlsx", workbook_bytes(rows=12, drawing=False))}).json()
    file_id = job["files"][0]["id"]
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_slice_batches", 1)
    calls = []

    def handler(request):
        source = source_from(request)
        parts = json.loads(request.content)["messages"][1]["content"]
        assert not any(part["type"] == "image_url" for part in parts)
        calls.append((source["page"], [row["row"] for row in source["rows"]]))
        body = reply(source).json()
        body["choices"][0]["message"]["content"] = body["choices"][0]["message"]["content"].replace(
            '"file_id": 41', f'"file_id": {file_id}')
        return httpx.Response(200, json=body)

    configure_transport(monkeypatch, handler)
    response = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert response.status_code == 200, response.text
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    progress = current["agent_runs"][0]["usage"]["extraction_progress"]
    assert progress["unit"] == "行"
    assert progress["completed_units"] == 5 and progress["total_units"] == 15
    assert progress["saved_lines"] == 0
    assert "单元格" in progress["detail"] and "分块识别" not in current["agent_runs"][0]["message"]
    assert "completed_row_keys" not in progress
    for _ in range(12):
        if current["status"] != "processing":
            break
        agent_worker.run_pending_once(threading.Event())
        current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert len(calls) == 4
    assert len(current["requirements"]) == 13
    assert len({line["id"] for line in current["requirements"]}) == 13
    assert run["usage"]["extraction_progress"]["completed_units"] == 15
    assert run["usage"]["extraction_progress"]["completed_pages"] == 2
    assert run["usage"]["extraction_progress"]["saved_lines"] == 13
    assert run["usage"]["total_tokens"] == 40
    assert current["quotes"] == []


def test_mixed_attachment_progress_switches_from_cells_to_actual_image(client, monkeypatch, redis_worker):
    job = client.post("/api/jobs", data={"title": "Mixed sources"}, files=[
        ("files", ("plain.xlsx", workbook_bytes(rows=1, drawing=False))),
        ("files", ("drawing.png", png_bytes())),
    ]).json()
    file_id = job["files"][0]["id"]
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_slice_batches", 1)
    modalities = []

    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        has_image = any(part["type"] == "image_url" for part in parts)
        modalities.append(has_image)
        if has_image:
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                "message": {"content": '{"lines":[]}'}}]})
        body = reply(source_from(request)).json()
        body["choices"][0]["message"]["content"] = body["choices"][0]["message"]["content"].replace(
            '"file_id": 41', f'"file_id": {file_id}')
        return httpx.Response(200, json=body)

    configure_transport(monkeypatch, handler)
    response = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert response.status_code == 200, response.text
    current = response.json()
    for _ in range(8):
        if current["status"] != "processing":
            break
        agent_worker.run_pending_once(threading.Event())
        current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert modalities == [False, True]
    progress = current["agent_runs"][0]["usage"]["extraction_progress"]
    assert progress["completed_units"] == progress["total_units"] == 3
    assert progress["unit"] == "资料段"
    assert progress["detail"] == "drawing.png，识别图片"
    assert progress["file_id"] == job["files"][1]["id"]
    assert len(current["requirements"]) == 2
