import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app import agent_worker, deepseek
from app.config import settings
from app.customer_excel import ROWS_PER_BATCH, page_source, row_source
from app.customer_files import prepare_file, storage_path
from app.extraction_checkpoints import ExtractionCheckpoint
from test_customer_excel import workbook_bytes
from test_deepseek import configure_transport
from test_redis_agent import redis_worker  # noqa: F401


def source_from(request):
    parts = json.loads(request.content)["messages"][1]["content"]
    return next(json.loads(part["text"].split(":\n", 1)[1])
                for part in parts if "原始文本/单元格" in part.get("text", ""))


def reply(source, status="stop", content=None):
    result = {"lines": [{"source_item": str(row["row"] - 1),
        "product": next(cell["value"] for cell in row["cells"] if cell["cell"].startswith("B")),
        "evidence": [{"file_id": 41, "page": source["page"], "sheet": source["sheet"],
            "cell": next(cell["cell"] for cell in row["cells"] if cell["cell"].startswith("B")),
            "text": next(cell["value"] for cell in row["cells"] if cell["cell"].startswith("B"))}]}
        for row in source["rows"] if row["row"] > 1]}
    return httpx.Response(200, json={"choices": [{"finish_reason": status,
        "message": {"content": content if content is not None else json.dumps(result)}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}})


def fixture_file(rows=12, drawing=False):
    return SimpleNamespace(id=41, **prepare_file(workbook_bytes(rows=rows, drawing=drawing), "rows.xlsx"))


def test_large_table_batches_preserve_all_rows_and_skip_whole_page_preview(monkeypatch):
    file = fixture_file()
    seen = []

    def handler(request):
        source = source_from(request)
        seen.append(source)
        parts = json.loads(request.content)["messages"][1]["content"]
        assert not any(part["type"] == "image_url" for part in parts)
        assert len(source["rows"]) <= ROWS_PER_BATCH
        assert [row["row"] for row in source["header_rows"]] == [1]
        return reply(source)

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "",
        _page_scope={(41, 1)}, _excel_plans={})
    assert attempts == 3 and usage["total_tokens"] == 30
    assert [row["row"] for source in seen for row in source["rows"]] == list(range(2, 14))
    assert [line.source_item for line in result.lines] == [str(i) for i in range(1, 13)]
    assert all(line.quantity == 2 for line in result.lines)
    assert all(line.type_note == "发光字" and line.difficulty == "B" for line in result.lines)
    assert all(e.page == 1 for line in result.lines for e in line.evidence)


def test_truncated_batch_splits_instead_of_repeating_and_saves_only_complete_json(monkeypatch):
    file = fixture_file(rows=7)
    seen, saved, charged = [], [], []

    def handler(request):
        source = source_from(request)
        rows = [row["row"] for row in source["rows"]]
        seen.append(rows)
        return reply(source, "length", '{"lines":[{"product":"partial') if len(rows) > 2 else reply(source)

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("row-batches", {},
        lambda cache, usage: (saved.append(deepcopy(cache)), charged.append(dict(usage))), lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "",
        _page_scope={(41, 1)}, _excel_plans={}, _checkpoint=checkpoint)
    assert len(result.lines) == 7
    assert seen == [[2, 3, 4, 5, 6], [2, 3], [4, 5, 6], [4], [5, 6], [7, 8]]
    assert attempts == 6 and usage["total_tokens"] == 60
    assert sum(tokens.get("total_tokens", 0) for tokens in charged) == 60
    assert saved[-1]["splits"]
    assert "partial" not in json.dumps(saved[-1])
    restored = ExtractionCheckpoint("row-batches", saved[-1], lambda *args: None, lambda: None)
    again, attempts, usage = deepseek.extract([file], "", [], "",
        _page_scope={(41, 1)}, _excel_plans={}, _checkpoint=restored)
    assert again.model_dump() == result.model_dump()
    assert attempts == 0 and usage == {} and len(seen) == 6


def test_retry_reuses_subdivision_and_successful_children_after_network_failure(monkeypatch):
    file = fixture_file(rows=5)
    seen, saved = [], []

    def handler(request):
        source = source_from(request)
        rows = [row["row"] for row in source["rows"] if row["row"] > 1]
        seen.append(rows)
        if len(seen) == 1:
            return reply(source, "length", "incomplete")
        if len(seen) == 3:
            return httpx.Response(503)
        return reply(source)

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("row-retry", {},
        lambda cache, usage: saved.append(deepcopy(cache)), lambda: None)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={},
                         _checkpoint=checkpoint)
    assert failure.value.usage["total_tokens"] == 20
    assert failure.value.attempts == 3 and failure.value.diagnostics["retryable"]
    restored = ExtractionCheckpoint("row-retry", saved[-1], lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "",
        _page_scope={(41, 1)}, _excel_plans={}, _checkpoint=restored)
    assert seen == [[2, 3, 4, 5, 6], [2, 3], [4, 5, 6], [4, 5, 6]]
    assert len(result.lines) == 5
    assert attempts == 1 and usage["total_tokens"] == 10


@pytest.mark.parametrize("cell", ["B1", "B2", "B999"])
def test_header_or_other_batch_cannot_be_used_as_project_source(monkeypatch, cell):
    file = fixture_file(rows=12)

    def handler(request):
        source = source_from(request)
        result = {"lines": [{"product": "fake", "evidence": [
            {"file_id": 41, "page": 1, "sheet": source["sheet"], "cell": cell, "text": "fake"}]}]}
        return reply(source, content=json.dumps(result))

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={}, _excel_rows=(7, 8))
    assert failure.value.attempts == 2 and not failure.value.diagnostics["retryable"]


def test_row_source_only_contains_selected_drawings_and_real_header_rows():
    file = fixture_file(rows=12, drawing=True)
    source = page_source(storage_path(file.storage_key), 1)
    first = row_source(source, (2, 3))
    other = row_source(source, (7, 8))
    assert len(first["images"]) == 1 and other["images"] == []
    assert [row["row"] for row in other["rows"]] == [7, 8]
    assert [row["row"] for row in other["header_rows"]] == [1]


def test_row_batch_network_failure_reports_bounds_and_reuses_successful_batches(monkeypatch):
    file = fixture_file(rows=12)
    saved, seen = [], []

    def handler(request):
        source = source_from(request)
        seen.append([row["row"] for row in source["rows"]])
        if len(seen) == 2:
            return httpx.Response(503)
        return reply(source)

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("batch-retry", {},
        lambda cache, usage: saved.append(deepcopy(cache)), lambda: None)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={}, _checkpoint=checkpoint)
    assert failure.value.diagnostics["row_start"] == 7
    assert failure.value.diagnostics["row_end"] == 11
    assert failure.value.usage["total_tokens"] == 10
    restored = ExtractionCheckpoint("batch-retry", saved[-1], lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "",
        _page_scope={(41, 1)}, _excel_plans={}, _checkpoint=restored)
    assert len(result.lines) == 12
    assert seen == [[2, 3, 4, 5, 6], [7, 8, 9, 10, 11], [7, 8, 9, 10, 11], [12, 13]]
    assert attempts == 2 and usage["total_tokens"] == 20


def test_single_row_truncation_is_bounded_and_never_saves_partial_content(monkeypatch):
    file = fixture_file(rows=1)
    calls, saved = [], []

    def handler(request):
        calls.append(1)
        return reply(source_from(request), "length", '{"lines":[{"product":"partial')

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("one-row", {},
        lambda cache, usage: saved.append(deepcopy(cache)), lambda: None)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={},
                         _excel_rows=(2,), _checkpoint=checkpoint)
    assert len(calls) == failure.value.attempts == 2
    assert failure.value.usage["total_tokens"] == 20
    assert all(not snapshot["batches"] for snapshot in saved)


def test_multi_page_batches_preserve_page_numbers_and_full_row_coverage(monkeypatch):
    file = fixture_file(rows=22)
    seen = []

    def handler(request):
        source = source_from(request)
        seen.append(source)
        return reply(source)

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "", _excel_plans={})
    main = [line for line in result.lines if line.evidence[0].sheet == "客户明细"]
    assert len(main) == 22 and len(result.lines) == 23
    assert [line.source_item for line in main] == [str(i) for i in range(1, 23)]
    assert all(line.evidence[0].page == (1 if int(line.source_item) <= 19 else 2) for line in main)
    assert attempts == 6 and usage["total_tokens"] == 60
    assert {source["page"] for source in seen} == {1, 2, 3}


def test_embedded_drawings_are_only_sent_for_their_original_batch_rows(monkeypatch):
    file = fixture_file(rows=12, drawing=True)
    images_per_batch = []

    def handler(request):
        source = source_from(request)
        parts = json.loads(request.content)["messages"][1]["content"]
        images_per_batch.append(sum(part["type"] == "image_url" for part in parts))
        return reply(source)

    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={})
    assert len(result.lines) == 12
    assert images_per_batch == [1, 0, 0]


def test_worker_counts_truncated_and_successful_calls_once_and_keeps_quotes_empty(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "excel_native_first", False)
    job = client.post("/api/jobs", data={"title": "row batch test"},
        files={"files": ("rows.xlsx", workbook_bytes(rows=7, drawing=False))}).json()
    file_id = job["files"][0]["id"]
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-test-key")
    calls = []

    def handler(request):
        source = source_from(request)
        calls.append(source["page"])
        count = sum(row["row"] > 1 for row in source["rows"])
        response = reply(source, "length", "incomplete") if count > 2 else reply(source)
        body = response.json()
        body["choices"][0]["message"]["content"] = body["choices"][0]["message"]["content"].replace(
            '"file_id": 41', f'"file_id": {file_id}')
        return httpx.Response(200, json=body)

    def stop_before_pricing(*args):
        raise deepseek.ModelFailure("pricing not called in extraction test", diagnostics={"retryable": False})

    configure_transport(monkeypatch, handler)
    monkeypatch.setattr(deepseek, "agent_turn", stop_before_pricing)
    response = client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": job["revision"], "intent": "extract", "allow_external_processing": True})
    assert response.status_code == 200, response.text
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    for _ in range(3):
        if current["agent_runs"][0]["usage"].get("prepared"):
            break
        agent_worker.run_pending_once(threading.Event())
        current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(calls) == 7 and len(current["requirements"]) == 8
    assert current["quotes"] == []
    assert current["agent_runs"][0]["usage"]["prepared"]
    assert current["agent_runs"][0]["usage"]["extraction_tokens"] == 70
    assert current["agent_runs"][0]["usage"]["total_tokens"] == 70
