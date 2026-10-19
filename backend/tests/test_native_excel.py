import io
import json
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app import deepseek, native_excel
from app import excel_skill_planner
from app.config import settings
from app.customer_files import prepare_file
from app.extraction_checkpoints import ExtractionCheckpoint, ExtractionYield
from test_redis_agent import redis_worker  # noqa: F401


def make_file(rows=None, file_id=41):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["序号", "产品名称", "数量", "单位", "描述", "宽度(cm)", "材质"])
    for row in rows or [[1, "入口标识", 2, "个", "文字高300mm，底板另定", 20, "铝板"]]:
        sheet.append(row)
    data = io.BytesIO()
    workbook.save(data)
    workbook.close()
    return SimpleNamespace(id=file_id, **prepare_file(data.getvalue(), "native.xlsx"))


def test_default_route_reads_facts_without_model(monkeypatch):
    file = make_file()
    monkeypatch.setattr(excel_skill_planner, "plan_files", lambda *a, **k: pytest.fail("model planner called"))
    result, attempts, usage = deepseek.extract([file], "报价给我吧", [], "")
    line, = result.lines
    assert attempts == 0 and usage == {}
    assert line.product == "入口标识" and line.quantity == 2
    assert line.width_mm == Decimal(200) and line.height_mm is None
    assert line.material == "铝板"
    assert "文字高300mm" in line.notes
    assert line.dimension_evidence[0].value == 20
    assert line.dimension_evidence[0].unit == "cm"
    assert all(e.file_id == 41 for e in line.evidence)
    assert line.quantity_evidence


@pytest.mark.parametrize("unit", ["m", "m2"])
def test_engineering_measurements_are_not_piece_counts(unit):
    result, _, _ = deepseek.extract([make_file([[1, "墙面涂装", 819.17, unit]])], "", [], "")
    line, = result.lines
    assert line.quantity is None
    assert line.source_measurements[0].value == Decimal("819.17")
    assert line.source_measurements[0].unit == unit


def test_same_file_cache_rebinds_sources_without_caching_edits():
    first = make_file()
    _, pages = native_excel.file_pages(first)
    pages[1][0].lines[0].product = "用户修改"
    second = SimpleNamespace(**{**vars(first), "id": 99})
    _, reused = native_excel.file_pages(second)
    result, hit = reused[1]
    assert hit and result.lines[0].product == "入口标识"
    assert all(e.file_id == 99 for e in result.lines[0].evidence)
    assert result.lines[0].dimension_evidence[0].file_id == 99
    assert result.lines[0].quantity_evidence[0].file_id == 99


def test_new_business_instructions_and_previous_edits_are_not_ignored():
    file = make_file()
    assert native_excel.extract_native([file], "所有数量改成3", [], "", None) is None
    assert native_excel.extract_native([file], "", [], "改为不锈钢", None) is None
    assert native_excel.extract_native([file], "", [{"product": "用户修改"}], "", None) is None


def test_ambiguous_page_falls_back_without_reprocessing_standard_pages(monkeypatch):
    file = make_file([[i, f"标识{i}", 2, "个"] for i in range(1, 24)] + [["安装", None, "待定"]])
    _, pages = native_excel.file_pages(file)
    assert pages[1][0] is not None and pages[2][0] is None
    scopes = []
    original = deepseek.extract

    def fallback(files, brief, previous, supplementary, **kwargs):
        scopes.append(kwargs["_page_scope"])
        return native_excel.ExtractionResult(lines=[]), 1, {"total_tokens": 10}

    monkeypatch.setattr(deepseek, "extract", fallback)
    result, attempts, usage = original([file], "", [], "")
    assert len(result.lines) == 19
    assert scopes == [{(41, 2)}] and attempts == 1 and usage["total_tokens"] == 10


def test_yield_resume_preserves_completed_page():
    file = make_file([[i, f"标识{i}", 2, "个"] for i in range(1, 24)])
    state = {}
    checkpoint = None

    def persist(snapshot, usage):
        state.update(snapshot)
        if checkpoint.last_saved:
            raise ExtractionYield()

    checkpoint = ExtractionCheckpoint("test", {}, persist, lambda: None)
    with pytest.raises(ExtractionYield):
        deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    resumed = ExtractionCheckpoint("test", state, lambda *a: None, lambda: None)
    result, attempts, _ = deepseek.extract([file], "", [], "", _checkpoint=resumed)
    assert len(result.lines) == 23 and attempts == 0
    assert len({line.source_item for line in result.lines}) == 23


def test_sql_batch_store_progress_is_reused_and_children_override_parent():
    file = make_file()
    _, pages = native_excel.file_pages(file)
    base = pages[1][0]
    parent = base.model_copy(deep=True)
    parent.lines[0].material = "旧识别"
    child = base.model_copy(deep=True)
    child.lines[0].material = "已核实材料"
    saved = {
        json.dumps([[[41, 1]], None, [2, 3, 4]]): parent,
        json.dumps([[[41, 1]], None, [2]]): child,
    }
    store = SimpleNamespace(keys=set(saved), source_keys=set(),
                            load=lambda key: saved[key].model_dump(mode="json"))
    checkpoint = ExtractionCheckpoint("test", {}, lambda *a: None, lambda: None, store=store)
    result = native_excel.reuse_batches(base, checkpoint, 41, 1)
    assert len(result.lines) == 1
    assert result.lines[0].material == "已核实材料"


def test_section_title_excluded_but_same_named_product_retained():
    result, _, _ = deepseek.extract([make_file([
        [None, "基础"], [1, "基础", 3, "个", "混凝土基础"], ["合计"]
    ])], "", [], "")
    assert len(result.lines) == 1 and result.lines[0].quantity == 3


def test_pending_fee_footer_keeps_question_without_model():
    file = make_file([[1, "门牌", 2, "个"], ["安装", None, None, None, None, None, None, None, "待定"],
                      ["发票", None, None, None, None, None, None, None, "待确认"]])
    result, attempts, _ = deepseek.extract([file], "", [], "")
    assert attempts == 0 and len(result.lines) == 1
    assert any("安装" in question and "A3" in question for question in result.questions)
    assert any("发票" in question and "I4" in question for question in result.questions)
    assert all(row.status == "non_product" for row in result.source_coverage if row.row in (3, 4))


@pytest.mark.parametrize("row", [
    ["安装", None, None, None, None, None, None, None, "1500"],
    ["安装", None, None, None, None, None, None, None, "=1+2"],
    ["安装", None, None, None, "现场高空", None, None, None, "待定"],
])
def test_nontrivial_fee_footer_still_requires_interpretation(row):
    file = make_file([[1, "门牌", 2, "个"], row])
    assert native_excel.file_pages(file)[1][1][0] is None


def test_large_workbook_keeps_all_rows_without_model():
    result, attempts, _ = deepseek.extract(
        [make_file([[i, f"标识{i}", 2, "个"] for i in range(1, 126)])], "", [], "")
    assert len(result.lines) == 125 and attempts == 0
    assert {line.source_item for line in result.lines} == {str(i) for i in range(1, 126)}


def test_drawings_retain_source_and_defer_dimensions():
    from test_customer_excel import workbook_bytes
    file = SimpleNamespace(id=41, **prepare_file(workbook_bytes(), "drawing.xlsx"))
    _, pages = native_excel.file_pages(file)
    line = pages[1][0].lines[0]
    assert line.width_mm is None and line.height_mm is None
    assert native_excel.IMAGE_NOTE in line.uncertainties
    assert any(e.source == "image" and e.cell == "G2" for e in line.evidence)


def test_reference_filename_bypasses_native():
    file = make_file()
    file.filename = "供应商报价.xlsx"
    assert native_excel.extract_native([file], "", [], "", None) is None


def test_existing_reference_plan_is_respected():
    file = make_file()
    checkpoint = ExtractionCheckpoint("test", {}, lambda *a: None, lambda: None)
    checkpoint.plans = {str(file.id): {"sheets": [{"sheet": "Sheet", "purpose": "price_reference"}]}}
    assert native_excel.extract_native([file], "", [], "", checkpoint) is None


def test_broken_cache_is_rebuilt():
    file = make_file()
    manifest, _ = native_excel.file_pages(file)
    path = native_excel.cache_path(file, manifest)
    path.write_text('{"pages": {}}', encoding="utf-8")
    _, pages = native_excel.file_pages(file)
    assert len(pages[1][0].lines) == 1 and not pages[1][1]


def test_different_aliases_for_quantity_require_semantic_choice():
    file = make_file()
    manifest, _ = native_excel.file_pages(file)
    source = manifest["pages"][0]
    source["header_rows"][0]["cells"].append({"cell": "H1", "value": "采购数量"})
    source["rows"][1]["cells"].append({"cell": "H2", "value": "99"})
    assert native_excel.native_page(41, 1, source) is None


def test_worker_native_progress_and_resume(client, monkeypatch, redis_worker):
    from app import agent_worker
    from app.customer_files import storage_path
    file = make_file([[i, f"标识{i}", 2, "个"] for i in range(1, 24)])
    original = storage_path(file.storage_key) / "original"
    job = client.post("/api/jobs", data={"title": "Native progress"},
        files={"files": ("native.xlsx", original.read_bytes())}).json()
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_slice_batches", 1)
    monkeypatch.setattr(deepseek, "get_key", lambda: "test")
    monkeypatch.setattr(excel_skill_planner, "plan_files",
                        lambda *a, **k: pytest.fail("standard table must not invoke model"))
    response = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert response.status_code == 200
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(current["requirements"]) == 19
    progress = current["agent_runs"][0]["usage"]["extraction_progress"]
    assert "程序直接读取单元格" in progress["detail"]
    for _ in range(4):
        if current["status"] != "processing":
            break
        agent_worker.run_pending_once(threading.Event())
        current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert len(current["requirements"]) == 23
