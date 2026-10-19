import io
import json
from copy import deepcopy
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.agent_execution import execute_tool
from app.agent_context import context_messages
from app import agent_work_state as work
from app.customer_files import prepare_file
from app.models import RequirementJob, CustomerFile, AgentRun, Category, Product, PriceItem
from app.native_excel import extract_native
from app.pricing_proposals import SubmitProposal, RepairProposal, prepare_packet
from app.requirements_schemas import RequirementLine
from app.quote_engine import build_quote
from app.conversation import RequirementChange, changed_requirements
from app.file_workspace import job_root


def test_drawing_binding_requires_current_line_image_and_preserves_facts(order):
    from app.agent_execution import event
    from app.agent_tools import BindDrawingInput
    from app.source_agent_tools import InspectRegion
    from test_large_pdf import document_bytes

    db, job, run = order
    file = CustomerFile(job_id=job.id, **prepare_file(document_bytes(pages=1), "drawing.pdf"))
    db.add(file)
    db.flush()
    original = deepcopy(job.requirements[0])
    job.extraction = {"source_agent": {"reference_observations": {
        "ref": {"product": original["product"], "source_code": "DRAW-01",
                "width_mm": "9999", "quantity": 999,
                "evidence": [{"file_id": file.id, "page": 1, "source": "image",
                              "text": "Old extraction is not an adopted dimension"}]},
    }}}
    packet = prepare_packet(db, job, original)
    candidate = packet["drawing_candidates"][0]
    assert candidate["reference_id"] == "ref" and candidate["candidate_only"]
    assert "width_mm" not in candidate and "quantity" not in candidate
    args = BindDrawingInput(line_id="one", reference_id="ref", file_id=file.id, page=1,
                            reason="Original drawing title and construction match this ordered row")
    with pytest.raises(ValueError, match="view_source_region"):
        execute_tool(db, job, run, "bind_drawing_source", args, set())
    # A different line's successful read must never authorize this line's binding.
    region = InspectRegion(file_id=file.id, page=1, region=[0, 0, 1, 1])
    run.usage = {**run.usage, "active_line_id": "other"}
    viewed = execute_tool(db, job, run, "view_source_region", region, set())
    viewed.pop("_source_images")
    event(run, "view_source_region", region.model_dump(mode="json"), viewed)
    run.usage = {**run.usage, "active_line_id": "one"}
    with pytest.raises(ValueError, match="view_source_region"):
        execute_tool(db, job, run, "bind_drawing_source", args, set())
    work.update_state(job, run, "one",
                      pricing_proposal={"version": 2, "plan": proposal(order)},
                      validated_plan={"old": "validation"}, preview_passed=True)
    previous_plan = deepcopy(work.line_state(job, run, "one")["pricing_proposal"]["plan"])
    viewed = execute_tool(db, job, run, "view_source_region", region, set())
    viewed.pop("_source_images")
    event(run, "view_source_region", region.model_dump(mode="json"), viewed)
    result = execute_tool(db, job, run, "bind_drawing_source", args, set())
    assert result["bound"]
    updated = job.requirements[0]
    assert {key: value for key, value in updated.items() if key != "evidence"} == {
        key: value for key, value in original.items() if key != "evidence"}
    assert updated["evidence"][:-1] == original["evidence"]
    source = next(s for s in result["pricing_packet"]["sources"] if s["file_id"] == file.id)
    assert source["source"] == "image" and source["region"] == [0, 0, 1, 1]
    state = work.line_state(job, run, "one")
    assert state["pricing_proposal"]["plan"] == previous_plan
    assert state["pricing_proposal"]["requires_revalidation"]
    assert state["validated_plan"] is None and not state["preview_passed"]
    execute_tool(db, job, run, "bind_drawing_source", args, set())
    assert len(job.requirements[0]["evidence"]) == len(original["evidence"]) + 1


@pytest.fixture
def order(test_db):
    wb = Workbook()
    wb.active.append(["序号", "产品名称", "数量", "单位", "说明"])
    wb.active.append([1, "铝板标识", 2, "套", "铝板1000*500mm；支架500*200mm，每套3个"])
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    with test_db() as db:
        job = RequirementJob(title="Proposal", business_rules_version="2026-09-23.v1",
                             brief="按资料和规则报价", requirements=[], messages=[], extraction={})
        db.add(job)
        db.flush()
        file = CustomerFile(job_id=job.id, **prepare_file(output.getvalue(), "customer.xlsx"))
        db.add(file)
        db.flush()
        result, _, _ = extract_native([file], "", [], "", None)
        job.requirements = [RequirementLine(id="one", difficulty="B", business_rules_version=job.business_rules_version,
                                            **{k: v for k, v in result.lines[0].model_dump().items()
                                               if k != "difficulty"}).model_dump(mode="json")]
        run = AgentRun(job_id=job.id, model="test", steps=[],
                       usage={"durable": True, "active_line_id": "one"})
        db.add(run)
        db.commit()
        yield db, job, run


def proposal(order):
    db, job, _ = order
    packet = prepare_packet(db, job, job.requirements[0])
    source = next(s["id"] for s in packet["sources"] if s["cell"] == "E2")
    return {"strategy": "area", "scope": "板材加工，不含现场安装及税费",
            "parameters": [{"target": "item", "values": {"width_mm": 1000, "height_mm": 500},
                            "basis": "source", "source_ids": [source], "reason": "原文板材尺寸"}],
            "costs": [
                {"label": "板材", "kind": "material", "measurement": "item_area",
                 "rate": "100", "unit": "m2", "reason": "验收样本给定材料费率，待审核"},
                {"label": "加工", "kind": "process", "measurement": "item_area",
                 "rate": "20", "unit": "m2", "reason": "验收样本给定基础加工费率，待审核"}]}


def submit(order, plan):
    db, job, run = order
    return execute_tool(db, job, run, "submit_pricing_plan",
                        SubmitProposal(line_id="one", plan=plan), set())


def test_parameter_errors_identify_editable_parameter_instead_of_costs(order):
    plan = proposal(order)
    plan["parameters"][0]["values"] = {"geometry": "flat_rectangle"}
    result = submit(order, plan)
    assert not result["saved"]
    assert result["diagnostics"][0]["paths"] == ["/parameters/0/values/geometry"]
    plan["parameters"][0]["values"] = {"width_mm": 1000}
    plan["parameters"][0]["source_ids"] = []
    result = submit(order, plan)
    assert not result["saved"]
    assert result["diagnostics"][0]["paths"] == ["/parameters/0/source_ids"]


def test_user_dimension_reply_reuses_failed_costs_and_recalculates_with_current_facts(order):
    db, job, run = order
    plan = proposal(order)
    del plan["parameters"][0]["values"]["height_mm"]
    assert not submit(order, plan)["saved"]
    original_costs = deepcopy(work.line_state(job, run, "one")["pricing_proposal"]["plan"]["costs"])
    job.requirements = changed_requirements(job.requirements, [
        RequirementChange(line_id="one", field="width_mm", value="1200", reason="User confirmed width"),
        RequirementChange(line_id="one", field="height_mm", value="500", reason="User confirmed height")])
    state = work.line_state(job, run, "one")
    assert state["pricing_proposal"]["plan"]["parameters"] == []
    assert state["pricing_proposal"]["plan"]["costs"] == original_costs
    result = execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(line_id="one",
        version=state["pricing_proposal"]["version"], edits=[
            {"path": "/scope", "value": "按用户确认的新尺寸重算，费用范围保持不变"}]), set())
    assert result["saved"] and result["amount"] == "168.00", result
    assert job.requirements[0]["width_mm"] == "1200"
    assert len(job.requirements[0]["estimate"]["costs"]) == 2


def test_supplemented_component_coexists_with_remaining_new_component(order):
    db, job, run = order
    job.requirements = [{**job.requirements[0], "difficulty": "C"}]
    plan = proposal(order)
    source = plan["parameters"][0]["source_ids"][0]
    plan.update(strategy="composite", objects=[
        {"id": "face", "kind": "component", "name": "铝面板", "source_ids": [source]},
        {"id": "back", "kind": "component", "name": "铝背板", "source_ids": [source]}],
        parameters=[{"target": target, "values": {"height_mm": 500, "quantity_per_item": 1,
                     "material": "铝", "thickness_mm": 3}, "basis": "assumption",
                     "reason": "样本部件参数，用户审核"} for target in ("face", "back")])
    plan["costs"] = [{**plan["costs"][0], "target": target, "measurement": "component_area"}
                     for target in ("face", "back")]
    assert not submit(order, plan)["saved"]
    job.requirements = [{**job.requirements[0], "components": [{
        "object_id": "face", "name": "铝面板", "width_mm": "1200", "height_mm": None,
        "material": "铝", "thickness_mm": "3", "quantity_per_item": None}]}]
    state = work.line_state(job, run, "one")
    assert [obj["id"] for obj in state["pricing_proposal"]["plan"]["objects"]] == ["back"]
    result = execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(line_id="one",
        version=state["pricing_proposal"]["version"], edits=[
            {"path": "/parameters/1/values/width_mm", "op": "add", "value": 1000}]), set())
    assert result["saved"] and result["amount"] == "220.00", result.get("blockers")
    assert [cost["target_index"] for cost in job.requirements[0]["estimate"]["costs"]] == [0, 1]


def test_compact_plan_binds_original_text_calculates_and_saves(order):
    db, job, run = order
    result = submit(order, proposal(order))
    assert result["saved"] and result["amount"] == "140.00"
    assert result["estimate_costs"][1]["difficulty_multiplier"] == "2"
    saved = job.requirements[0]
    assert saved["width_mm"] is None
    assert saved["estimate"]["assumptions"][0]["evidence"][0]["cell"] == "E2"
    assert saved["estimate"]["assumptions"][0]["evidence"][0]["text"].startswith("铝板1000*500mm")
    assert all(c["expected_amount"] is None for c in saved["estimate"]["costs"])
    assert len(list((job_root(job.id) / "pricing" / "one").glob("*.json"))) == 1
    assert build_quote(db, job.requirements, automatic=True)["total"] == "140.00"


def test_local_patch_retains_good_costs_and_rejects_stale_revision(order):
    db, job, run = order
    plan = proposal(order)
    del plan["parameters"][0]["values"]["height_mm"]
    first = submit(order, plan)
    assert not first["saved"] and first["amount"] is None
    assert first["plan_version"] == 1
    assert "/parameters/0/values/height_mm" in [
        path for diagnosis in first.get("diagnostics", []) for path in diagnosis["paths"]]
    original_costs = deepcopy(work.line_state(job, run, "one")["pricing_proposal"]["plan"]["costs"])
    result = execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(line_id="one", version=1,
        edits=[{"path": "/parameters/0/values/height_mm", "op": "add", "value": 500}]), set())
    assert result["saved"] and result["amount"] == "140.00"
    assert work.line_state(job, run, "one")["pricing_proposal"]["plan"]["costs"] == original_costs
    with pytest.raises(ValueError, match="版本"):
        execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(line_id="one", version=1,
            edits=[{"path": "/costs/0/rate", "value": "300"}]), set())


def test_changed_inputs_invalidate_local_plan_patch(order):
    db, job, run = order
    plan = proposal(order)
    del plan["parameters"][0]["values"]["height_mm"]
    submit(order, plan)
    job.requirements = [{**job.requirements[0], "quantity": 3}]
    with pytest.raises(ValueError, match="版本"):
        execute_tool(db, job, run, "repair_pricing_plan", RepairProposal(line_id="one", version=1,
            edits=[{"path": "/parameters/0/values/height_mm", "op": "add", "value": 500}]), set())


def test_source_ids_cannot_reference_another_project_or_invent_numbers(order):
    plan = proposal(order)
    plan["parameters"][0]["source_ids"] = ["s_forged"]
    assert "来源编号" in submit(order, plan)["error"]
    plan = proposal(order)
    plan["parameters"][0]["values"]["height_mm"] = 999
    assert "数值不在" in submit(order, plan)["error"]


def test_provisional_values_are_explicit_and_never_override_source_values(order):
    _, job, _ = order
    plan = proposal(order)
    plan["parameters"][0].update(basis="assumption", source_ids=[], reason="样本暂估尺寸，待审核")
    assert submit(order, plan)["saved"]
    assert "暂定假设" in job.requirements[0]["estimate"]["assumptions"][0]["reason"]


def test_catalog_rate_unit_revision_are_read_by_server(order):
    db, job, _ = order
    category = Category(name="Material")
    db.add(category)
    db.flush()
    product = Product(category_id=category.id, name="铝板")
    db.add(product)
    db.flush()
    price = PriceItem(product_id=product.id, unit="m2", amount=Decimal("100"),
                      status="active", price_kind="suggested")
    db.add(price)
    db.flush()
    plan = proposal(order)
    cost = plan["costs"][0]
    cost.pop("rate")
    cost.pop("unit")
    cost["catalog_id"] = price.id
    assert submit(order, plan)["saved"]
    compiled = job.requirements[0]["estimate"]["costs"][0]
    assert compiled["rate"] == "100" and compiled["reference_revision"] == price.revision
    assert compiled["source"] == "catalog" and compiled["rate_unit"] == "m2"


def test_component_coverage_is_required_and_target_indices_generated(order):
    order[1].requirements = [{**order[1].requirements[0], "difficulty": "C"}]
    plan = proposal(order)
    source = plan["parameters"][0]["source_ids"][0]
    plan["strategy"] = "composite"
    plan["objects"] = [{"id": "panel", "kind": "component", "name": "铝板", "source_ids": [source]},
                       {"id": "frame", "kind": "component", "name": "支架", "source_ids": [source]}]
    plan["parameters"] = [
        {"target": "panel", "values": {"width_mm": 1000, "height_mm": 500, "quantity_per_item": 1},
         "source_ids": [source], "basis": "assumption", "reason": "每套一块板，尺寸见原文，数量暂定"},
        {"target": "frame", "values": {"width_mm": 500, "height_mm": 200, "quantity_per_item": 3},
         "source_ids": [source], "basis": "source", "reason": "原文支架每套3个"}]
    plan["costs"] = [{**plan["costs"][0], "target": "panel", "measurement": "component_area"}]
    result = submit(order, plan)
    assert not result["saved"] and any("支架" in b for b in result["blockers"]), result.get("blockers")
    db, job, run = order
    result = execute_tool(db, job, run, "repair_pricing_plan",
        RepairProposal(line_id="one", version=1, edits=[{"op": "add", "path": "/costs/-", "value": {
            "label": "方通材料", "kind": "material", "target": "frame", "measurement": "component_area",
            "rate": 200, "unit": "m2", "reason": "验收样本给定材料费率，待审核"}}]), set())
    assert result["saved"] and result["amount"] == "220.00", result.get("blockers")
    assert job.requirements[0]["estimate"]["costs"][1]["target_index"] == 1


def test_script_uses_live_dimensions_and_conversation_recalculates(order):
    db, job, _ = order
    job.requirements = [{**job.requirements[0], "difficulty": "C"}]
    plan = proposal(order)
    plan["costs"] = [{**plan["costs"][0], "measurement": "script",
                      "script": "result = width_mm * height_mm / 1000000\nassert result > 0"}]
    result = submit(order, plan)
    assert result["amount"] == "100.00", result.get("blockers")
    original = deepcopy(job.requirements)
    changed = changed_requirements(job.requirements, [RequirementChange(
        line_id="one", field="width_mm", value="2000", reason="客户修改宽度")])
    assert build_quote(db, changed, automatic=True)["total"] == "200.00"
    assert job.requirements == original
    changed = changed_requirements(changed, [RequirementChange(
        line_id="one", field="quantity", value="3", reason="客户修改数量")])
    assert build_quote(db, changed, automatic=True)["total"] == "300.00"


def test_packet_and_plan_survive_context_rebuild_without_repeat_reads(order):
    db, job, run = order
    plan = proposal(order)
    del plan["parameters"][0]["values"]["height_mm"]
    submit(order, plan)
    context = json.loads(context_messages(job, run, db)[1]["content"])
    assert context["pricing_packet"]["sources"]
    assert context["pricing_proposal"]["version"] == 1
    assert context["pricing_proposal"]["plan"]["costs"] == work.line_state(
        job, run, "one")["pricing_proposal"]["plan"]["costs"]


def catalog_parts(order):
    db, _, _ = order
    category = Category(name="Imported material")
    db.add(category)
    db.flush()
    product = Product(category_id=category.id, name="材料+表面处理建议价")
    db.add(product)
    db.flush()
    price = PriceItem(product_id=product.id, unit="m2", amount=Decimal("200"),
                      status="active", price_kind="suggested", source_cell="J12",
                      attributes={"material_price": "100", "process_1_price": "20",
                                  "process_2_price": "10"})
    db.add(price)
    db.flush()
    plan = proposal(order)
    for cost, part in zip(plan["costs"], ["material_price", "process_1_price"]):
        cost.pop("rate")
        cost.pop("unit")
        cost.update(catalog_id=price.id, catalog_part=part)
    return price, plan


def test_catalog_parts_apply_difficulty_once_and_detect_changed_rate(order):
    db, job, _ = order
    price, plan = catalog_parts(order)
    result = submit(order, plan)
    assert result["saved"] and result["amount"] == "140.00", result.get("blockers")
    assert job.requirements[0]["estimate"]["costs"][0]["reference_part"] == "material_price"
    price.attributes = {**price.attributes, "material_price": "110"}
    db.flush()
    quote = build_quote(db, job.requirements, automatic=True)
    assert quote["lines"][0]["amount"] is None


@pytest.mark.parametrize("raw,expected", [
    ("99.99999962", "100.000000"), ("99.9999962", "99.999996"),
    ("29.9999952", "29.999995"), ("12.1234565", "12.123457"),
    ("12.1234564", "12.123456"), ("100", "100"), ("0", "0"),
])
def test_imported_part_precision_uses_same_rate_for_compile_and_validation(order, raw, expected):
    db, job, _ = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": raw}
    original = deepcopy(price.attributes)
    result = submit(order, plan)
    assert result["saved"], result.get("blockers")
    assert job.requirements[0]["estimate"]["costs"][0]["rate"] == expected
    assert build_quote(db, job.requirements, automatic=True)["total"] == result["amount"]
    reference = result["estimate_costs"][0]["reference"]
    assert reference["source_rate"] == raw and reference["source_cell"] == "H12"
    assert reference["rate_precision"] == 6 and reference["rate_rounding"] == "ROUND_HALF_UP"
    assert price.attributes == original
    price.attributes = {**price.attributes, "material_price": "101"}
    assert build_quote(db, job.requirements, automatic=True)["lines"][0]["amount"] is None


def test_packet_normalizes_part_rates_but_preserves_imported_values(order):
    db, job, _ = order
    price, _ = catalog_parts(order)
    price.material = "铝"
    price.attributes = {**price.attributes, "material_price": "99.99999962", "process_2_price": None}
    entry = next(p for p in prepare_packet(db, job, job.requirements[0])["catalog_candidates"]
                 if p["id"] == price.id)
    assert entry["cost_columns"]["material_price"] == "100.000000"
    assert entry["source_cost_columns"]["material_price"] == "99.99999962"
    assert entry["cost_columns"]["process_2_price"] is None


def test_original_row_process_alias_retrieves_catalog_cost_and_reaches_quote(order):
    db, job, _ = order
    price, plan = catalog_parts(order)
    price.material = "铝"
    price.attributes = {**price.attributes, "process_1": "丝印"}
    # The fabrication instruction exists only in the original row, not extracted fields.
    file = db.scalar(select(CustomerFile).where(CustomerFile.job_id == job.id))
    wb = Workbook()
    wb.active.append(["序号", "产品名称", "数量", "单位", "说明"])
    wb.active.append([1, "安全提示", 2, "套", "铝板1000*500mm；丝网印刷"])
    output = io.BytesIO()
    wb.save(output)
    wb.close()
    for key, value in prepare_file(output.getvalue(), "process.xlsx").items():
        setattr(file, key, value)
    raw = {**job.requirements[0], "product": "安全提示", "material": "", "process": "", "notes": ""}
    job.requirements = [raw]
    packet = prepare_packet(db, job, raw)
    assert packet["catalog_candidates"][0]["id"] == price.id
    assert packet["catalog_candidates"][0]["matched_targets"] == ["item"]
    source = next(s["id"] for s in packet["sources"] if s["cell"] == "E2")
    plan["parameters"][0]["source_ids"] = [source]
    plan["costs"][1]["label"] = "丝印加工"
    result = submit(order, plan)
    assert result["saved"] and result["amount"] == "140.00", result.get("blockers")
    assert build_quote(db, job.requirements, automatic=True)["known_subtotal"] == "140.00"


def test_process_field_alone_retrieves_matching_catalog_column(order):
    from app.pricing_proposals import catalog_packet
    db, _, _ = order
    price, _ = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "丝印"}
    assert catalog_packet(db, {"product": "Warning", "process": "丝网印刷"})[0]["id"] == price.id


def test_source_text_cannot_override_known_incompatible_material(order):
    from app.pricing_proposals import catalog_packet
    from app.requirements_schemas import Evidence
    db, _, _ = order
    price, _ = catalog_parts(order)
    price.material = "铝"
    price.attributes = {**price.attributes, "process_1": "丝印"}
    assert catalog_packet(db, {"product": "Warning", "material": "亚克力"},
                          {"source": Evidence(file_id=1, page=1, text="丝网印刷")}) == []


def test_aggregate_material_process_price_cannot_include_unrequested_process(order):
    order[1].requirements = [{**order[1].requirements[0], "difficulty": "C"}]
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "烤漆"}
    plan["costs"] = [{**plan["costs"][0], "kind": "finished", "label": "综合成品"}]
    plan["costs"][0].pop("catalog_part")
    result = submit(order, plan)
    assert not result["saved"] and "对应工艺依据" in str(result["blockers"])


@pytest.mark.parametrize("process", ["围边焊接烤半哑光漆-黑", "烤半哑光金属漆"])
def test_paint_finish_modifier_matches_only_the_paint_catalog_column(order, process):
    from app.catalog_parts import validate_part_process
    from app.catalog_matching import process_score
    price, _ = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "烤漆"}
    line = RequirementLine(id="paint", product="Plate", process=process)
    validate_part_process(price, "process_1_price", line)
    assert process_score(process, "烤漆") > 0
    price.attributes = {**price.attributes, "process_1": "焊接"}
    with pytest.raises(ValueError, match="对应工艺依据"):
        validate_part_process(price, "process_1_price",
                              RequirementLine(id="paint", product="Plate", process="烤半哑光漆"))


@pytest.mark.parametrize("process", ["围边焊接", "细乱纹半哑光防指纹"])
def test_welding_or_surface_sheen_does_not_imply_painting(order, process):
    from app.catalog_parts import validate_part_process
    price, _ = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "烤漆"}
    with pytest.raises(ValueError, match="对应工艺依据"):
        validate_part_process(price, "process_1_price",
                              RequirementLine(id="other", product="Plate", process=process))


def test_process_only_catalog_column_does_not_introduce_unprovided_material(order):
    db, job, _ = order
    price, plan = catalog_parts(order)
    price.material = "亚克力"
    job.requirements = [{**job.requirements[0], "product": "Direct printing", "material": "",
                         "notes": "", "process": "", "difficulty": "C",
                         "evidence": [], "quantity_evidence": [], "dimension_evidence": []}]
    plan["parameters"][0].update(basis="assumption", source_ids=[], reason="Test dimensions for direct printing")
    plan["costs"] = [plan["costs"][0]]
    result = submit(order, plan)
    assert not result["saved"] and "没有明确该材料" in str(result["blockers"])
    plan["costs"][0].update(kind="process", catalog_part="process_1_price", label="加工")
    result = submit(order, plan)
    assert result["saved"] and result["amount"] == "20.00", result.get("blockers")


@pytest.mark.parametrize("value", ["-0.00000001", "Infinity", "1E50", "999999999.9999995", "not a number"])
def test_precision_normalization_does_not_accept_invalid_costs(order, value):
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": value}
    result = submit(order, plan)
    assert not result["saved"] and result.get("error")
    assert order[1].requirements[0]["estimate"] is None


def legacy_precision_failure(order, monkeypatch, plan):
    import app.catalog_parts as parts
    with monkeypatch.context() as patch:
        patch.setattr(parts, "part_rate", lambda price, part: Decimal(str(price.attributes[part])))
        result = submit(order, plan)
    assert not result["saved"] and "decimal_max_places" in result["technical_error"]
    work.update_state(order[1], order[2], "one", deferred=True,
                      repair_failures={"legacy": 10}, repair_attempts=10)


def test_saved_precision_failure_is_replayed_without_model_and_audited(order, monkeypatch):
    from app.pricing_proposals import recover_catalog_precision
    db, job, run = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": "99.99999962"}
    legacy_precision_failure(order, monkeypatch, plan)
    original = deepcopy(work.line_state(job, run, "one")["pricing_proposal"]["plan"])
    assert recover_catalog_precision(db, job, run, set())
    assert build_quote(db, job.requirements, automatic=True)["total"] == "140.00"
    assert not work.line_state(job, run, "one")["deferred"]
    assert run.steps[-1]["result"]["model_called"] is False
    assert run.steps[-1]["result"]["recovery"] == "catalog_rate_precision"
    assert work.line_state(job, run, "one")["pricing_proposal"]["plan"] == original
    assert not recover_catalog_precision(db, job, run, set())


def test_precision_recovery_retains_new_business_blockers_and_only_attempts_once(order, monkeypatch):
    from app.pricing_proposals import recover_catalog_precision
    db, job, run = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": "99.99999962", "process_1": "烤漆"}
    legacy_precision_failure(order, monkeypatch, plan)
    assert recover_catalog_precision(db, job, run, set())
    state = work.line_state(job, run, "one")
    assert not state["deferred"] and not state["repair_failures"]
    assert "对应工艺依据" in str(state["diagnostics"])
    assert job.requirements[0]["estimate"] is None
    assert not recover_catalog_precision(db, job, run, set())


def test_precision_recovery_cannot_reuse_a_plan_after_original_inputs_change(order, monkeypatch):
    from app.pricing_proposals import recover_catalog_precision
    db, job, run = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": "99.99999962"}
    legacy_precision_failure(order, monkeypatch, plan)
    job.requirements = [{**job.requirements[0], "quantity": 3}]
    assert not recover_catalog_precision(db, job, run, set())
    assert job.requirements[0]["estimate"] is None


def test_precision_recovery_honors_preview_only_and_interruption(order, monkeypatch):
    from app.pricing_proposals import recover_catalog_precision
    from app.agent_execution import Stopped
    db, job, run = order
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": "99.99999962"}
    legacy_precision_failure(order, monkeypatch, plan)
    with pytest.raises(Stopped):
        recover_catalog_precision(db, job, run, set(), lambda: True)
    run.usage = {**run.usage, "proposal_preview_only": True}
    assert recover_catalog_precision(db, job, run, set())
    assert job.requirements[0]["estimate"] is None
    assert work.line_state(job, run, "one")["validated_plan"]
    assert run.steps[-1]["result"]["preview"]


def test_invalid_plan_returns_field_paths_without_raw_validation_dump(order):
    db, job, run = order
    plan = proposal(order)
    plan["costs"][0]["rate"] = "100.1234567"
    work.update_state(job, run, "one", pricing_proposal={
        "version": 1, "input_fingerprint": work.fingerprint(job.requirements[0]), "plan": plan})
    result = execute_tool(db, job, run, "repair_pricing_plan",
                          RepairProposal(line_id="one", version=1, edits=[
                              {"path": "/scope", "value": plan["scope"]}]), set())
    assert not result["saved"] and "/costs/0/rate" in result["error"]
    assert "小数最多保留 6 位" in result["error"]
    assert "validation error" not in result["error"] and "pydantic.dev" not in result["error"]
    assert result["diagnostics"][0]["paths"] == ["/costs/0/rate"]
    assert "decimal_max_places" in result["technical_error"]
    assert "technical_error" not in work.context_state(job, run, "one")["diagnostics"]


def test_catalog_parts_reject_duplicates_and_aggregate_overlap(order):
    _, plan = catalog_parts(order)
    plan["costs"].append(deepcopy(plan["costs"][0]))
    result = submit(order, plan)
    assert not result["saved"] and any("重复" in b for b in result["blockers"])
    plan["costs"].pop()
    plan["costs"][0].pop("catalog_part")
    result = submit(order, plan)
    assert not result["saved"]


@pytest.mark.parametrize("value", [None, "", "NaN", "-1"])
def test_catalog_parts_missing_or_invalid_is_not_free(order, value):
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "material_price": value}
    result = submit(order, plan)
    assert not result["saved"] and result.get("error")


def test_invalid_typed_plan_can_be_repaired_without_recreating_costs(order):
    from app.pricing_proposals import remember_invalid_plan
    db, job, run = order
    plan = proposal(order)
    plan["parameters"][0]["values"]["pricing_family"] = "area"
    version = remember_invalid_plan(job, run, {"line_id": "one", "plan": plan})
    result = execute_tool(db, job, run, "repair_pricing_plan",
        RepairProposal(line_id="one", version=version, edits=[{
            "path": "/parameters/0/values/pricing_family", "value": "material"}]), set())
    assert result["saved"] and result["amount"] == "140.00", result.get("blockers")


def test_repair_can_remove_forbidden_root_fields_without_touching_facts_or_costs(order):
    from app.pricing_proposals import remember_invalid_plan
    db, job, run = order
    plan = proposal(order)
    plan.update(quantity=99, width_mm=9999)
    original_facts = deepcopy(job.requirements)
    version = remember_invalid_plan(job, run, {"line_id": "one", "plan": plan})
    result = execute_tool(db, job, run, "repair_pricing_plan",
        RepairProposal(line_id="one", version=version, edits=[
            {"path": "/quantity", "op": "remove"}, {"path": "/width_mm", "op": "remove"}]), set())
    assert result["saved"] and result["amount"] == "140.00", result.get("blockers")
    repaired = work.line_state(job, run, "one")["pricing_proposal"]["plan"]
    assert repaired["costs"] == plan["costs"]
    assert "quantity" not in repaired and "width_mm" not in repaired
    assert job.requirements[0]["quantity"] == original_facts[0]["quantity"]
    assert job.requirements[0]["width_mm"] == original_facts[0]["width_mm"]
    assert plan["quantity"] == 99 and plan["width_mm"] == 9999


@pytest.mark.parametrize("edit", [
    {"path": "/quantity", "op": "replace", "value": 10},
    {"path": "/quantity", "op": "add", "value": 10},
    {"path": "/missing", "op": "remove"},
    {"path": "/quantity/nested", "op": "remove"},
])
def test_forbidden_root_fields_cannot_be_added_or_changed_during_repair(order, edit):
    from app.pricing_proposals import PlanEdit, patched_plan
    plan = {**proposal(order), "quantity": {"nested": 99}}
    before = deepcopy(plan)
    with pytest.raises(ValueError, match="多余顶层字段"):
        patched_plan(plan, [PlanEdit.model_validate(edit)])
    assert plan == before


def test_conversation_updates_proposed_component_and_recalculates(order):
    db, job, _ = order
    job.requirements = [{**job.requirements[0], "difficulty": "C"}]
    plan = proposal(order)
    source = plan["parameters"][0]["source_ids"][0]
    plan["strategy"] = "composite"
    plan["objects"] = [{"id": "panel", "kind": "component", "name": "铝板", "source_ids": [source]}]
    plan["parameters"][0].update(target="panel", basis="assumption", reason="每套一块板，待核实")
    plan["parameters"][0]["values"]["quantity_per_item"] = 1
    plan["costs"] = [{**plan["costs"][0], "target": "panel", "measurement": "component_area"}]
    assert submit(order, plan)["amount"] == "100.00"
    changed = changed_requirements(job.requirements, [RequirementChange(
        line_id="one", target="component", target_id="panel", field="width_mm",
        value="2000", reason="用户修改板宽")])
    assert build_quote(db, changed, automatic=True)["total"] == "200.00"
    changed = changed_requirements(changed, [RequirementChange(
        line_id="one", target="component", target_id="panel", field="material",
        value="不锈钢", reason="用户修改板材")])
    assert changed[0]["estimate"] is None
    assert changed[0]["components"][0]["material"] == "不锈钢"


def test_component_invalid_fields_report_all_paths_together(order):
    plan = proposal(order)
    source = plan["parameters"][0]["source_ids"][0]
    plan["objects"] = [{"id": "panel", "kind": "component", "name": "铝板", "source_ids": [source]}]
    plan["parameters"][0].update(target="panel")
    plan["parameters"][0]["values"].update(quantity=2, process="烤漆")
    result = submit(order, plan)
    assert "/parameters/0/values/quantity" in result["error"]
    assert "/parameters/0/values/process" in result["error"]


def test_catalog_process_cannot_be_mislabeled_as_matching(order):
    price, plan = catalog_parts(order)
    price.attributes = {**price.attributes, "process_1": "烤漆"}
    result = submit(order, plan)
    assert not result["saved"] and any("对应工艺依据" in b for b in result["blockers"])


def test_material_grade_must_not_be_invented_as_source_fact(order):
    plan = proposal(order)
    plan["parameters"][0]["values"]["material"] = "6061铝"
    assert "牌号不在原文" in submit(order, plan)["error"]


def test_material_subtotal_cannot_be_saved_as_fabricated_finished_quote(order):
    _, job, _ = order
    job.requirements = [{**job.requirements[0], "notes": "铝板切割、烤漆、丝网印刷"}]
    plan = proposal(order)
    plan["costs"] = plan["costs"][:1]
    result = submit(order, plan)
    assert not result["saved"] and "方案仅覆盖材料" in result["error"]
    assert job.requirements[0]["estimate"] is None


def test_layered_sign_cannot_silently_count_only_one_board(order):
    _, job, _ = order
    job.requirements = [{**job.requirements[0], "notes": "面层5mm亚克力，背板10mm硬质PVC，切割烤漆"}]
    result = submit(order, proposal(order))
    assert not result["saved"] and "面层及底板" in result["error"]


def test_explicit_accessories_and_printing_need_cost_coverage(order):
    _, job, _ = order
    job.requirements = [{**job.requirements[0], "notes": "丝网印刷，盲文珠，背藏磁铁"}]
    result = submit(order, proposal(order))
    assert not result["saved"] and all(term in result["error"] for term in ("丝网印刷", "盲文", "磁铁"))


def test_wrong_coverage_fields_return_compact_paths_together(order):
    plan = proposal(order)
    for cost in plan["costs"]:
        cost["covers"] = ["panel"]
    result = submit(order, plan)
    assert not result["saved"]
    assert "/costs/0/covers" in result["error"] and "/costs/1/covers" in result["error"]
