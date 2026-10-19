"""Cross-order regression coverage for retrieval, contracts and engineering billing."""
import io
from copy import deepcopy
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.agent_execution import execute_tool, defer_requirement
from app.agent_tools import DeferInput, SearchInput, tool_definitions
from app import agent_work_state as work
from app.catalog_search import search_rows
from app.customer_files import prepare_file
from app.models import AgentRun, Category, CustomerFile, PriceItem, Product, RequirementJob
from app.native_excel import extract_native
from app.pricing_proposals import SubmitProposal, RepairProposal, prepare_packet, catalog_packet
from app.quote_engine import build_quote
from app.requirements_schemas import RequirementLine

from test_pricing_proposals import order, proposal, submit
from test_quantity_recovery import source, requirement, reconcile


def add_price(db, material, thickness, process="无", name="材料及表面处理"):
    category = db.query(Category).filter_by(name="Foundation samples").first()
    if category is None:
        category = Category(name="Foundation samples")
        db.add(category)
        db.flush()
    product = db.query(Product).filter_by(category_id=category.id, name=name).first()
    if product is None:
        product = Product(category_id=category.id, name=name)
        db.add(product)
        db.flush()
    price = PriceItem(product_id=product.id, material=material, thickness_mm=thickness,
                      process=process, unit="m2", amount=100, status="active", price_kind="suggested",
                      attributes={"material_price": "100"})
    db.add(price)
    db.flush()
    return price


@pytest.mark.parametrize("material", ["304#不锈钢", "304 不锈钢", "304＃不锈钢"])
def test_base_grade_outweighs_surface_color_and_wrong_materials(order, material):
    db, _, _ = order
    correct = add_price(db, "304#", Decimal("3"), "电镀")
    for value in ("H62黄铜", "亚克力", "镀锌板", "201#"):
        add_price(db, value, Decimal("3"), "黄铜色电镀")
    add_price(db, "304#", Decimal("5"), "电镀黄铜色")
    raw = {"product": "门牌", "material": material, "thickness_mm": "3",
           "notes": "电镀黄铜色，T3.0mm304#不锈钢", "components": []}
    candidates = catalog_packet(db, raw)
    assert [p["id"] for p in candidates if p["cost_group"] == "material_process"] == [correct.id]
    assert candidates[0]["matched_targets"] == ["item"]


def test_missing_grade_does_not_turn_another_grade_into_a_matching_candidate(order):
    db, _, _ = order
    add_price(db, "304#", Decimal("3"))
    result = catalog_packet(db, {"product": "导视牌", "material": "316#不锈钢", "thickness_mm": "3"})
    assert not [p for p in result if p["cost_group"] == "material_process"]


def test_sizes_and_project_numbers_do_not_become_material_grades(order):
    from app.catalog_matching import material_grade
    db, _, _ = order
    correct = add_price(db, "6061铝", Decimal("3"))
    assert material_grade("铝板，规格304*201mm") is None
    assert material_grade("项目编号201，304#不锈钢") == "304"
    candidates = catalog_packet(db, {"product": "铝板标识", "material": "", "thickness_mm": "3",
                                     "notes": "铝板，规格304*201mm"})
    assert any(p["id"] == correct.id for p in candidates)


def test_each_component_keeps_its_own_thickness_and_a_reserved_candidate(order):
    db, _, _ = order
    acrylic = add_price(db, "亚克力", Decimal("5"))
    steel = add_price(db, "304#", Decimal("3"))
    add_price(db, "亚克力", Decimal("3"))
    candidates = catalog_packet(db, {"product": "组合牌", "material": "",
        "components": [{"object_id": "face", "name": "面板", "material": "亚克力", "thickness_mm": "5"},
                       {"object_id": "back", "name": "背板", "material": "304#不锈钢", "thickness_mm": "3"}]})
    assert next(p for p in candidates if p["id"] == acrylic.id)["matched_targets"] == ["face"]
    assert next(p for p in candidates if p["id"] == steel.id)["matched_targets"] == ["back"]


def test_long_searches_rank_partial_matches_and_material_aliases_preserve_grade(order):
    db, job, run = order
    correct = add_price(db, "304#", Decimal("3"), "电镀")
    add_price(db, "201#", Decimal("3"), "电镀")
    add_price(db, "H62黄铜", Decimal("3"), "电镀")
    total, rows = search_rows(db, "不锈钢 平板标识 需要电镀黄铜色 防指纹", "304 不锈钢")
    assert total == 1 and rows[0][0].id == correct.id
    result = execute_tool(db, job, run, "search_prices", SearchInput(
        query="电镀 防指纹 平板标识", material="304#不锈钢"), set())
    assert result["prices"][0]["id"] == correct.id and result["candidate_only"]
    assert search_rows(db, "没有这个产品")[0] == 0


def test_composite_material_description_reserves_every_explicit_specification(order):
    db, _, _ = order
    expected = [
        add_price(db, "304#", Decimal("1.5")).id,
        add_price(db, "304#", Decimal("1.2")).id,
        add_price(db, "铝板", Decimal("3")).id,
        add_price(db, "亚克力", Decimal("5")).id,
    ]
    wrong = add_price(db, "亚克力", Decimal("3"))
    raw = {"product": "商场楼层信息索引立牌 FLOOR INFORMATION INDEX",
           "material": "1.5mm不锈钢箱体+1.2mm不锈钢箱体；3mm铝板立体字；5mm乳白透光亚克力板"}
    original = deepcopy(raw)
    packet = catalog_packet(db, raw)
    assert set(expected) <= {p["id"] for p in packet}
    assert wrong.id not in {p["id"] for p in packet}
    assert all(p["matched_targets"] == ["item"] for p in packet)
    assert raw == original
    from app.quote_engine import candidate_prices
    line = RequirementLine.model_validate({**raw, "id": "composite"})
    candidates = candidate_prices(db, line, raw["product"])
    assert set(expected) <= {p["id"] for p in candidates}
    assert all(any("需组合计价" in reason for reason in p["mismatches"]) for p in candidates)
    assert candidate_prices(db, line, "绝对不存在的价格") == []


def test_composite_retrieval_keeps_grades_and_does_not_read_size_as_thickness(order):
    from app.catalog_matching import retrieval_targets
    db, _, _ = order
    correct = add_price(db, "304#", Decimal("1.5"))
    wrong = add_price(db, "201#", Decimal("1.5"))
    add_price(db, "铝板", Decimal("3"))
    packet = catalog_packet(db, {"material": "1.5mm304#不锈钢+3mm铝板"})
    assert correct.id in {p["id"] for p in packet}
    assert wrong.id not in {p["id"] for p in packet}
    targets = retrieval_targets({"material": "520x2000mm铝板；5mm亚克力板"})
    assert not any(t["material"] == "2000mm铝板" for t in targets)


def test_rule_lookup_ranks_multiple_words_and_excludes_inactive(order):
    from app.models import PricingRule
    from app.catalog_search import search_rules
    from app.agent_tools import RulesInput
    db, job, run = order
    for index in range(22):
        db.add(PricingRule(name=f"无关说明{index}", content="普通说明", status="active"))
    relevant = PricingRule(name="钣金工艺", content="焊接 35-55元/平方米；喷涂油漆 40-60元/平方米",
                           status="active")
    inactive = PricingRule(name="喷涂 焊接", content="停用计费", status="inactive")
    db.add_all([relevant, inactive])
    db.flush()
    total, rules = search_rules(db, "喷涂 焊接 组装")
    assert total == 1 and rules[0].id == relevant.id
    result = execute_tool(db, job, run, "read_rules", RulesInput(query="喷涂 焊接 组装"), set())
    assert [r["id"] for r in result["rules"]] == [relevant.id]
    assert result["next_offset"] is None
    packet = prepare_packet(db, job, {**job.requirements[0], "process": "焊接和喷涂"})
    assert relevant.id in [r["id"] for r in packet["rules"]]


def test_composite_parameters_expand_without_losing_fields_or_evidence(order):
    from app.pricing_proposals import compile_proposal, source_register, PricingProposal
    db, job, _ = order
    raw = job.requirements[0]
    sources = source_register(db, job, raw)
    source_id = next(iter(sources))
    plan = proposal(order)
    plan["parameters"] = []
    plan["objects"] = []
    for index in range(7):
        identity = f"panel_{index}"
        plan["objects"].append({"id": identity, "kind": "component",
                                "name": identity, "source_ids": [source_id]})
        plan["parameters"].append({
            "target": identity, "target_kind": "component",
            "values": {"width_mm": 500, "height_mm": 200, "thickness_mm": 3, "quantity_per_item": 1},
            "basis": "assumption", "reason": "Synthetic multipart compiler regression",
            "source_ids": [source_id]})
    compiled, _ = compile_proposal(db, job, raw, PricingProposal.model_validate(plan), sources)
    assert len(compiled.assumptions) == 28
    assert len(compiled.proposed_components) == 7
    assert all(a.evidence and a.target_id for a in compiled.assumptions)


def test_wrong_catalog_thickness_is_a_plan_error_not_missing_catalog(order):
    from app.quote_safety import validate_catalog_target
    from app.requirements_schemas import EstimateCost
    from app.pricing_errors import PricingError
    db, _, _ = order
    price = add_price(db, "304#", Decimal("1.2"))
    line = RequirementLine(id="sample", product="箱体", material="304#不锈钢", thickness_mm="1.5")
    cost = EstimateCost(label="箱体材料", rate="100", rate_unit="m2",
                        kind="material", source="agent_estimate", reason="测试错误引用")
    with pytest.raises(PricingError) as failure:
        validate_catalog_target(price, line, cost, 0)
    assert failure.value.diagnostic["cause"] == "plan_repair"


def test_price_search_uses_explicit_thickness_before_pagination(order):
    db, _, _ = order
    for thickness in range(2, 24):
        add_price(db, "201#", Decimal(thickness), name="不锈钢激光切割字")
    correct = add_price(db, "201#", Decimal("1.5"))
    _, rows = search_rows(db, "201# 1.5mm 材料价格", "201#")
    assert rows and rows[0][0].id == correct.id
    assert all(price.thickness_mm == Decimal("1.5") for price, _ in rows)
    _, dimensions = search_rows(db, "表面处理 520x2000mm", "201#")
    assert any(price.id == correct.id for price, _ in dimensions)


def test_negative_process_warning_is_not_applicability_evidence(order):
    from app.catalog_parts import validate_part_process
    db, _, _ = order
    price = add_price(db, "201#", Decimal("1.5"))
    price.attributes = {**price.attributes, "process_1": "烤漆", "process_1_price": "50"}
    line = RequirementLine(id="warning", product="立牌", material="不锈钢", process="喷金属哑光漆",
                           notes="图纸喷漆不能无依据当作烤漆。")
    with pytest.raises(ValueError, match="没有对应工艺依据"):
        validate_part_process(price, "process_1_price", line)
    line.process = "烤漆"
    validate_part_process(price, "process_1_price", line)


def test_composite_materials_cannot_repeat_overall_area_on_one_target(order):
    from app.pricing_proposals import compile_proposal, source_register, PricingProposal
    from app.pricing_errors import PricingError
    db, job, _ = order
    raw = {**job.requirements[0], "material": "1.5mm不锈钢围边；1.2mm不锈钢箱体；5mm亚克力背托"}
    plan = proposal(order)
    plan["costs"].append({**plan["costs"][0], "label": "第二层材料"})
    with pytest.raises(PricingError) as failure:
        compile_proposal(db, job, raw, PricingProposal.model_validate(plan), source_register(db, job, raw))
    assert failure.value.diagnostic["code"] == "composite_material_scope"
    assert failure.value.diagnostic["cause"] == "plan_repair"
    from app.quote_safety import validate_manufacturing
    from app.requirements_schemas import EstimatePlan, EstimateCost
    line = RequirementLine.model_validate(raw)
    line.estimate = EstimatePlan(
        contract_version=3, scope="Saved legacy plan must retain the same validation",
        costs=[EstimateCost(label=label, kind="material", rate="100", rate_unit="m2",
                            source="agent_estimate", reason="Legacy plan test")
               for label in ("Top panel", "Bottom panel")])
    with pytest.raises(PricingError) as runtime_failure:
        validate_manufacturing(line, [], "")
    assert runtime_failure.value.diagnostic["code"] == "composite_material_scope"


def test_power_cost_does_not_cover_requested_led_modules():
    from app.quote_safety import validate_manufacturing
    from app.requirements_schemas import EstimatePlan
    from app.pricing_errors import PricingError
    line = RequirementLine(id="led", product="灯箱", estimate=EstimatePlan(contract_version=3, scope="灯箱"))
    costs = [{"kind": "power", "target": "item", "label": "电源含LED模组",
              "reason": "按电源200元/台计价"}]
    with pytest.raises(PricingError) as failure:
        validate_manufacturing(line, costs, "内置LED模组，色温6000K")
    assert failure.value.diagnostic["code"] == "missing_light_source_cost"
    costs.append({"kind": "material", "target": "component", "label": "LED模组",
                  "reason": "按已核实型号、用量和价格计取光源材料"})
    validate_manufacturing(line, costs, "内置LED模组，色温6000K")


def test_partial_box_panel_cannot_reuse_the_whole_sign_area():
    from app.quote_safety import validate_area_basis
    from app.requirements_schemas import MaterialComponent, EstimateCost
    from app.pricing_errors import PricingError
    line = RequirementLine(id="sign", product="立牌", width_mm="520", height_mm="2000",
        components=[MaterialComponent(name="上部箱体", width_mm="520", height_mm="2000",
                                      area_m2="1.04", material="不锈钢")])
    cost = EstimateCost(label="箱体板材", kind="material", rate="150", rate_unit="m2",
                        target="component", target_index=0, source="agent_estimate",
                        reason="Regression sample", measurement="component_area")
    with pytest.raises(PricingError) as failure:
        validate_area_basis(line, cost)
    assert failure.value.diagnostic["code"] == "partial_component_uses_overall_area"


def test_printing_alias_search_prefers_existing_process_cost_over_letter_catalog(order):
    db, _, _ = order
    letter = add_price(db, "亚克力", Decimal("5"), name="亚克力标识字")
    correct = add_price(db, "亚克力", Decimal("5"))
    correct.attributes = {**correct.attributes, "process_2": "蚀刻填漆/丝印", "process_2_price": "400"}
    db.flush()
    _, rows = search_rows(db, "丝网印刷 标识 亚克力")
    assert rows[0][0].id == correct.id
    assert letter.id in [price.id for price, _ in rows]


def test_advertised_parameter_types_match_runtime_field_contract():
    schema = next(t for t in tool_definitions() if t["function"]["name"] == "submit_pricing_plan"
                  )["function"]["parameters"]["$defs"]
    assert "geometry" not in schema["item_parameters"]["properties"]
    assert "quantity" not in schema["component_parameters"]["properties"]
    assert "depth_mm" in schema["component_parameters"]["properties"]
    assert "count" in schema["letter_group_parameters"]["properties"]
    assert len(schema["ParameterBlock"]["anyOf"]) == 3


@pytest.mark.parametrize("media_type", ["application/pdf", "image/png"])
def test_typed_dimension_and_quantity_sources_keep_base_contract(order, monkeypatch, media_type):
    from app import pricing_proposals
    from app.pricing_proposals import source_register
    from pydantic import ValidationError
    db, job, _ = order
    file = db.query(CustomerFile).filter_by(job_id=job.id).one()
    file.media_type = media_type
    text = "Plate 1000mm, quantity 2"
    monkeypatch.setattr(pricing_proposals, "pdf_page_text", lambda *args: text)
    anchor = {"file_id": file.id, "page": 1, "text": text,
              "source": "text" if media_type == "application/pdf" else "image"}
    raw = {"quantity_evidence": [{**anchor, "quantity": 2, "raw_quantity": "2",
                                 "validation_status": "unchecked"}],
           "dimension_evidence": [{**anchor, "field": "width_mm", "value": "1000",
                                  "unit": "mm", "target": "item", "role": "overall"}]}
    sources = source_register(db, job, raw)
    assert len(sources) == 1
    evidence = next(iter(sources.values()))
    assert evidence.text == text and evidence.source == anchor["source"]
    assert "field" not in evidence.model_dump() and "quantity" not in evidence.model_dump()
    raw["dimension_evidence"][0]["invented_field"] = True
    with pytest.raises(ValidationError):
        source_register(db, job, raw)
    if media_type == "application/pdf":
        raw["dimension_evidence"][0].pop("invented_field")
        monkeypatch.setattr(pricing_proposals, "pdf_page_text", lambda *args: "Unrelated page")
        assert source_register(db, job, raw) == {}


def test_shell_depth_belongs_to_the_component_and_never_borrows_item_depth(order):
    _, job, _ = order
    job.requirements = [{**job.requirements[0], "depth_mm": "1000", "difficulty": "C"}]
    plan = proposal(order)
    source_id = plan["parameters"][0]["source_ids"][0]
    plan["strategy"] = "composite"
    plan["objects"] = [{"id": "shell", "kind": "component", "name": "外壳", "source_ids": [source_id]}]
    plan["parameters"] = [{"target": "shell", "target_kind": "component",
        "values": {"depth_mm": 200, "quantity_per_item": 1}, "basis": "assumption",
        "source_ids": [source_id], "reason": "测试壳深与每套数量，待人工审核"}]
    plan["costs"] = [{"label": "壳体加工", "kind": "process", "target": "shell",
        "measurement": "script", "script": "result = depth_mm / 1000 * component_count",
        "rate": 100, "unit": "m", "reason": "测试按本部件壳深计费，待审核"}]
    result = submit(order, plan)
    assert result["saved"] and result["amount"] == "40.00", result
    assert result["estimate_costs"][0]["parameters"]["depth_mm"] == "200"


def test_component_volume_script_uses_its_own_depth_and_unit(order):
    _, job, _ = order
    job.requirements = [{**job.requirements[0], "depth_mm": "1000", "difficulty": "C"}]
    plan = proposal(order)
    source_id = plan["parameters"][0]["source_ids"][0]
    plan["strategy"] = "composite"
    plan["objects"] = [{"id": "solid", "kind": "component", "name": "实体块", "source_ids": [source_id]}]
    plan["parameters"] = [{"target": "solid", "target_kind": "component",
        "values": {"width_mm": 1000, "height_mm": 500, "depth_mm": 200, "quantity_per_item": 1},
        "basis": "assumption", "source_ids": [source_id], "reason": "测试实体块三边及数量，待人工审核"}]
    plan["costs"] = [{"label": "实体材料", "kind": "material", "target": "solid",
        "measurement": "script", "script": "result = width_mm * height_mm * depth_mm / 1000000000 * component_count",
        "rate": 100, "unit": "m3", "reason": "测试实体长宽深换算立方米，待审核"}]
    result = submit(order, plan)
    assert result["saved"] and result["amount"] == "20.00", result
    assert result["estimate_costs"][0]["rate_unit"] == "m3"


def test_missing_patch_paths_return_add_operation_and_do_not_advance_version(order):
    db, job, run = order
    plan = proposal(order)
    del plan["parameters"][0]["values"]["height_mm"]
    assert not submit(order, plan)["saved"]
    result = execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(
        line_id="one", version=1, edits=[{"path": "/parameters/0/values/height_mm", "value": 500}]), set())
    assert not result["saved"] and result["plan_version"] == 1
    field = next(f for f in result["repair_contract"]["fields"]
                 if f["path"] == "/parameters/0/values/height_mm")
    assert field["suggested_op"] == "add"
    assert work.line_state(job, run, "one")["pricing_proposal"]["version"] == 1
    repaired = execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(
        line_id="one", version=1,
        edits=[{"path": field["path"], "op": field["suggested_op"], "value": 500}]), set())
    assert repaired["saved"]


@pytest.mark.parametrize("unit", ["处", "点", "座", "项", "批"])
def test_count_units_are_retained_without_customer_specific_rules(unit):
    line = reconcile(requirement(quantity=3, text="3"), source("3", unit)).lines[0]
    assert line.quantity == 3 and line.quantity_unit == unit


def engineering_order(db, quantity, unit, human_formula=None):
    wb = Workbook()
    wb.active.title = "工程报价"
    wb.active.append(["产品名称", "单位", "工程量", "说明", "单价", "金额", "备注"])
    wb.active.append(["测试工程", unit, quantity, "按原表工程量计费", human_formula, None, "不含安装"])
    out = io.BytesIO()
    wb.save(out)
    job = RequirementJob(title="Engineering sample", requirements=[], messages=[], extraction={},
                         business_rules_version="legacy", brief="")
    db.add(job)
    db.flush()
    file = CustomerFile(job_id=job.id, **prepare_file(out.getvalue(), "engineering.xlsx"))
    db.add(file)
    db.flush()
    extracted, _, _ = extract_native([file], "", [], "", None)
    job.requirements = [RequirementLine(id="engineering", **extracted.lines[0].model_dump()).model_dump(mode="json")]
    run = AgentRun(job_id=job.id, model="test", steps=[], usage={"durable": True, "active_line_id": "engineering"})
    db.add(run)
    db.flush()
    return job, run


def engineering_plan(unit):
    return {"strategy": "area" if unit == "m2" else "length" if unit == "m" else "labor",
            "scope": "原表工程量及整项固定费用，待审核",
            "costs": [{"label": "主体工程", "kind": "finished", "measurement": "source_quantity",
                       "source_measurement_index": 0, "rate": "20", "unit": unit, "basis": "total",
                       "reason": "测试综合费率，每单位二十元，待审核"},
                      {"label": "固定制作费", "kind": "other", "measurement": "fixed",
                       "rate": "10", "unit": "fixed", "basis": "total", "reason": "测试整项固定制作费，待审核"}]}


@pytest.mark.parametrize("value,source_unit,canonical,expected", [
    (500, "m", "m", "10010.00"), (819.17, "m2", "m2", "16393.40"),
    (250, "cm", "m", "60.00"), (2.5, "立方米", "m3", "60.00"),
    (0.25, "吨", "kg", "5010.00"), (1.5, "小时", "hour", "40.00"),
])
def test_engineering_quantities_price_end_to_end_without_a_fake_piece_count(test_db, value, source_unit, canonical, expected):
    with test_db() as db:
        job, run = engineering_order(db, value, source_unit)
        original = deepcopy(job.requirements)
        result = execute_tool(db, job, run, "submit_pricing_plan", SubmitProposal(
            line_id="engineering", plan=engineering_plan(canonical)), set())
        assert result["saved"], result
        assert result["amount"] == expected and result["unit_price"] == "20.00"
        assert result["billing_unit"] == canonical and Decimal(result["fixed_charges"]) == Decimal("10")
        assert job.requirements[0]["quantity"] is None
        assert job.requirements[0]["source_measurements"] == original[0]["source_measurements"]
        quote = build_quote(db, job.requirements, automatic=True)
        assert quote["complete"] and quote["total"] == expected


@pytest.mark.parametrize("change", ["value", "unit", "row", "file"])
def test_engineering_billing_rejects_tampered_measurement_evidence(test_db, change):
    with test_db() as db:
        job, run = engineering_order(db, 2.5, "m2")
        changed = deepcopy(job.requirements)
        measure = changed[0]["source_measurements"][0]
        if change == "value":
            measure["value"] = "100"
        elif change == "unit":
            measure["unit"] = "m"
        elif change == "row":
            measure["evidence"][0]["cell"] = "C999"
        else:
            measure["evidence"][0]["file_id"] = 99999
        job.requirements = changed
        result = execute_tool(db, job, run, "submit_pricing_plan", SubmitProposal(
            line_id="engineering", plan=engineering_plan(measure["unit"])), set())
        assert not result["saved"] and result["amount"] is None


def test_order_engineering_quantity_cannot_be_multiplied_by_order_count(test_db):
    with test_db() as db:
        job, run = engineering_order(db, 2.5, "m2")
        job.requirements = [{**job.requirements[0], "quantity": 30}]
        result = execute_tool(db, job, run, "submit_pricing_plan", SubmitProposal(
            line_id="engineering", plan=engineering_plan("m2")), set())
        assert result["saved"] and result["amount"] == "60.00"
        job.requirements = [{**job.requirements[0], "estimate": None}]
        plan = engineering_plan("m2")
        plan["costs"][0]["basis"] = "per_piece"
        result = execute_tool(db, job, run, "submit_pricing_plan", SubmitProposal(
            line_id="engineering", plan=plan), set())
        assert not result["saved"]


def test_source_measurement_changes_recalculate_saved_plan_and_stale_source_is_blocked(test_db):
    with test_db() as db:
        job, run = engineering_order(db, 2.5, "m2")
        execute_tool(db, job, run, "submit_pricing_plan", SubmitProposal(
            line_id="engineering", plan=engineering_plan("m2")), set())
        changed = deepcopy(job.requirements)
        changed[0]["source_measurements"][0]["value"] = "50"
        quote = build_quote(db, changed, automatic=True)
        assert quote["lines"][0]["amount"] is None
        assert not quote["complete"]


def test_human_formula_is_a_review_reference_and_never_parameter_evidence(test_db):
    with test_db() as db:
        job, run = engineering_order(db, 2.5, "m2", "=0.12*0.12*1150+10")
        packet = prepare_packet(db, job, job.requirements[0])
        ref = packet["uploaded_price_references"][0]
        assert Decimal(ref["calculated_value"]) == Decimal("26.56")
        assert ref["reference_only"] and not ref["reviewed"] and ref["remarks"] == "不含安装"
        assert "E2" not in {e["cell"] for e in packet["sources"]}
        assert job.requirements[0]["manual_unit_price"] is None


def test_known_order_engineering_quantity_is_not_missing_customer_piece_count(test_db):
    with test_db() as db:
        job, run = engineering_order(db, 2.5, "m2")
        result = defer_requirement(job, run, DeferInput(line_id="engineering", cause="customer_input",
            reason="缺少数量", question="请提供数量", missing_fields=["quantity"]))
        assert result["error"] and result["known_fields"] == ["source_measurements"]
