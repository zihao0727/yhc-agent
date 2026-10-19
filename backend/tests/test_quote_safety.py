from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.quote_engine import build_quote
from app.quote_presentation import sql_progress
from app.requirements_schemas import RequirementLine
from app.quote_safety import validate_area_basis, material_family, material_thickness
from test_pricing_proposals import order, proposal, submit, catalog_parts  # noqa: F401


def test_required_rate_evidence_blocks_agent_rates_but_preserves_administrator_price(order):
    db, job, _ = order
    assert submit(order, proposal(order))["saved"]
    raw = {**job.requirements[0], "rate_evidence_required": True}
    quote = build_quote(db, [raw], automatic=True)
    assert quote["lines"][0]["amount"] is None
    assert quote["lines"][0]["diagnostics"][0]["code"] == "unsupported_rate"
    raw.update(manual_unit_price="120", manual_price_note="管理员核定整套制作价")
    assert build_quote(db, [raw], automatic=True)["total"] == "240.00"


def test_component_cannot_relabel_whole_letter_layout_as_material_area():
    from app.requirements_schemas import EstimateCost, Evidence
    from app.pricing_errors import PricingError
    line = RequirementLine(id="letters", product="存包处", rate_evidence_required=True,
        width_mm=496, height_mm=753, evidence=[{"file_id": 1, "page": 1, "text": "T8mm铝板切割立体字"}],
        components=[{"object_id": "plate", "name": "字牌板材", "width_mm": 496,
                     "height_mm": 753, "quantity_per_item": 1}])
    cost = EstimateCost(label="材料", rate="100", rate_unit="m2", target="component",
                        target_index=0, measurement="component_area", kind="material",
                        reason="按组合外框计算")
    with pytest.raises(PricingError, match="文字组合外框"):
        validate_area_basis(line, cost)
    line.components[0].area_m2 = Decimal("0.1")
    line.components[0].evidence = [Evidence(file_id=1, page=2, text="实际用料面积0.1平方米")]
    validate_area_basis(line, cost)


@pytest.mark.parametrize("component", [False, True])
def test_shaped_cutouts_need_material_area_even_after_material_description_correction(component):
    from app.requirements_schemas import EstimateCost, Evidence
    from app.pricing_errors import PricingError
    line = RequirementLine(id="icons", product="洗手间标识", rate_evidence_required=True,
        material="304#不锈钢", width_mm=80, height_mm=100,
        notes="原图为独立人形图标组合，80×100mm是图标组合外框",
        components=[{"object_id": "icons", "name": "图标", "width_mm": 80,
                     "height_mm": 100, "quantity_per_item": 1}])
    cost = EstimateCost(label="材料", rate="420", rate_unit="m2", kind="material",
        target="component" if component else "item", target_index=0 if component else None,
        measurement="component_area" if component else "item_area", reason="外框面积")
    source = "T3mm304#不锈钢切割造型，拉丝电镀黄铜色"
    with pytest.raises(PricingError, match="切割造型的外框"):
        validate_area_basis(line, cost, source_text=source)
    target = line.components[0] if component else line
    target.area_m2 = Decimal("0.003")
    target.evidence = [Evidence(file_id=1, page=1, text="实际用料面积0.003平方米")]
    validate_area_basis(line, cost, source_text=source)
    target.area_m2 = None
    line.manual_unit_price = Decimal("100")
    validate_area_basis(line, cost, source_text=source)


def test_cutting_description_alone_does_not_imply_separate_icons():
    from app.requirements_schemas import EstimateCost
    line = RequirementLine(id="plate", product="蚀刻标牌", rate_evidence_required=True,
        material="304#不锈钢切割造型", width_mm=178, height_mm=200)
    cost = EstimateCost(label="材料", rate="280", rate_unit="m2", kind="material",
                        target="item", measurement="item_area", reason="整板面积")
    validate_area_basis(line, cost)


def test_required_rate_evidence_allows_applicable_catalog_costs(order):
    db, job, _ = order
    _, plan = catalog_parts(order)
    job.requirements = [{**job.requirements[0], "rate_evidence_required": True}]
    assert submit(order, plan)["saved"]


def test_saved_panel_only_costs_cannot_cover_a_framed_illuminated_assembly(order):
    db, job, _ = order
    _, plan = catalog_parts(order)
    assert submit(order, plan)["saved"]
    raw = deepcopy(job.requirements[0])
    raw.update(rate_evidence_required=True,
               notes="亚克力发光面板；不锈钢边框；圆管吊挂；内藏LED光源")
    quote = build_quote(db, [raw], automatic=True)
    assert quote["lines"][0]["amount"] is None
    assert quote["lines"][0]["diagnostics"][0]["code"] == "missing_component_costs"
    assert raw["estimate"] == job.requirements[0]["estimate"]
    raw.update(manual_unit_price="350", manual_price_note="管理员核定含所有部件的整套价格")
    assert build_quote(db, [raw], automatic=True)["total"] == "700.00"


def test_material_only_inlay_draft_is_retained_but_not_counted_as_finished_quote(order):
    db, job, _ = order
    job.requirements = [{**job.requirements[0], "difficulty": "C"}]
    _, plan = catalog_parts(order)
    plan["costs"] = [cost for cost in plan["costs"] if cost["kind"] == "material"]
    assert submit(order, plan)["saved"]
    raw = deepcopy(job.requirements[0])
    raw["notes"] = "地面镶嵌齐平"
    quote = build_quote(db, [raw], automatic=True)
    assert quote["lines"][0]["amount"] is None
    assert quote["lines"][0]["diagnostics"][0]["code"] == "missing_manufacturing_costs"
    assert "镶嵌" in str(quote["lines"][0]["blockers"])
    job.requirements = [raw]
    result = submit(order, plan)
    assert not result["saved"] and "镶嵌" in str(result)
    assert job.requirements[0]["estimate"] == raw["estimate"]


def test_letter_layout_cannot_bypass_area_guard_by_changing_target_to_item():
    from app.requirements_schemas import EstimateCost, Evidence
    from app.pricing_errors import PricingError
    line = RequirementLine(id="letters", product="存包处", rate_evidence_required=True,
        pricing_family="material", width_mm=496, height_mm=753,
        evidence=[Evidence(file_id=1, page=1, text="T8mm铝板切割立体字")])
    cost = EstimateCost(label="材料", rate="100", rate_unit="m2", target="item",
                        measurement="item_area", kind="material", reason="按组合外框计算")
    with pytest.raises(PricingError, match="整组外框"):
        validate_area_basis(line, cost)
    line.area_m2 = Decimal("0.1")
    line.evidence.append(Evidence(file_id=1, page=2, text="实际用料面积0.1平方米"))
    validate_area_basis(line, cost)


def test_layout_dimensions_cannot_price_saved_plan_but_manual_price_remains_authoritative(order):
    db, job, _ = order
    plan = proposal(order)
    assert submit(order, plan)["saved"]
    raw = deepcopy(job.requirements[0])
    raw["difficulty"] = "C"
    raw["uncertainties"] = ["8227*10405mm疑为整体地面指引范围，需核图"]
    raw["width_mm"], raw["height_mm"] = "8227", "10405"
    raw["estimate"]["assumptions"] = []
    quote = build_quote(db, [raw], automatic=True)
    assert quote["known_subtotal"] == "0"
    assert quote["lines"][0]["diagnostics"][0]["code"] == "layout_area_not_material"
    raw.update(manual_unit_price="6207", manual_price_note="人工核定整套加工价，范围已确认")
    assert build_quote(db, [raw], automatic=True)["total"] == "12414.00"
    validate_area_basis(RequirementLine(id="rectangle", product="矩形整板", width_mm=8227, height_mm=10405))
    validate_area_basis(RequirementLine(id="net", product="地面指引", area_m2="0.2",
        uncertainties=["整体布置范围待核"], evidence=[{
            "file_id": 1, "page": 1, "text": "图纸标注实际用料面积0.2平方米"}]))


@pytest.mark.parametrize("catalog_material,catalog_thickness,code", [
    ("亚克力", "5", "catalog_material_mismatch"),
    ("PC板", "1.5", "catalog_thickness_mismatch"),
])
def test_saved_catalog_material_and_thickness_must_match_target(order, catalog_material, catalog_thickness, code):
    db, job, _ = order
    price, plan = catalog_parts(order)
    price.material = catalog_material
    price.thickness_mm = Decimal(catalog_thickness)
    db.flush()
    raw = deepcopy(job.requirements[0])
    raw.update(material="5mm透明PC板", thickness_mm="5")
    job.requirements = [raw]
    result = submit(order, plan)
    assert not result["saved"]
    assert result["diagnostics"][0]["code"] == code


def test_empty_catalog_process_cannot_be_renamed_and_combined_process_supports_silkscreen(order):
    db, job, _ = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "无", "process_1_price": "0",
                        "process_2": "蚀刻填漆/丝印", "process_2_price": "20"}
    db.flush()
    job.requirements = [{**job.requirements[0], "process": "丝网印刷"}]
    plan["costs"][1]["label"] = "丝网印刷"
    assert "工艺为“无”" in submit(order, plan)["blockers"][0]
    plan["costs"][1]["catalog_part"] = "process_2_price"
    assert submit(order, plan)["saved"]
    raw = deepcopy(job.requirements[0])
    raw["estimate"]["costs"] = [raw["estimate"]["costs"][0]]
    raw["difficulty"] = "C"
    raw["estimate"]["costs"][0]["label"] = "材料含丝网印刷"
    quote = build_quote(db, [raw], automatic=True)
    assert quote["known_subtotal"] == "0"
    assert quote["lines"][0]["diagnostics"][0]["code"] == "missing_manufacturing_costs"


def test_catalog_unit_inheritance_does_not_bypass_layout_area_guard(order):
    db, job, _ = order
    _, plan = catalog_parts(order)
    assert submit(order, plan)["saved"]
    raw = deepcopy(job.requirements[0])
    raw.update(width_mm="8227", height_mm="10405",
               uncertainties=["整体地面指引范围尚待核图"])
    raw["estimate"].update(contract_version=2, assumptions=[])
    for cost in raw["estimate"]["costs"]:
        cost.update(rate_unit="unknown", measurement="auto",
                    quantity_formula="=width_mm*height_mm/1000000")
    quote = build_quote(db, [raw], automatic=True)
    assert quote["total"] is None
    assert quote["lines"][0]["diagnostics"][0]["code"] == "layout_area_not_material"


def test_same_row_duplicate_blocking_matches_live_and_quote_totals(order):
    db, job, _ = order
    assert submit(order, proposal(order))["saved"]
    duplicate = {**deepcopy(job.requirements[0]), "id": "duplicate"}
    requirements = [job.requirements[0], duplicate]
    quote = build_quote(db, requirements, automatic=True)
    progress = sql_progress(db, SimpleNamespace(requirements=requirements, revision=1))
    assert quote["known_subtotal"] == progress["known_subtotal"] == "0"
    assert quote["priced_count"] == progress["priced_count"] == 0
    assert all("同一原始清单行" in line["blockers"][-1] for line in quote["lines"])
    other = deepcopy(duplicate)
    for evidence in other["evidence"]:
        evidence["cell"] = evidence["cell"].replace("2", "3")
    # Equal names and quantities alone do not establish duplicate physical rows.
    assert build_quote(db, [job.requirements[0], other], automatic=True)["priced_count"] == 2


def test_sources_include_same_row_material_remark_and_conflict_blocks_automatic_price(order):
    from app.models import CustomerFile
    from app.pricing_proposals import prepare_packet
    from sqlalchemy import select
    db, job, _ = order
    file = db.scalar(select(CustomerFile).where(CustomerFile.job_id == job.id))
    # A real source cell is added through the workbook import, not the model's evidence text.
    import io
    from openpyxl import Workbook
    from app.customer_files import prepare_file
    workbook = Workbook()
    workbook.active.append(["序号", "产品名称", "数量", "单位", "说明", "备注"])
    workbook.active.append([1, "地面指引", 1, "套",
        "规格尺寸1000*500mm；材质T10.0mm实心黄铜，切割造型并防氧化处理", "钛金板"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    prepared = prepare_file(buffer.getvalue(), "conflict.xlsx")
    for key, value in prepared.items():
        setattr(file, key, value)
    db.flush()
    raw = deepcopy(job.requirements[0])
    raw.update(material="实心黄铜", quantity=1, evidence=[
        {"file_id": file.id, "page": 1, "sheet": "Sheet", "cell": "A2", "text": "1"}])
    job.requirements = [raw]
    packet = prepare_packet(db, job, raw)
    assert next(s for s in packet["sources"] if s["cell"] == "F2")["text"] == "钛金板"
    result = submit(order, proposal(order))
    assert not result["saved"]
    assert any(d["code"] == "conflicting_source_material" for d in result["diagnostics"])


def test_material_recognition_does_not_confuse_plating_color_with_base():
    assert material_family("T5.0mm304#不锈钢电镀黄铜色") == "stainless"
    assert material_family("φ5.0mm透明PC板") == "pc"
    assert material_thickness("5mm透明亚克力+金属环") == Decimal("5")
    assert material_thickness("1.规格尺寸:300*300mm\n2.5mm透明亚克力\n3.其它:综合考虑") == Decimal("5")
    assert material_thickness("2.5mm透明亚克力") == Decimal("2.5")
    from app.quote_safety import validate_catalog_target
    cost = SimpleNamespace(kind="finished", label="逐字库价", target="item")
    price = SimpleNamespace(unit="cm", material="镀锌板", thickness_mm=Decimal("5"))
    with pytest.raises(ValueError, match="不匹配"):
        validate_catalog_target(price, RequirementLine(id="one", material="304#不锈钢"), cost, 0)
    cost = SimpleNamespace(kind="material", label="部件材料", target="component", target_index=0)
    price = SimpleNamespace(unit="m2", material="H62黄铜", thickness_mm=Decimal("1"))
    with pytest.raises(ValueError, match="板厚尚未明确"):
        validate_catalog_target(price, RequirementLine(id="one", material="黄铜"), cost, 0)
    with pytest.raises(ValueError, match="材质尚未明确"):
        validate_catalog_target(price, RequirementLine(id="one"), cost, 0)


def test_missing_inventory_rows_prevent_live_and_final_completeness(order):
    db, job, _ = order
    assert submit(order, proposal(order))["saved"]
    job.extraction = {"source_agent": {"inventory": {"expected_rows": 2, "issues": [
        {"file_id": 1, "page": 1, "sheet": "Sheet", "row": 3, "kind": "missing"}]}}}
    quote = build_quote(db, job.requirements, automatic=True, extraction=job.extraction)
    progress = sql_progress(db, job)
    assert quote["known_subtotal"] == progress["known_subtotal"] == "140.00"
    assert quote["total"] is None and not progress["complete"]


def test_inventory_restore_never_adds_quantities_or_selects_conflicting_plans(order):
    from app.native_excel import file_pages
    from app.models import CustomerFile
    from scripts.repair_native_inventory import restore_rows
    from sqlalchemy import select
    db, job, _ = order
    assert submit(order, proposal(order))["saved"]
    original = deepcopy(job.requirements[0])
    duplicate = {**deepcopy(original), "id": "duplicate"}
    duplicate["estimate"]["costs"][0]["rate"] = "1000"
    job.requirements = [original, duplicate]
    file = db.scalar(select(CustomerFile).where(CustomerFile.job_id == job.id))
    _, pages = file_pages(file)
    native = [line for result, _ in pages.values() for line in result.lines]
    missing = native[0].model_copy(deep=True)
    missing.product, missing.source_item = "Missing sign", "2"
    for evidence in [*missing.evidence, *missing.quantity_evidence, *missing.dimension_evidence]:
        evidence.cell = evidence.cell.replace("2", "3")
    rows, mapping, recovered = restore_rows(job, file.id, [*native, missing])
    assert len(rows) == 2 and rows[0]["quantity"] == 2
    assert rows[0]["estimate"] is None
    assert recovered == ["2"] and mapping[rows[0]["id"]] == [original["id"], "duplicate"]
    assert job.requirements == [original, duplicate]
