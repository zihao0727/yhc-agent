import copy
import io
import json
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
import httpx
from openpyxl import Workbook

from app import agent_worker, deepseek
from app.config import settings
from app.customer_excel import MEDIA_TYPE
from app.extraction_recovery import recover_extraction
from app.extraction_validation import ExtractionValidationFailure, validate_extraction
from test_agent_loop import call
from test_extraction_recovery import checkpoint, turns
from test_deepseek import configure_transport
from test_redis_agent import redis_worker  # noqa: F401


@pytest.mark.parametrize("text,expected", [
    ("2.0.5mm304#不锈钢板", {"0.5", "304"}),
    ("12.1.25mm不锈钢板", {"1.25"}),
    (" 2. 0.5 mm", {"0.5"}),
    ("2．0.5mm", {"0.5"}),
    ("2、0.5mm", {"0.5"}),
    ("1.规格尺寸:35*370mm\n2.0.5mm304#不锈钢板", {"35", "370", "0.5", "304"}),
    ("2.5mm", {"2.5"}),
    ("0.5mm", {"0.5"}),
    ("T2.0mm", {"2.0"}),
    ("规格328*300*950mm", {"328", "300", "950"}),
    ("厚度12.05mm", {"12.05"}),
    ("型号T2.0.5mm", set()),
])
def test_numbered_decimal_is_separated_without_accepting_numeric_fragments(text, expected):
    assert deepseek.dimension_numbers(text) == {Decimal(value) for value in expected}


def source_fixture():
    clause = "2.0.5mm304#不锈钢板冲压盲文"
    dimensions = "1.规格尺寸:35*370mm"
    evidence = {"file_id": 16, "page": 4, "sheet": "清单", "cell": "D70", "source": "text"}
    raw = {"lines": [{"source_item": "46", "product": "无障碍电梯", "width_mm": 35,
        "height_mm": 370, "thickness_mm": 0.5,
        "evidence": [{**evidence, "cell": "C70", "text": "无障碍电梯"}],
        "dimension_evidence": [
            {**evidence, "field": "width_mm", "value": 35, "text": dimensions},
            {**evidence, "field": "height_mm", "value": 370, "text": dimensions},
            {**evidence, "field": "thickness_mm", "value": 0.5, "text": clause, "role": "plate_thickness"},
        ]}]}
    source = {"page": 4, "sheet": "清单", "images": [], "header_rows": [{"row": 1, "cells": [
        {"cell": "A1", "value": "序号"}, {"cell": "C1", "value": "产品名称"},
        {"cell": "D1", "value": "项目特征"}, {"cell": "E1", "value": "单位"},
        {"cell": "F1", "value": "数量"}]}], "rows": [{"row": 70, "cells": [
            {"cell": "A70", "value": "46"}, {"cell": "C70", "value": "无障碍电梯"},
            {"cell": "D70", "value": dimensions + "\n" + clause},
            {"cell": "E70", "value": "套"}, {"cell": "F70", "value": "24"},
        ]}]}
    files = [SimpleNamespace(id=16, page_count=6, media_type=MEDIA_TYPE)]
    return raw, files, {(16, 4): json.dumps(source, ensure_ascii=False)}


def invalid_fixture():
    raw, files, texts = source_fixture()
    raw["lines"][0]["thickness_mm"] = 5
    raw["lines"][0]["dimension_evidence"][2]["value"] = 5
    with pytest.raises(ExtractionValidationFailure) as failure:
        validate_extraction(copy.deepcopy(raw), files, texts, "", "")
    return raw, files, texts, failure.value.diagnostics


def test_real_cell_quote_passes_with_decimal_and_preserves_source():
    raw, files, texts = source_fixture()
    original = copy.deepcopy(raw)
    result = validate_extraction(raw, files, texts, "", "", excel_rows=(70,), page_scope={(16, 4)})
    assert len(result.lines) == 1
    line = result.lines[0]
    assert (line.width_mm, line.height_mm, line.thickness_mm, line.quantity) == (35, 370, Decimal("0.5"), 24)
    assert line.dimension_evidence[2].text == original["lines"][0]["dimension_evidence"][2]["text"]
    assert line.dimension_evidence[2].cell == "D70"


@pytest.mark.parametrize("value", [2, 5, 0.05])
def test_incorrect_size_is_not_accepted_by_splitting_numbered_decimal(value):
    raw, files, texts = source_fixture()
    raw["lines"][0]["dimension_evidence"][2]["value"] = value
    with pytest.raises(ExtractionValidationFailure) as failure:
        validate_extraction(raw, files, texts, "", "")
    assert failure.value.diagnostics["reason"] == "尺寸标注与引用原文不一致"


def test_dimension_failure_identifies_exact_annotation_field_and_cell():
    _, _, _, diagnostic = invalid_fixture()
    assert diagnostic["path"] == "lines.0.dimension_evidence.2"
    assert diagnostic["field"] == "thickness_mm"
    assert diagnostic["cell"] == "D70" and diagnostic["sheet"] == "清单"
    assert diagnostic["source_item"] == "46"
    assert diagnostic["product"] == "无障碍电梯"
    assert diagnostic["value"] == "5" and diagnostic["unit"] == "mm"


def test_decimal_normalization_does_not_bypass_original_cell_quote():
    raw, files, texts = source_fixture()
    raw["lines"][0]["dimension_evidence"][2]["text"] = "板材厚度0.5mm"
    with pytest.raises(ExtractionValidationFailure) as failure:
        validate_extraction(raw, files, texts, "", "")
    assert failure.value.diagnostics["reason"] == "尺寸文字来源无效"
    assert failure.value.diagnostics["cell"] == "D70"


def test_recovery_cannot_clear_correct_width_to_fix_bad_thickness(monkeypatch):
    raw, files, texts, diagnostic = invalid_fixture()
    fixed, _, _ = source_fixture()
    seen = turns(monkeypatch, [
        call("read_extraction_source", file_id=16, page=4),
        call("mark_dimension_unreadable", line_index=0, evidence_index=0, reason="标注不清"),
        call("repair_extraction_line", line_index=0, line=fixed["lines"][0]),
    ])
    cache, _, _, _ = checkpoint()
    result, _, _ = recover_extraction(raw, diagnostic, files, texts, [], "", "", cache, "batch")
    feedback = json.loads(seen[2][-1]["content"])
    assert "不是当前出错尺寸" in feedback["error"]
    assert feedback["diagnostics"]["evidence_index"] == 2
    assert result.lines[0].width_mm == 35 and result.lines[0].thickness_mm == Decimal("0.5")


def test_saved_failure_revalidates_without_model_calls_even_after_budget_exhausted(monkeypatch):
    raw, files, texts = source_fixture()
    cache, _, charges, events = checkpoint()
    cache.recoveries["batch"] = {"pending": raw, "diagnostic": {
        "stage": "dimension_evidence", "line_index": 0, "reason": "尺寸标注与引用原文不一致"},
        "rounds": settings().extraction_recovery_rounds, "read_pages": []}

    def forbidden(*args, **kwargs):
        raise AssertionError("Revalidation must not call the model")

    monkeypatch.setattr(deepseek, "agent_turn", forbidden)
    result, attempts, usage = recover_extraction(raw, {}, files, texts, [], "", "", cache, "batch")
    assert attempts == 0 and usage == {}
    assert result.lines[0].thickness_mm == Decimal("0.5")
    assert sum(item.get("total_tokens", 0) for item in charges) == 0
    assert events[-1]["result"]["revalidated"]


def test_recovery_failure_message_exposes_location_and_field(monkeypatch):
    raw, files, texts, diagnostic = invalid_fixture()
    monkeypatch.setattr(settings(), "extraction_recovery_rounds", 1)
    turns(monkeypatch, [call("read_extraction_source", file_id=16, page=4)])
    cache, _, _, _ = checkpoint()
    with pytest.raises(deepseek.ModelFailure) as failure:
        recover_extraction(raw, diagnostic, files, texts, [], "", "", cache, "batch")
    for expected in ("第46项", "无障碍电梯", "清单", "D70", "板材厚度", "5mm", "尺寸标注与引用原文不一致"):
        assert expected in str(failure.value)
    assert "quantity_evidence" not in str(failure.value)


def test_worker_exposes_actionable_failure_in_chat_and_progress(client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "excel_native_first", False)
    raw, _, texts, _ = invalid_fixture()
    source = json.loads(texts[16, 4])
    workbook = Workbook()
    workbook.active.title = "清单"
    for row in [*source["header_rows"], *source["rows"]]:
        for cell in row["cells"]:
            workbook.active[cell["cell"]] = cell["value"]
    data = io.BytesIO()
    workbook.save(data)
    workbook.close()
    job = client.post("/api/jobs", data={"title": "Dimension failure"},
        files={"files": ("numbered.xlsx", data.getvalue())}).json()
    file_id = job["files"][0]["id"]
    for line in raw["lines"]:
        for evidence in [*line["evidence"], *line["dimension_evidence"]]:
            evidence.update(file_id=file_id, page=1)

    def handler(request):
        payload = json.loads(request.content)
        answer = (call("read_extraction_source", file_id=file_id, page=1) if payload.get("tools")
                  else {"content": json.dumps(raw)})
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "tool_calls" if payload.get("tools") else "stop", "message": answer}],
            "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_recovery_rounds", 1)
    response = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert response.status_code == 200, response.text
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting"
    for expected in ("第46项", "无障碍电梯", "D70", "板材厚度", "5mm"):
        assert expected in run["message"]
        assert expected in current["messages"][-1]["text"]
    progress = client.get(f'/api/jobs/{job["id"]}/progress').json()
    assert progress["run"]["message"] == run["message"]
    assert current["quotes"] == []
