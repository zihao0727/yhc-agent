import io
import json
import zipfile
from decimal import Decimal

import pytest
from openpyxl import Workbook
from PIL import Image

from app.customer_files import prepare_file, storage_path
from app.customer_excel import page_source, validate_excel_rows
from app.estimate_pricing import calculate_estimate
from app.requirements_schemas import EstimatePlan, ExtractionResult, RequirementLine
from test_agent_routing import redis_worker  # noqa: F401


def line_v3(**updates):
    data = {"id": "test", "product": "Panel", "quantity": 2, "width_mm": 1000, "height_mm": 500,
            "pricing_family": "material", "business_rules_version": "2026-09-23.v1",
            "difficulty": "B", "estimate": {"contract_version": 3, "pricing_strategy": "area",
                "scope": "Manufacturing only", "costs": [
                    {"label": "Material", "kind": "material", "measurement": "item_area",
                     "rate": 100, "rate_unit": "m2", "reason": "Explicit provisional rate"},
                    {"label": "Processing", "kind": "process", "measurement": "item_area",
                     "rate": 20, "rate_unit": "m2", "reason": "Base processing rate"}]}}
    data.update(updates)
    return RequirementLine.model_validate(data)


def calculate(line):
    return calculate_estimate(None, line, lambda *args, **kwargs: {})


def test_program_compiles_area_and_applies_difficulty_and_order_count_once():
    line = line_v3()
    before = line.model_dump()
    result = calculate(line)
    assert not result["blockers"]
    assert result["unit_price"] == "70.00" and result["amount"] == "140.00"
    assert result["estimate_costs"][1]["difficulty_multiplier"] == "2"
    assert result["compiled_plan"]["costs"][0]["quantity_formula"] == "=area_m2"
    assert line.model_dump() == before
    assert calculate(line)["amount"] == result["amount"]


def test_overall_area_is_never_multiplied_by_letter_count_in_v3():
    line = line_v3(pricing_family="letter", character_count=6)
    assert calculate(line)["amount"] == "140.00"
    line.estimate.costs[0].measurement = "letter_area"
    result = calculate(line)
    assert result["amount"] is None
    assert result["diagnostics"][0]["code"] == "ambiguous_letter_dimensions"


def test_stable_component_ids_survive_reordering_and_reject_stale_index():
    line = line_v3(components=[
        {"name": "Front", "width_mm": 100, "height_mm": 200, "quantity_per_item": 2},
        {"name": "Back", "width_mm": 300, "height_mm": 400, "quantity_per_item": 1}])
    target = line.components[0].object_id
    line.estimate.costs[0].target = "component"
    line.estimate.costs[0].target_id = target
    line.estimate.costs[0].measurement = "component_area"
    a = calculate(line)
    line.components.reverse()
    b = calculate(line)
    assert a["amount"] == b["amount"] == "48.00"
    line.estimate.costs[0].target_index = 0
    assert calculate(line)["diagnostics"][0]["code"] == "stale_target"


def test_missing_power_count_is_not_guessed_and_known_count_is_auto_added():
    line = line_v3(lighting="yes")
    result = calculate(line)
    assert result["diagnostics"][0]["code"] == "missing_power_count"
    line.power_count = 1
    result = calculate(line)
    assert result["amount"] == "540.00"
    assert len([c for c in result["estimate_costs"] if c["kind"] == "power"]) == 1
    assert len(line.estimate.costs) == 2


def test_auto_bracket_uses_declared_whole_item_measurement():
    result = calculate(line_v3(installation_scene="rooftop"))
    fee = next(c for c in result["compiled_plan"]["costs"] if c["kind"] == "bracket")
    assert fee["measurement"] == "item_area"
    assert fee["quantity_formula"] == "=area_m2"


@pytest.mark.parametrize("formula", ["=area_m2*2", "=area_m2*quantity", "=area_m2*6"])
def test_custom_multipliers_are_blocked(formula):
    line = line_v3()
    line.estimate.costs[0].quantity_formula = formula
    assert calculate(line)["diagnostics"][0]["code"] == "custom_formula_forbidden"


def test_external_check_amount_is_still_validated():
    line = line_v3()
    line.estimate.costs[1].expected_amount = Decimal("10")
    result = calculate(line)
    assert result["amount"] is None
    assert result["diagnostics"][0]["code"] == "amount_mismatch"


def embedded_workbook(kind="wps", missing=False):
    book, output = Workbook(), io.BytesIO()
    sheet = book.active
    sheet.append(["序号", "项目名称", "数量", "单位", "图纸"])
    sheet.append([1, "门牌", 2, "个", '=DISPIMG("IMG_ONE",1)' if kind == "wps" else "#VALUE!"])
    book.save(output)
    image = io.BytesIO()
    Image.new("RGB", (30, 20), "red").save(image, "PNG")
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as original, zipfile.ZipFile(result, "w") as zipped:
        for name in original.namelist():
            content = original.read(name)
            if kind == "rich" and name == "xl/worksheets/sheet1.xml":
                content = content.replace(b'<c r="E2"', b'<c r="E2" vm="1"')
            zipped.writestr(name, content)
        zipped.writestr("xl/media/embedded.png", image.getvalue())
        rel = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="image" Target="../media/embedded.png"/></Relationships>')
        if kind == "wps":
            zipped.writestr("xl/cellimages.xml", '<cellImages xmlns:d="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                f'<cellImage><d:pic><d:cNvPr name="{"OTHER" if missing else "IMG_ONE"}"/>'
                '<a:blip r:embed="rId1"/></d:pic></cellImage></cellImages>')
            zipped.writestr("xl/_rels/cellimages.xml.rels", rel.replace("../media/", "media/"))
        else:
            zipped.writestr("xl/richData/_rels/richValueRel.xml.rels", rel)
            zipped.writestr("xl/richData/richValueRel.xml",
                '<richValueRels xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><rel r:id="rId1"/></richValueRels>')
            zipped.writestr("xl/richData/rdrichvalue.xml", '<rvData><rv s="0"><v>0</v></rv></rvData>')
            zipped.writestr("xl/richData/rdrichvaluestructure.xml",
                '<rvStructures><s t="_localImage"><k n="_rvRel:LocalImageIdentifier"/></s></rvStructures>')
            zipped.writestr("xl/metadata.xml",
                '<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="urn:rich">'
                '<metadataTypes><metadataType name="XLRICHVALUE"/></metadataTypes>'
                '<futureMetadata name="XLRICHVALUE"><bk><extLst><ext><r:rvb i="0"/></ext></extLst></bk></futureMetadata>'
                '<valueMetadata><bk><rc t="1" v="0"/></bk></valueMetadata></metadata>')
    return result.getvalue()


@pytest.mark.parametrize("kind", ["wps", "rich"])
def test_offline_cell_image_adapters_have_complete_resource_accounting(kind):
    item = prepare_file(embedded_workbook(kind), "generic.xlsx")
    directory = storage_path(item["storage_key"])
    manifest = json.loads((directory / "excel.json").read_text(encoding="utf-8"))
    assert manifest["resource_summary"] == {"discovered": 1, "resolved": 1, "unresolved": 0}
    assert manifest["pages"][0]["images"][0]["cell"] == "E2"
    with Image.open(directory / manifest["pages"][0]["images"][0]["storage"]) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)


def test_missing_image_is_visible_not_silently_dropped():
    item = prepare_file(embedded_workbook(missing=True), "generic.xlsx")
    source = page_source(storage_path(item["storage_key"]), 1)
    assert source["resource_audit"][0]["status"] == "unresolved"
    result = validate_excel_rows(ExtractionResult(lines=[]), {(1, 1): source})
    assert len(result.lines) == 1
    assert any("图纸资源未读取" in w for w in result.lines[0].uncertainties)
    assert {c.status for c in result.source_coverage} == {"header", "extracted"}


def test_brand_instruction_is_accounted_for_but_not_quoted_as_product():
    source = {"sheet": "Sheet1", "images": [], "rows": [
        {"row": 1, "cells": [{"cell": "A1", "value": "灯及LED灯品牌要求：指定品牌"}]}]}
    result = ExtractionResult.model_validate({"lines": [{"product": "灯及LED灯品牌要求：指定品牌",
        "evidence": [{"file_id": 1, "page": 1, "sheet": "Sheet1", "cell": "A1", "text": "品牌要求"}]}]})
    result = validate_excel_rows(result, {(1, 1): source})
    assert not result.lines
    assert result.source_coverage[0].status == "non_product"
    assert result.questions


def test_compiler_can_propose_missing_components_without_replacing_original_facts():
    line = line_v3()
    plan = line.estimate.model_dump(mode="json")
    plan["pricing_strategy"] = "composite"
    plan["proposed_components"] = [{"object_id": "panel", "name": "Panel",
        "width_mm": 1000, "height_mm": 500, "quantity_per_item": 2,
        "evidence": [{"file_id": 1, "page": 1, "text": "two panels"}]}]
    plan["costs"][0].update(target="component", target_id="panel", measurement="component_area")
    line.estimate = EstimatePlan.model_validate(plan)
    result = calculate(line)
    assert result["amount"] == "240.00"
    assert not line.components
    assert result["estimate_review"][0]["kind"] == "proposed_object"
    line.confirmed = True
    assert calculate(line)["diagnostics"][0]["code"] == "object_replacement_forbidden"


def test_per_order_and_per_piece_outsourcing_minimums_are_not_applied_twice():
    from app.requirements_schemas import EstimateCost
    line = line_v3(width_mm=100, height_mm=100, quantity=20, difficulty="C")
    fee = EstimateCost(label="Adhesive", kind="outsourcing", outsourcing_type="adhesive", rate=50,
        rate_unit="m2", measurement="item_area", minimum_area_scope="order", basis="total", reason="Order minimum")
    line.estimate.costs = [fee]
    assert calculate(line)["amount"] == "10.00"
    fee.minimum_area_scope, fee.basis = "per_piece", "per_piece"
    assert calculate(line)["amount"] == "100.00"


def test_profit_uses_computed_subtotal_and_generated_formula():
    from app.requirements_schemas import EstimateCost
    line = line_v3()
    line.estimate.costs.append(EstimateCost(label="Profit", kind="profit", measurement="subtotal_ratio",
        rate="0.1", rate_unit="ratio", reason="Explicit provisional margin"))
    assert calculate(line)["amount"] == "154.00"


def test_explicit_difficulty_surcharge_is_rejected():
    line = line_v3()
    line.estimate.costs[1].label = "B级难度加成"
    assert calculate(line)["diagnostics"][0]["code"] == "duplicate_difficulty"


@pytest.mark.parametrize("unit,rate", [("m", 200), ("cm", 2)])
def test_length_unit_conversion_has_identical_amount(unit, rate):
    line = line_v3(billing_length_mm=750, difficulty="C")
    line.estimate.pricing_strategy = "length"
    line.estimate.costs = [line.estimate.costs[0]]
    line.estimate.costs[0].measurement = "item_length"
    line.estimate.costs[0].rate_unit, line.estimate.costs[0].rate = unit, Decimal(rate)
    assert calculate(line)["amount"] == "300.00"


def test_missing_component_quantity_has_object_level_repair_path():
    line = line_v3(components=[{"name": "Panel", "width_mm": 100, "height_mm": 100}])
    line.estimate.costs[0].target = "component"
    line.estimate.costs[0].target_id = line.components[0].object_id
    line.estimate.costs[0].measurement = "component_area"
    result = calculate(line)
    assert result["diagnostics"][0]["paths"] == ["components[0].quantity_per_item"]


def test_legacy_quotation_display_enrichment_does_not_change_saved_data():
    from app.quote_presentation import enrich_legacy_issues
    from types import SimpleNamespace
    line = line_v3().model_dump(mode="json")
    payload = {"lines": [{"line_id": "test", "product": "Panel", "amount": None, "blockers": ["价格未选"],
        "requirement": line, "deferred_issue": {"cause": "execution_error",
            "reason": "该项连续多轮未形成可计价方案，已停止无进展调用", "product": "Panel"}}]}
    before = json.loads(json.dumps(payload))
    run = SimpleNamespace(steps=[], usage={"work_state": {"test": {"diagnostics": {
        "blockers": ["发光产品须单列电源费用"]}}}})
    result = enrich_legacy_issues(payload, run)
    assert payload == before
    assert result["lines"][0]["resolution"] == "estimate_repair"
    assert "电源" in result["lines"][0]["deferred_issue"]["reason"]


def test_line_repair_budget_preserves_exact_reason_and_isolated_progress(monkeypatch):
    from types import SimpleNamespace
    from app import agent_work_state as work
    from app.config import settings
    monkeypatch.setattr(settings(), "agent_repair_attempts", 2)
    raw = line_v3().model_dump(mode="json")
    job, run = SimpleNamespace(requirements=[raw]), SimpleNamespace(usage={})
    result = {"saved": False, "blockers": ["Missing component count"],
              "diagnostics": [{"code": "missing_target_measurement", "paths": ["components[0].quantity_per_item"],
                               "action": "inspect_source", "cause": "recognition_review"}]}
    for _ in range(2):
        work.remember(job, run, "complete_estimate", {"line_id": "test", "estimate": {}}, result)
    assert work.exhausted_reason(job, run, "test") == "repeated_failure"
    assert work.pause_details(job, run, "test")["cause"] == "recognition_review"
    assert work.pause_details(job, run, "test")["reason"] == "Missing component count"


def test_preview_tool_and_shadow_audit_are_read_only(test_db):
    from types import SimpleNamespace
    from app.agent_tools import CompleteEstimateInput
    from app.agent_execution import execute_tool
    from app.pipeline_audit import shadow_line
    raw = line_v3().model_dump(mode="json")
    job = SimpleNamespace(id=100, requirements=[raw], revision=1)
    run = SimpleNamespace(usage={})
    before = json.loads(json.dumps(raw))
    with test_db() as db:
        result = execute_tool(db, job, run, "preview_estimate",
            CompleteEstimateInput(line_id="test", estimate=raw["estimate"]), set())
        assert result["preview"] and not result["saved"] and result["amount"] == "140.00"
        report = shadow_line(db, raw)
        assert report["status"] == "unchanged"
    assert job.requirements == [before] and job.revision == 1


def test_preview_preserves_typed_source_validation_failure(test_db):
    from types import SimpleNamespace
    from app.agent_tools import CompleteEstimateInput
    from app.agent_execution import execute_tool
    from app.requirements_schemas import EstimateAssumption
    line = line_v3(components=[{"name": "Panel"}])
    line.estimate.assumptions = [EstimateAssumption(
        target="component", target_id="missing-object", field="width_mm",
        value="100", reason="Source review required")]
    raw = line.model_dump(mode="json")
    with test_db() as db:
        result = execute_tool(db, SimpleNamespace(id=100, requirements=[raw]),
                              SimpleNamespace(usage={}), "preview_estimate",
                              CompleteEstimateInput(line_id="test", estimate=raw["estimate"]), set())
    assert not result["saved"]
    assert result["diagnostics"][0]["code"] == "stale_target"
    assert result["diagnostics"][0]["cause"] == "recognition_review"
    assert result["next_action"] == "read_target"


def test_audit_endpoints_preserve_job_and_original_files(client, test_db):
    from app.models import CustomerFile
    response = client.post("/api/jobs", data={"title": "Read-only audit"},
                           files={"files": ("images.xlsx", embedded_workbook())})
    assert response.status_code == 201
    job = response.json()
    file_id = job["files"][0]["id"]
    with test_db() as db:
        directory = storage_path(db.get(CustomerFile, file_id).storage_key)
    before_files = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    before_job = client.get(f'/api/jobs/{job["id"]}').json()
    audit = client.get(f'/api/jobs/{job["id"]}/pipeline-audit')
    assert audit.status_code == 200 and audit.json()["read_only"]
    preview = client.post(f"/api/customer-files/{file_id}/reparse-preview")
    assert preview.status_code == 200 and preview.json()["read_only"]
    assert preview.json()["resource_summary"]["resolved"] == 1
    assert client.get(f'/api/jobs/{job["id"]}').json() == before_job
    assert before_files == {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_source_inspection_is_scoped_read_only_and_accounts_usage(client, test_db, monkeypatch):
    from types import SimpleNamespace
    from app import deepseek
    from app.agent_tools import ComponentInput
    from app.agent_execution import execute_tool
    uploaded = client.post("/api/jobs", data={"title": "Inspection"},
                          files={"files": ("images.xlsx", embedded_workbook())}).json()
    file_id = uploaded["files"][0]["id"]
    raw = line_v3(evidence=[{"file_id": file_id, "page": 1, "sheet": "Sheet",
                            "cell": "E2", "text": "Drawing", "source": "image"}]).model_dump(mode="json")
    job = SimpleNamespace(id=uploaded["id"], requirements=[raw], revision=1)
    run = SimpleNamespace(usage={"active_line_id": "test"})
    before = json.loads(json.dumps(raw))
    def extract(files, *args, **kwargs):
        assert [f.id for f in files] == [file_id]
        assert kwargs["_page_scope"] == {(file_id, 1)}
        assert kwargs["_excel_rows"] == (2,)
        return ExtractionResult(lines=[]), 1, {"total_tokens": 123}
    monkeypatch.setattr(deepseek, "extract", extract)
    with test_db() as db:
        result = execute_tool(db, job, run, "inspect_source",
                              ComponentInput(line_id="test"), set())
    assert result["read_only"] and result["usage"]["total_tokens"] == 123
    assert run.usage["total_tokens"] == run.usage["source_inspection_tokens"] == 123
    assert run.usage["work_state"]["test"]["line_tokens"] == 123
    assert job.requirements == [before] and job.revision == 1


def test_legacy_parser_upgrade_preserves_original_and_page_identity():
    from app.excel_manifest import ensure_preview, load_manifest
    item = prepare_file(embedded_workbook(), "old.xlsx")
    directory = storage_path(item["storage_key"])
    ensure_preview(directory, 1)
    manifest = json.loads((directory / "excel.json").read_text(encoding="utf-8"))
    manifest.pop("parser_version")
    manifest.pop("resource_summary")
    manifest["pages"][0]["images"] = []
    manifest["pages"][0].pop("resource_audit")
    (directory / "excel.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = (directory / "excel.json").read_bytes(), (directory / "page-1.png").read_bytes()
    updated = load_manifest(directory, upgrade=True)
    assert updated["upgrade_status"] == "compatible_cache"
    assert updated["pages"][0]["images"][0]["cell"] == "E2"
    assert updated["pages"][0]["images"][0]["storage"].startswith("excel-image-2000000")
    assert before == ((directory / "excel.json").read_bytes(), (directory / "page-1.png").read_bytes())
    assert load_manifest(directory) == updated
    assert not (directory / "page-v2-1.png").exists()
    assert ensure_preview(directory, 1).name == "page-v2-1.png"
    assert (directory / "page-1.png").read_bytes() == before[1]


def test_parser_upgrade_never_changes_evidence_page_number_silently():
    from app.excel_manifest import load_manifest
    item = prepare_file(embedded_workbook(), "old.xlsx")
    directory = storage_path(item["storage_key"])
    manifest = json.loads((directory / "excel.json").read_text(encoding="utf-8"))
    manifest.pop("parser_version")
    manifest["pages"][0]["rows"].pop()
    (directory / "excel.json").write_text(json.dumps(manifest), encoding="utf-8")
    updated = load_manifest(directory, upgrade=True)
    assert updated["upgrade_status"] == "requires_reextraction"
    assert not (directory / "excel-v2.json").exists()


def test_disabled_recovery_defers_program_exception_without_discarding_other_lines(client, monkeypatch, redis_worker):
    from app import agent_loop
    from app.config import settings
    monkeypatch.setattr(settings(), "agent_runtime_recovery", False)
    monkeypatch.setattr(agent_loop.deepseek, "get_key", lambda: "test-only-key")
    from test_agent_routing import enqueue, run_workers
    from test_estimate_first import provider, call, plan
    original = agent_loop.execute_tool
    def broken(db, job, run, name, args, discovered, interrupted=None):
        if name == "read_requirement":
            raise RuntimeError("private implementation exception")
        return original(db, job, run, name, args, discovered, interrupted=interrupted)
    job = enqueue(client, monkeypatch, 2)
    monkeypatch.setattr(agent_loop, "execute_tool", broken)
    provider(monkeypatch, [call("read_requirement", line_id="line0"),
                           call("complete_estimate", line_id="line1", estimate=plan())])
    run_workers()
    data = client.get(f'/api/jobs/{job["id"]}').json()
    issue = data["quotes"][0]["payload"]["lines"][0]["deferred_issue"]
    assert issue["cause"] == "execution_error" and issue["diagnostic_id"]
    assert "private implementation exception" not in json.dumps(data)
    assert data["quotes"][0]["payload"]["known_subtotal"] == "141.60"
