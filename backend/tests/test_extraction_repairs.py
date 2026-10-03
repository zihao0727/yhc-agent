import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from app import deepseek
from app import agent_worker
from app.config import settings
from app.customer_excel import normalize_evidence, validate_evidence, validate_excel_rows
from app.customer_files import prepare_file, storage_path
from app.extraction_checkpoints import ExtractionCheckpoint
from app.requirements_schemas import ExtractedLine, ExtractionResult
from test_customer_excel import workbook_bytes
from test_deepseek import configure_transport
from test_redis_agent import redis_worker


def source_fixture():
    return {"sheet": "浙江世界贸易中心导示系统标识制作清单",
            "header_rows": [{"row": 2, "cells": [
                {"cell": "B2", "value": "名称"}, {"cell": "F2", "value": "数量"},
                {"cell": "G2", "value": "单位"}]}],
            "rows": [{"row": 3, "cells": [{"cell": "B3", "value": "户外A座造型标识"},
                                         {"cell": "F3", "value": "1"}, {"cell": "G3", "value": "个"}]},
                     {"row": 4, "cells": [{"cell": "B4", "value": "腰线"},
                                         {"cell": "F4", "value": "1"}, {"cell": "G4", "value": "米"}]}],
            "images": [{"row": 3, "cell": "C3"}, {"row": 4, "cell": "C4"}]}


def test_missing_sheet_absolute_address_and_unique_image_anchor_are_resolved():
    source = source_fixture()
    line = ExtractedLine.model_validate({"evidence": [
        {"file_id": 8, "page": 1, "cell": "$B$3", "text": "户外A座造型标识"}],
        "dimension_evidence": [{"file_id": 8, "page": 1, "field": "height_mm",
                                "value": 2500, "text": "2500mm", "source": "image"}],
        "letter_groups": [{"count": 1, "evidence": [
            {"file_id": 8, "page": 1, "text": "A", "source": "image"}]}]})
    normalize_evidence(line, source, 8, 1)
    assert line.evidence[0].cell == "B3"
    for evidence in [line.evidence[0], line.dimension_evidence[0], line.letter_groups[0].evidence[0]]:
        validate_evidence(evidence, source)
    assert line.dimension_evidence[0].cell == "C3"
    assert line.letter_groups[0].evidence[0].cell == "C3"


@pytest.mark.parametrize("sheet,cell", [("编造表名", "B3"), ("", "B999"), ("", "另一张表!B3"),
                                       ("", "B3:F3")])
def test_explicit_wrong_provenance_is_never_overwritten(sheet, cell):
    source = source_fixture()
    line = ExtractedLine.model_validate({"evidence": [
        {"file_id": 8, "page": 1, "sheet": sheet, "cell": cell, "text": "户外A座造型标识"}]})
    normalize_evidence(line, source, 8, 1)
    with pytest.raises(ValueError):
        validate_evidence(line.evidence[0], source)


def test_project_row_disambiguates_repeated_quantity_without_guessing_other_rows():
    source = source_fixture()
    line = ExtractedLine.model_validate({"evidence": [
        {"file_id": 8, "page": 1, "text": "户外A座造型标识"},
        {"file_id": 8, "page": 1, "cell": "'" + source["sheet"] + "'!$C$3", "text": "图纸"}],
        "quantity_evidence": [{"file_id": 8, "page": 1, "text": "1", "quantity": 1}]})
    normalize_evidence(line, source, 8, 1)
    assert [e.cell for e in line.evidence] == ["B3", "C3"]
    assert line.quantity_evidence[0].cell == "F3"


def test_excel_count_column_needs_no_fabricated_quantity_sentence():
    source = source_fixture()
    line = ExtractedLine.model_validate({"evidence": [
        {"file_id": 8, "page": 1, "sheet": source["sheet"], "cell": "B3", "text": "户外A座造型标识"}],
        "quantity_evidence": [{"file_id": 8, "page": 1, "sheet": source["sheet"],
                              "cell": "F3", "text": "1", "quantity": 1}]})
    deepseek.validate_quantity_evidence(line, {(8, 1): json.dumps(source, ensure_ascii=False)})
    assert line.quantity == 1
    line.quantity_evidence[0].quantity = 2
    with pytest.raises(ValueError):
        deepseek.validate_quantity_evidence(line, {(8, 1): json.dumps(source, ensure_ascii=False)})


def test_row_fallback_recovers_real_count_but_not_length_quantity():
    result = validate_excel_rows(ExtractionResult(lines=[]), {(8, 1): source_fixture()})
    assert len(result.lines) == 2
    assert result.lines[0].quantity == 1 and result.lines[0].quantity_evidence[0].cell == "F3"
    assert result.lines[1].quantity is None


def test_financial_summary_never_becomes_product_even_when_model_extracts_it():
    source = source_fixture()
    source["rows"].extend([{"row": 20, "cells": [{"cell": "B20", "value": "包装运费"}]},
                           {"row": 22, "cells": [{"cell": "B22", "value": "税费1%"}]}])
    result = ExtractionResult(lines=[ExtractedLine(product="税费1%", evidence=[
        {"file_id": 8, "page": 1, "sheet": source["sheet"], "cell": "B22", "text": "税费1%"}])])
    checked = validate_excel_rows(result, {(8, 1): source})
    assert {line.product for line in checked.lines} == {"户外A座造型标识", "腰线"}
    assert any("B22" in question for question in checked.questions)


def test_table_count_conflicts_and_formula_values_are_not_silently_resolved():
    source = source_fixture()
    result = ExtractionResult(lines=[ExtractedLine(product="牌", quantity=3, evidence=[
        {"file_id": 8, "page": 1, "sheet": source["sheet"], "cell": "B3", "text": "牌"}])])
    checked = validate_excel_rows(result, {(8, 1): source})
    assert checked.lines[0].quantity is None and any("数量" in w for w in checked.lines[0].uncertainties)
    checked = validate_excel_rows(checked, {(8, 1): source})
    assert checked.lines[0].quantity is None
    source["rows"][0]["cells"][1]["formula"] = "=1"
    fallback = validate_excel_rows(ExtractionResult(lines=[]), {(8, 1): source})
    assert fallback.lines[0].quantity is None


@pytest.mark.parametrize("cell,quantity", [("B3", 1), ("F4", 1), ("F3", 9)])
def test_non_count_cell_length_units_or_different_value_cannot_become_count(cell, quantity):
    source = source_fixture()
    line = ExtractedLine.model_validate({"evidence": [{"file_id": 8, "page": 1, "text": "项目"}],
        "quantity_evidence": [{"file_id": 8, "page": 1, "sheet": source["sheet"], "cell": cell,
                              "text": f"数量：{quantity}个", "quantity": quantity}]})
    with pytest.raises(ValueError):
        deepseek.validate_quantity_evidence(line, {(8, 1): json.dumps(source)})


@pytest.mark.parametrize("text", ["数量：1", "数量 1 件", "数量：1台"])
def test_explicit_diagram_count_without_required_unit(text):
    line = ExtractedLine.model_validate({"evidence": [{"file_id": 1, "page": 1, "text": "图"}],
        "quantity_evidence": [{"file_id": 1, "page": 1, "text": text, "quantity": 1, "source": "image"}]})
    deepseek.validate_quantity_evidence(line)
    assert line.quantity == 1


@pytest.mark.parametrize("text", ["数量：1m", "数量1.5个", "尺寸：1mm"])
def test_diagram_length_or_decimal_cannot_become_count(text):
    line = ExtractedLine.model_validate({"evidence": [{"file_id": 1, "page": 1, "text": "图"}],
        "quantity_evidence": [{"file_id": 1, "page": 1, "text": text, "quantity": 1}]})
    with pytest.raises(ValueError):
        deepseek.validate_quantity_evidence(line)


@pytest.mark.parametrize("value,unit,expected", [
    ("80mm", "mm", "80"), (" 2.2 m ", "m", "2.2"), ("80x15", "mm", "80x15"),
    ("80mm", "cm", "80mm"), ("H=80", "mm", "H=80"), (None, "mm", None)])
def test_dimension_adapter_only_changes_unambiguous_unit_scalar(value, unit, expected):
    raw = {"lines": [{"dimension_evidence": [{"value": value, "unit": unit}]}]}
    deepseek.normalize_dimension_values(raw)
    assert raw["lines"][0]["dimension_evidence"][0]["value"] == expected


def test_unknown_dimension_keeps_provenance_and_clears_unsupported_main_value():
    line = ExtractedLine.model_validate({"height_mm": 999,
        "evidence": [{"file_id": 1, "page": 1, "text": "项目"}],
        "dimension_evidence": [{"file_id": 1, "page": 1, "field": "height_mm",
                                "value": None, "text": "高度标注不清晰"}]})
    assert deepseek.validate_dimension_evidence(line, {}) == set()
    assert line.height_mm is None
    assert line.dimension_evidence[0].value is None
    assert any("height_mm" in warning for warning in line.uncertainties)
    deepseek.validate_dimension_evidence(line, {})
    assert len(line.uncertainties) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_unknown_annotation_does_not_hide_an_independent_readable_dimension(reverse):
    evidence = [{"file_id": 1, "page": 1, "field": "height_mm",
                 "value": None, "text": "标注不清晰"},
                {"file_id": 1, "page": 1, "field": "height_mm",
                 "value": 2.2, "unit": "m", "text": "高2.2m"}]
    line = ExtractedLine.model_validate({"evidence": [{"file_id": 1, "page": 1, "text": "项目"}],
                                        "dimension_evidence": evidence[::-1] if reverse else evidence})
    assert deepseek.validate_dimension_evidence(line, {}) == {"height_mm"}
    assert line.height_mm == 2200


@pytest.mark.parametrize("source,page,text", [
    ("image", 2, "标注不清晰"), ("text", 1, "编造的原文")])
def test_unknown_dimension_still_validates_project_page_and_text(source, page, text):
    line = ExtractedLine.model_validate({"evidence": [{"file_id": 1, "page": 1, "text": "项目"}],
        "dimension_evidence": [{"file_id": 1, "page": page, "field": "height_mm",
                                "value": None, "text": text, "source": source}]})
    with pytest.raises(ValueError):
        deepseek.validate_dimension_evidence(line, {})


def test_excel_schema_repair_accepts_unknown_dimension_without_dropping_the_row(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(drawing=True), "drawing.xlsx"))
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        parts = payload["messages"][1]["content"]
        source = next(json.loads(p["text"].split(":\n", 1)[1]) for p in parts
                      if "原始文本/单元格" in p.get("text", ""))
        image = source["images"][0]
        evidence = {"file_id": 41, "page": 1, "sheet": source["sheet"], "cell": image["cell"],
                    "source": "image", "text": "高度不清晰", "field": "height_mm", "value": None}
        calls.append(payload)
        if len(calls) == 1:
            evidence["file_id_note"] = "多余字段"
        result = {"lines": [{"product": "图纸项目", "height_mm": 999,
            "evidence": [{"file_id": 41, "page": 1, "sheet": source["sheet"],
                          "cell": image["cell"], "source": "image", "text": "项目图纸"}],
            "dimension_evidence": [evidence]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(result)}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    saved = []
    checkpoint = ExtractionCheckpoint("unknown-dimension", {},
        lambda cache, usage: saved.append(cache), lambda: None)
    from app.excel_skill_planner import plan_files
    plans, _, _ = plan_files([file], checkpoint)
    result, attempts, usage = deepseek.extract(
        [file], "", [], "", _page_scope={(41, 1)}, _checkpoint=checkpoint, _excel_plans=plans)
    line = next(line for line in result.lines if line.product == "图纸项目")
    assert line.height_mm is None and line.dimension_evidence[0].value is None
    assert attempts == len(calls) == 2 and usage["total_tokens"] == 20
    assert "extra_forbidden" in calls[1]["messages"][-1]["content"]
    restored = ExtractionCheckpoint("unknown-dimension", saved[-1], lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract(
        [file], "", [], "", _page_scope={(41, 1)}, _checkpoint=restored, _excel_plans=plans)
    line = next(line for line in result.lines if line.product == "图纸项目")
    assert line.height_mm is None and line.dimension_evidence[0].value is None
    assert attempts == 0 and usage == {} and len(calls) == 2


@pytest.mark.parametrize("invalid", [
    {"value": []}, {"value": {}}, {"value": -1}, {"value": "80×15"},
    {"value": None, "file_id_num": 41},
    {"value": None, "file_id": 99}, {"value": None, "cell": "C999"}])
def test_unknown_dimension_does_not_relax_other_schema_or_excel_source_checks(monkeypatch, invalid):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(drawing=True), "drawing.xlsx"))

    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        source = next(json.loads(p["text"].split(":\n", 1)[1]) for p in parts
                      if "原始文本/单元格" in p.get("text", ""))
        image = source["images"][0]
        evidence = {"file_id": 41, "page": 1, "sheet": source["sheet"], "cell": image["cell"],
                    "source": "image", "text": "尺寸不清晰", "field": "height_mm", **invalid}
        result = {"lines": [{"evidence": [{"file_id": 41, "page": 1, "sheet": source["sheet"],
            "cell": image["cell"], "source": "image", "text": "项目"}], "dimension_evidence": [evidence]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(result)}}]})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _page_scope={(41, 1)}, _excel_plans={})
    assert failure.value.attempts == 2 and failure.value.diagnostics["retryable"] is False
    if failure.value.diagnostics["failures"][-1]["stage"] == "schema":
        assert "校验位置：lines.0.dimension_evidence.0." in str(failure.value)


def test_failed_later_batch_reuses_successful_batch_without_repaying(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(image_count=13), "many.xlsx"))
    saved, seen = [], []

    def persist(cache, usage):
        saved.append((json.loads(json.dumps(cache)), dict(usage)))

    checkpoint = ExtractionCheckpoint("same-input", {}, persist, lambda: None)

    def handler(request):
        parts = json.loads(request.content)["messages"][1]["content"]
        source = next(json.loads(p["text"].split(":\n", 1)[1]) for p in parts
                      if "原始文本/单元格" in p.get("text", ""))
        seen.append([i["storage"] for i in source["images"]])
        if len(seen) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"lines":[]}'}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    assert failure.value.diagnostics["retryable"] is True
    assert len(saved) == 2 and saved[-1][1]["total_tokens"] == 10
    restored = ExtractionCheckpoint("same-input", saved[-1][0], persist, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert len(result.lines) == 4 and attempts == 3 and usage["total_tokens"] == 30
    assert seen[0] == [f"excel-image-{i}.png" for i in range(1, 7)]
    assert seen[1] == seen[2] == [f"excel-image-{i}.png" for i in range(7, 13)]
    assert all(batch != seen[0] for batch in seen[2:])
    changed = ExtractionCheckpoint("changed-input", saved[0][0], persist, lambda: None)
    assert not changed.batches


def test_unresolved_schema_failures_are_not_transient(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(drawing=False), "bad.xlsx"))
    configure_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"finish_reason": "stop", "message": {"content": '{"lines":[{"price":99}]}'}}],
        "usage": {"total_tokens": 10}}))
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "")
    assert failure.value.diagnostics["retryable"] is False
    assert failure.value.usage["total_tokens"] == 20
    assert "字段" in str(failure.value) and "停止自动重试" in str(failure.value)


def enqueue_excel(client, monkeypatch):
    job = client.post("/api/jobs", data={"title": "分批识别"},
                      files={"files": ("many.xlsx", workbook_bytes(image_count=13))}).json()
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-test-key")
    response = client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": job["revision"], "intent": "extract", "allow_external_processing": True})
    assert response.status_code == 200, response.text
    return response.json()


def test_worker_schema_failure_waits_instead_of_scheduling_more_paid_calls(
        client, monkeypatch, redis_worker):
    job = enqueue_excel(client, monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"lines":[{"price":99}]}'}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    agent_worker.run_pending_once(threading.Event())
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert len(calls) == 2 and run["status"] == "waiting"
    assert run["usage"]["retry_at"] == 0 and "retry_count" not in run["usage"]
    assert "字段" in run["message"] and "存储" not in run["message"]
    assert current["requirements"] == [] and current["quotes"] == []


def test_worker_checkpoints_resume_and_accumulate_all_paid_usage(
        client, monkeypatch, redis_worker, test_db):
    from app.models import AgentRun
    job = enqueue_excel(client, monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"lines":[]}'}}], "usage": {"total_tokens": 10}})

    def denied(*args):
        raise deepseek.ModelFailure("计价连接测试中止", diagnostics={"retryable": False})

    configure_transport(monkeypatch, handler)
    monkeypatch.setattr(deepseek, "agent_turn", denied)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["usage"]["total_tokens"] == 10
    assert run["usage"]["phase"] == "retrying"
    assert "仅重试未完成部分" in run["message"]
    assert "extraction_checkpoint" not in run["usage"]
    with test_db() as db:
        cache = db.get(AgentRun, run["id"]).usage["extraction_checkpoint"]
        assert len(cache["batches"]) == 1
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert len(calls) == 5 and len(current["requirements"]) == 4
    assert run["usage"]["extraction_tokens"] == run["usage"]["total_tokens"] == 40
    assert "extraction_checkpoint" not in client.get(
        f'/api/jobs/{job["id"]}/progress').json()["run"]["usage"]
    with test_db() as db:
        assert len(db.get(AgentRun, run["id"]).usage["extraction_checkpoint"]["batches"]) == 4


def test_stop_during_extraction_prevents_subsequent_batches_and_overwrite(
        client, monkeypatch, redis_worker):
    job = enqueue_excel(client, monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        response = client.post(f'/api/jobs/{job["id"]}/agent/stop', json={
            "revision": job["revision"], "reason": "测试识别期间停止"})
        assert response.status_code == 200
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": '{"lines":[]}'}}], "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(calls) == 1 and current["requirements"] == []
    assert current["agent_runs"][0]["status"] == "cancelled"


def test_manual_retry_reuses_prior_run_batches_for_identical_inputs(
        client, monkeypatch, redis_worker):
    job = enqueue_excel(client, monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        content = '{"lines":[{"price":99}]}' if len(calls) in (2, 3) else '{"lines":[]}'
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": content}}], "usage": {"total_tokens": 10}})

    def denied(*args):
        raise deepseek.ModelFailure("计价连接测试中止", diagnostics={"retryable": False})

    configure_transport(monkeypatch, handler)
    monkeypatch.setattr(deepseek, "agent_turn", denied)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "waiting" and len(calls) == 3
    response = client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": current["revision"], "intent": "extract", "allow_external_processing": True})
    assert response.status_code == 200
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(calls) == 6 and len(current["requirements"]) == 4
    assert current["agent_runs"][0]["usage"]["total_tokens"] == 30
    assert current["agent_runs"][1]["usage"]["total_tokens"] == 30


def test_no_progress_context_encourages_saved_estimate_without_forcing_a_rate(
        client, monkeypatch, redis_worker):
    from test_agent_loop import call, provider
    from test_estimate_pricing import plan
    from test_redis_agent import enqueue
    seen = provider(monkeypatch, [
        call("search_prices", query="未知工艺"),
        call("search_prices", query="未知工艺"),
        call("search_prices", query="未知工艺"),
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote")])
    job = enqueue(client, monkeypatch, 1)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert "连续多轮尚无" in seen[3][0]["content"]
    assert "自主判断" in seen[3][0]["content"]
    assert "优先调用complete_estimate" not in seen[3][0]["content"]
    assert "暂估" in seen[3][0]["content"]
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["agent_runs"][0]["usage"]["no_progress_rounds"] == 1
