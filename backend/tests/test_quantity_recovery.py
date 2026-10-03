import json
import io
import threading
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from openpyxl import load_workbook

from app import agent_worker, deepseek
from app.config import settings
from app.customer_excel import normalize_evidence, table_measurement, validate_excel_rows
from app.customer_files import prepare_file
from app.extraction_checkpoints import ExtractionCheckpoint
from app.quantity_validation import normalize_quantity_values, validate_measurement_sources
from app.requirements_schemas import ExtractedLine, ExtractionResult
from test_customer_excel import workbook_bytes
from test_deepseek import configure_transport
from test_redis_agent import redis_worker


def mixed_workbook_bytes(image_count=1):
    workbook = load_workbook(io.BytesIO(workbook_bytes(image_count=image_count)))
    sheet = workbook["客户明细"]
    sheet["H1"] = "单位"
    for row, value, unit in [(2, 1.5, "米"), (3, 2.5, "平方米"), (4, 4, "件")]:
        sheet[f"E{row}"] = value
        sheet[f"H{row}"] = unit
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def mixed_response(request):
    parts = json.loads(request.content)["messages"][1]["content"]
    text = next(p["text"] for p in parts if "原始文本/单元格" in p.get("text", ""))
    source = json.loads(text.split(":\n", 1)[1])
    file_id = int(text.split(",")[0].split("=")[1])
    if source["sheet"] != "客户明细":
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"lines":[]}'}}], "usage": {"total_tokens": 10}})
    payload = {"lines": [
        {"product": "项目1", "quantity": 1.5,
         "evidence": [{"file_id": file_id, "page": 1, "cell": "B2", "sheet": "客户明细", "text": "项目1"}],
         "quantity_evidence": [{"file_id": file_id, "page": 1, "cell": "G2", "sheet": "客户明细",
                                "text": "数量1.5个", "quantity": "1.5"}]},
        {"product": "项目2", "quantity": 1,
         "evidence": [{"file_id": file_id, "page": 1, "cell": "B3", "sheet": "客户明细", "text": "项目2"}],
         "quantity_evidence": [{"file_id": file_id, "page": 1, "cell": "E3", "sheet": "客户明细",
                                "text": "2.5", "quantity": 1}]}]}
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
        "message": {"content": json.dumps(payload)}}], "usage": {"total_tokens": 10}})


def source(value="2", unit="件"):
    # Different column order and header offset; no customer-specific sheet or row assumptions.
    return {"sheet": "Mixed", "header_rows": [{"row": 5, "cells": [
        {"cell": "D5", "value": "产品名称"}, {"cell": "B5", "value": "数量"}, {"cell": "E5", "value": "单位"}]}],
        "rows": [
            {"row": 7, "cells": [{"cell": "D7", "value": "panel"}, {"cell": "B7", "value": value},
                                 {"cell": "E7", "value": unit}]},
            {"row": 8, "cells": [{"cell": "D8", "value": "other"}, {"cell": "B8", "value": "2"},
                                 {"cell": "E8", "value": "件"}]}],
        "images": [{"row": 7, "cell": "C7"}, {"row": 8, "cell": "C8"}]}


def requirement(cell="B7", text="2", quantity=2, **changes):
    return ExtractedLine.model_validate({
        "product": "panel", "quantity": quantity, "width_mm": 500, "height_mm": 400,
        "evidence": [{"file_id": 3, "page": 1, "sheet": "Mixed", "cell": "D7", "text": "panel"}],
        "quantity_evidence": [{"file_id": 3, "page": 1, "sheet": "Mixed", "cell": cell,
                              "text": text, "quantity": quantity}],
        **changes})


def reconcile(line, sources):
    deepseek.validate_quantity_evidence(line, {(3, 1): json.dumps(sources)}, recover_semantic=True)
    return validate_excel_rows(ExtractionResult(lines=[line]), {(3, 1): sources})


@pytest.mark.parametrize("value,unit,result,kind", [
    ("1", "米", "1", "m"), ("1.5", "m", "1.5", "m"), ("250", "cm", "2.50", "m"),
    ("600", "毫米", ".600", "m"), ("2.5", "平方米", "2.5", "m2"),
    ("2", "㎡", "2", "m2"), ("2", "m²", "2", "m2"), ("500", "cm2", ".0500", "m2"),
    ("3.25米", "", "3.25", "m"),
])
def test_engineering_quantity_is_preserved_and_never_becomes_piece_count(value, unit, result, kind):
    current = source(value, unit)
    line = requirement(quantity=1, text=value)
    checked = reconcile(line, current)
    row = checked.lines[0]
    assert row.quantity is None
    assert row.width_mm == 500 and row.height_mm == 400
    assert row.source_measurements[0].value == Decimal(result)
    assert row.source_measurements[0].unit == kind
    assert row.quantity_evidence[0].validation_status == "rejected"
    assert len(checked.lines) == 2


@pytest.mark.parametrize("cell,value,text", [("C7", "2", "数量：2个"), ("D7", "数量：2件", "数量：2件")])
def test_real_diagram_or_remark_quantity_is_not_forced_into_a_count_column(cell, value, text):
    current = source()
    if cell == "D7":
        current["rows"][0]["cells"][0]["value"] = value
    line = requirement(cell=cell, text=text)
    checked = reconcile(line, current).lines[0]
    assert checked.quantity == 2
    assert checked.quantity_evidence[0].validation_status == "verified"
    assert checked.quantity_evidence[0].source == ("image" if cell == "C7" else "text")


def test_unknown_annotation_keeps_project_and_does_not_stop_other_rows():
    line = requirement(cell="C7", text="数量标注不清楚", quantity=1)
    current = source("", "")
    result = reconcile(line, current)
    assert result.lines[0].quantity is None
    assert result.lines[0].quantity_evidence[0].validation_status == "rejected"
    assert result.lines[1].quantity == 2
    assert result.lines[0].width_mm == 500


def test_incorrect_model_count_can_be_recovered_from_real_table_without_erasing_claim():
    result = reconcile(requirement(quantity=9, text="9"), source()).lines[0]
    assert result.quantity == 2
    assert result.quantity_evidence[0].quantity == 9
    assert result.quantity_evidence[0].validation_status == "rejected"
    assert result.quantity_evidence[1].quantity == 2
    assert result.quantity_evidence[1].validation_status == "verified"


def test_real_diagram_and_table_conflict_remains_unknown_and_revalidation_is_idempotent():
    line = requirement(cell="C7", text="数量：3件", quantity=3)
    current = source()
    result = reconcile(line, current)
    assert result.lines[0].quantity is None
    before = result.model_dump()
    result = reconcile(result.lines[0], current)
    assert result.model_dump() == before


def test_same_project_row_resolves_repeated_bare_count_but_no_row_never_guesses():
    line = requirement(cell="", text="2")
    current = source()
    normalize_evidence(line, current, 3, 1)
    assert line.quantity_evidence[0].cell == "B7"
    line.evidence[0].cell = ""
    line.evidence[0].text = "unmatched project"
    line.quantity_evidence[0].cell = ""
    normalize_evidence(line, current, 3, 1)
    assert line.quantity_evidence[0].cell == ""


@pytest.mark.parametrize("changes", [
    {"cell": "B8"}, {"cell": "B999"}, {"sheet": "forged"}, {"page": 2}, {"file_id": 99},
])
def test_quantity_recovery_never_allows_foreign_or_invented_sources(changes):
    line = requirement()
    line.quantity_evidence[0] = line.quantity_evidence[0].model_copy(update=changes)
    with pytest.raises(ValueError):
        deepseek.validate_quantity_evidence(line, {(3, 1): json.dumps(source())}, recover_semantic=True)


@pytest.mark.parametrize("text,count", [
    ("数量：２件", 2), ("QTY: 2 pcs", 2), ("Quantity = 2 pieces", 2), ("制作数量：2套", 2),
])
def test_text_annotations_work_across_images_and_pdf_without_xlsx_json_parsing(text, count):
    line = requirement(text=text, quantity=count)
    line.evidence[0].sheet = ""
    line.quantity_evidence[0].sheet = ""
    deepseek.validate_quantity_evidence(line, {(3, 1): f"PDF text: {text}"}, recover_semantic=True)
    assert line.quantity == count and line.quantity_evidence[0].validation_status == "verified"


@pytest.mark.parametrize("text", ["数量：1 米", "数量1.5个", "数量：1 m2", "尺寸1mm", "数量未读清"])
def test_bad_annotation_is_local_review_instead_of_batch_failure(text):
    line = requirement(text=text, quantity=1)
    line.quantity_evidence[0].sheet = ""
    deepseek.validate_quantity_evidence(line, {(3, 1): text}, recover_semantic=True)
    assert line.quantity is None and line.quantity_evidence[0].validation_status == "rejected"


@pytest.mark.parametrize("value,expected,raw", [
    ("2件", 2, ""), ("2.0 pcs", 2, ""), ("2.5", None, "2.5"),
    ("1.5米", None, "1.5米"), (1.5, None, "1.5"), (None, None, ""),
])
def test_numeric_adapter_does_not_truncate_fractional_counts(value, expected, raw):
    original = {"lines": [{"quantity": value, "quantity_evidence": [{
        "file_id": 3, "page": 1, "text": str(value), "quantity": value}]}]}
    normalize_quantity_values(original)
    line = ExtractedLine.model_validate(original["lines"][0])
    assert line.quantity == expected
    assert line.quantity_evidence[0].quantity == expected
    assert line.quantity_evidence[0].raw_quantity == raw


@pytest.mark.parametrize("value", ["=1+2", "two or three", {}, [], True])
def test_numeric_adapter_never_executes_or_guesses_ambiguous_input(value):
    raw = {"lines": [{"quantity": value}]}
    original = deepcopy(raw)
    normalize_quantity_values(raw)
    assert raw == original


@pytest.mark.parametrize("value", [True, False])
def test_boolean_count_never_silently_becomes_one(value):
    with pytest.raises(ValueError):
        requirement(quantity=value)


@pytest.mark.parametrize("evidence", [None, {}, "invalid", 1])
def test_numeric_adapter_leaves_malformed_evidence_for_schema_validation(evidence):
    raw = {"lines": [{"quantity": "1.5", "quantity_evidence": evidence}]}
    normalize_quantity_values(raw)
    assert raw["lines"][0]["quantity"] is None
    assert raw["lines"][0]["quantity_evidence"] == evidence
    with pytest.raises(ValueError):
        ExtractedLine.model_validate({"product": "panel", **raw["lines"][0]})


@pytest.mark.parametrize("cell", ["B8", "B999"])
def test_unknown_count_still_requires_valid_same_project_provenance(cell):
    line = requirement(cell=cell, quantity=None)
    with pytest.raises(ValueError) as error:
        deepseek.validate_quantity_evidence(line, {(3, 1): json.dumps(source())}, recover_semantic=True)
    assert error.value.recoverable is False


@pytest.mark.parametrize("changes", [{"formula": "=1+2"}, {"value": "999999999999999999999"}])
def test_unverified_formula_or_out_of_range_measurement_never_aborts_row_fallback(changes):
    current = source("1", "米")
    current["rows"][0]["cells"][1].update(changes)
    assert table_measurement(current, 7, 3, 1) is None
    result = validate_excel_rows(ExtractionResult(lines=[requirement(quantity=1, quantity_evidence=[])]), {(3, 1): current})
    assert result.lines[0].quantity is None


def test_full_xlsx_extraction_saves_local_quantity_issues_in_one_call_and_restores_checkpoint(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(drawing=True), "mixed.xlsx"))
    calls, saved = [], []

    def handler(request):
        calls.append(1)
        payload = {"lines": [
            {"product": "项目1", "quantity": 1.5, "width_mm": 500,
             "evidence": [{"file_id": 41, "page": 1, "cell": "B2", "sheet": "客户明细", "text": "项目1"}],
             "quantity_evidence": [{"file_id": 41, "page": 1, "cell": "G2", "sheet": "客户明细",
                                    "text": "数量1.5个", "quantity": "1.5"}]},
            {"product": "项目2", "quantity": 9,
             "evidence": [{"file_id": 41, "page": 1, "cell": "B3", "sheet": "客户明细", "text": "项目2"}],
             "quantity_evidence": [{"file_id": 41, "page": 1, "cell": "E3", "sheet": "客户明细",
                                    "text": "9", "quantity": 9}]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(payload)}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("quantity-v3", {}, lambda cache, usage: saved.append(deepcopy(cache)), lambda: None)
    result, attempts, usage = deepseek.extract(
        [file], "", [], "", _page_scope={(41, 1)}, _checkpoint=checkpoint, _excel_plans={})
    assert len(calls) == attempts == 1
    assert usage["total_tokens"] == 10 and len(result.lines) == 3
    assert all(line.quantity == 2 for line in result.lines)
    assert any(e.validation_status == "rejected" for e in result.lines[0].quantity_evidence)
    restored = ExtractionCheckpoint("quantity-v3", saved[-1], lambda *args: None, lambda: None)
    again, attempts, usage = deepseek.extract(
        [file], "", [], "", _page_scope={(41, 1)}, _checkpoint=restored, _excel_plans={})
    assert attempts == 0 and usage == {} and len(calls) == 1
    assert again.model_dump() == result.model_dump()


@pytest.mark.parametrize("value,unit", [("999", "m"), ("9", "m2")])
def test_original_table_measurement_overrides_model_claim(value, unit):
    line = requirement(quantity=1, source_measurements=[{
        "value": value, "unit": unit, "evidence": [
            {"file_id": 3, "page": 1, "sheet": "Mixed", "cell": "B7", "text": "999"}]}])
    checked = reconcile(line, source("1.5", "米")).lines[0]
    assert len(checked.source_measurements) == 1
    assert checked.source_measurements[0].value == Decimal("1.5")
    assert checked.source_measurements[0].unit == "m"


def test_piece_count_row_does_not_preserve_model_fabricated_engineering_quantity():
    line = requirement(source_measurements=[{"value": "999", "unit": "m", "evidence": [
        {"file_id": 3, "page": 1, "sheet": "Mixed", "cell": "B7", "text": "999"}]}])
    checked = reconcile(line, source()).lines[0]
    assert checked.quantity == 2 and checked.source_measurements == []
    assert any("模型工程量" in warning for warning in checked.uncertainties)


@pytest.mark.parametrize("changes", [
    {"file_id": 99}, {"page": 2}, {"cell": "B8"}, {"cell": "B999"}, {"sheet": "fake"}])
def test_measurement_provenance_is_bound_to_project(changes):
    evidence = {"file_id": 3, "page": 1, "sheet": "Mixed", "cell": "B7", "text": "1.5", **changes}
    line = requirement(source_measurements=[{"value": "1.5", "unit": "m", "evidence": [evidence]}])
    with pytest.raises(ValueError):
        validate_measurement_sources(line, {(3, 1): json.dumps(source("1.5", "米"))})


def test_multi_batch_mixed_units_merge_and_replay_without_additional_calls(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(mixed_workbook_bytes(image_count=13), "mixed.xlsx"))
    calls, saved = [], []

    def handler(request):
        calls.append(1)
        return mixed_response(request)

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("multi-quantity-v3", {},
        lambda cache, usage: saved.append(deepcopy(cache)), lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=checkpoint, _excel_plans={})
    assert len(result.lines) == 4
    assert len(calls) == attempts == 4 and usage["total_tokens"] == 40
    by_name = {line.product: line for line in result.lines}
    assert by_name["项目1"].quantity is None and by_name["项目2"].quantity is None
    assert by_name["项目3"].quantity == 4
    assert by_name["项目1"].source_measurements[0].value == Decimal("1.5")
    assert by_name["项目2"].source_measurements[0].value == Decimal("2.5")
    assert any(e.validation_status == "rejected" for e in by_name["项目1"].quantity_evidence)
    assert len(by_name["项目1"].source_measurements) == 1
    restored = ExtractionCheckpoint("multi-quantity-v3", saved[-1], lambda *args: None, lambda: None)
    again, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored, _excel_plans={})
    assert again.model_dump() == result.model_dump()
    assert attempts == 0 and usage == {} and len(calls) == 4


@pytest.mark.parametrize("background", [False, True])
def test_uploaded_mixed_units_finish_recognition_without_quantity_failure(
        client, monkeypatch, redis_worker, background):
    job = client.post("/api/jobs", data={"title": "混合计量上传验收"},
                      files={"files": ("mixed.xlsx", mixed_workbook_bytes(image_count=13))}).json()
    calls = []

    def handler(request):
        calls.append(1)
        return mixed_response(request)

    configure_transport(monkeypatch, handler)
    if background:
        monkeypatch.setattr(settings(), "agent_background", True)

        def deny_quote(*args):
            raise deepseek.ModelFailure("模拟报价阶段结束", diagnostics={"retryable": False})

        monkeypatch.setattr(deepseek, "agent_turn", deny_quote)
        response = client.post(f'/api/jobs/{job["id"]}/agent', json={
            "revision": job["revision"], "intent": "extract", "allow_external_processing": True})
        assert response.status_code == 200, response.text
        agent_worker.run_pending_once(threading.Event())
        current = client.get(f'/api/jobs/{job["id"]}').json()
        assert any(s["tool"] == "extract_requirements" for s in current["agent_runs"][0]["steps"])
        assert "数量依据校验失败" not in current["agent_runs"][0]["message"]
    else:
        response = client.post(f'/api/jobs/{job["id"]}/extract', json={
            "revision": job["revision"], "allow_external_processing": True})
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["runs"][0]["status"] == "succeeded"
    assert len(calls) == 4 and len(current["requirements"]) == 4
    by_name = {line["product"]: line for line in current["requirements"]}
    assert by_name["项目1"]["quantity"] is None and by_name["项目2"]["quantity"] is None
    assert by_name["项目3"]["quantity"] == 4
    assert by_name["项目1"]["source_measurements"][0]["value"] == "1.5"
    assert by_name["项目2"]["source_measurements"][0]["unit"] == "m2"
    assert all(not line["confirmed"] for line in current["requirements"])


@pytest.mark.parametrize("changes", [
    {"file_id": 999}, {"page": 99}, {"cell": "H999"}, {"sheet": "fake"}, {"cell": "E3"}])
def test_requirements_api_rejects_fabricated_measurement_sources(client, changes):
    job = client.post("/api/jobs", data={"title": "工程量来源"},
                      files={"files": ("mixed.xlsx", mixed_workbook_bytes())}).json()
    file_id = job["files"][0]["id"]
    evidence = {"file_id": file_id, "page": 1, "sheet": "客户明细", "cell": "E2", "text": "1.5", **changes}
    line = {"id": "test", "product": "项目1", "evidence": [
        {"file_id": file_id, "page": 1, "sheet": "客户明细", "cell": "B2", "text": "项目1"}],
        "source_measurements": [{"value": "1.5", "unit": "m", "evidence": [evidence]}]}
    response = client.put(f'/api/jobs/{job["id"]}/requirements', json={
        "revision": job["revision"], "lines": [line], "reason": "核对来源"})
    assert response.status_code == 422
    assert client.get(f'/api/jobs/{job["id"]}').json()["requirements"] == []


def test_cross_row_quantity_failure_returns_actionable_line_and_cell(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(drawing=False), "cross-row.xlsx"))

    def handler(request):
        payload = {"lines": [{"product": "项目1",
            "evidence": [{"file_id": 41, "page": 1, "sheet": "客户明细", "cell": "B2", "text": "项目1"}],
            "quantity_evidence": [{"file_id": 41, "page": 1, "sheet": "客户明细",
                                   "cell": "E3", "quantity": 2, "text": "2"}]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(payload)}}]})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={})
    detail = failure.value.diagnostics["failures"][-1]
    assert detail["code"] == "quantity_other_row"
    assert detail["path"] == "lines.0.quantity_evidence.0"
    assert "lines.0.quantity_evidence.0" in str(failure.value)
    assert "E3" in str(failure.value)
