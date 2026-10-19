import io
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app import agent_worker, deepseek
from app.config import settings
from app.customer_files import image_regions, prepare_file, visual_region_content
from app.extraction_checkpoints import ExtractionCheckpoint
from app.models import AgentRun, RequirementJob
from app.requirements_schemas import ExtractionResult
from app.source_reconciliation import reconcile_sources, source_review
from test_deepseek import configure_transport
from test_large_pdf import document_bytes, response
from test_redis_agent import redis_worker
from test_requirements import create_job, line_input, png_bytes, save_lines


@pytest.fixture(autouse=True)
def enable_visual_audit(isolate_customer_storage, monkeypatch):
    monkeypatch.setattr(settings(), "visual_region_audit", True)


def task(request):
    body = json.loads(request.content)
    first = body["messages"][1]["content"][0]["text"]
    return json.loads(first)


def observation(item="1", product="Door", file_id=1, **values):
    return {"source_item": item, "product": product, "quantity": 2,
            "evidence": [{"file_id": file_id, "page": 1, "text": f"{item} {product}", "source": "image"}],
            **values}


def inventory(*items, role="requirements"):
    return {"document_role": role, "disposition": "content", "entries": [
        {"kind": "item", "source_item": item["source_item"], "label": item["product"],
         "source_code": item.get("source_code", "")} for item in items]}


def file_fixture(data=None, filename="sign.png", file_id=1):
    return SimpleNamespace(id=file_id, **prepare_file(data or png_bytes(), filename))


def test_independent_scan_recovers_missing_item_and_preserves_quantity(monkeypatch):
    file = file_fixture()
    first, missing = observation(), observation("2", "Exit")
    calls = []

    def handler(request):
        data = task(request)
        calls.append(data)
        if data.get("task") == "visual_region_inventory":
            assert "previous_requirements" not in data and "lines" not in data
            return response(inventory(first, missing))
        if len(calls) == 1:
            return response({"lines": [first]})
        text = str(json.loads(request.content))
        assert "coverage_recheck" in text
        return response({"lines": [first, missing]})

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert len(calls) == attempts == 3 and usage["total_tokens"] == 30
    assert [(line.source_item, line.quantity) for line in result.lines] == [("1", 2), ("2", 2)]
    regions = [entry for entry in result.source_coverage if entry.kind == "region"]
    assert len(regions) == 1 and regions[0].expected_items == regions[0].extracted_items == 2
    assert not source_review(result.model_dump(mode="json"))["required"]


def test_unresolved_omission_remains_unpriced_and_does_not_count_as_extracted(monkeypatch):
    missing = observation("2", "Exit")
    configure_transport(monkeypatch, lambda request: response(
        inventory(missing) if task(request).get("task") else {"lines": []}))
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert attempts == 3 and len(result.lines) == 1
    assert result.lines[0].quantity is None and result.lines[0].uncertainties
    region = next(entry for entry in result.source_coverage if entry.kind == "region")
    assert region.expected_items == 1 and region.extracted_items == 0 and region.status == "unresolved"
    assert source_review(result.model_dump(mode="json"))["unresolved_regions"] == 1


def test_notes_and_unreadable_regions_are_not_empty_products(monkeypatch):
    entries = {"document_role": "drawing", "disposition": "content", "entries": [
        {"kind": "note", "label": "安装不含，指定品牌灯珠"},
        {"kind": "unreadable", "label": "右侧数量标注未读清"}]}
    configure_transport(monkeypatch, lambda request: response(
        entries if task(request).get("task") else {"lines": []}))
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert attempts == 2 and len(result.lines) == 1 and result.lines[0].quantity is None
    assert any("指定品牌" in question for question in result.questions)
    assert any(entry.kind == "note" and entry.status == "non_product" for entry in result.source_coverage)


def test_empty_region_has_explicit_non_product_coverage(monkeypatch):
    configure_transport(monkeypatch, lambda request: response(
        {"document_role": "unknown", "disposition": "empty", "entries": []}
        if task(request).get("task") else {"lines": []}))
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert not result.lines and attempts == 2
    assert not source_review(result.model_dump(mode="json"))["required"]
    assert any(entry.kind == "empty" and entry.status == "non_product" for entry in result.source_coverage)


def test_extracted_product_in_an_independently_empty_region_is_not_silently_accepted(monkeypatch):
    configure_transport(monkeypatch, lambda request: response(
        {"document_role": "unknown", "disposition": "empty", "entries": []}
        if task(request).get("task") else {"lines": [observation()]}))
    result, _, _ = deepseek.extract([file_fixture()], "", [], "")
    assert result.lines[0].uncertainties
    assert source_review(result.model_dump(mode="json"))["required"]
    assert next(entry for entry in result.source_coverage if entry.kind == "region").status == "unresolved"


def test_audit_network_failure_resumes_without_repeating_extraction(monkeypatch):
    file, snapshots, calls = file_fixture(), [], []
    first = observation()

    def handler(request):
        data = task(request)
        calls.append(data)
        if len(calls) == 2:
            return httpx.Response(503)
        return response(inventory(first) if data.get("task") else {"lines": [first]})

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("audit", {}, lambda value, usage: snapshots.append(deepcopy(value)),
                                      lambda: None)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "", _checkpoint=checkpoint)
    assert failure.value.usage["total_tokens"] == 10
    restored = ExtractionCheckpoint("audit", snapshots[-1], lambda *args: None, lambda: None)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert len(calls) == 3 and attempts == 1 and usage["total_tokens"] == 10
    assert len(result.lines) == 1
    again, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored)
    assert again == result and attempts == 0 and usage == {} and len(calls) == 3


def test_dense_inventory_subdivides_and_keeps_paid_usage(monkeypatch):
    file, calls = file_fixture(), []

    def handler(request):
        data = task(request)
        calls.append(data)
        if len(calls) == 2:
            return response({}, finish="length")
        if data.get("task"):
            item = observation("2" if data["region"][1] else "1")
            return response(inventory(item))
        # Evidence carries the same item identity through the child crop.
        item = observation("2" if len(calls) == 5 else "1")
        return response({"lines": [item]})

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert len(calls) == attempts == 6 and usage["total_tokens"] == 60
    assert {line.source_item for line in result.lines} == {"1", "2"}
    assert len([entry for entry in result.source_coverage if entry.kind == "region"]) == 2


def test_original_image_tiles_are_bounded_and_keep_overlap(monkeypatch):
    output = io.BytesIO()
    with Image.new("RGB", (4200, 2200), "red") as image:
        image.paste("green", (2000, 0, 4200, 2200))
        image.save(output, "PNG")
    file = file_fixture(output.getvalue())
    assert len(image_regions(file)) == 6
    for region in image_regions(file):
        text, pixels = visual_region_content(file, 1, region)
        assert text == ""
        with Image.open(io.BytesIO(pixels)) as image:
            assert max(image.size) <= 1800 and min(image.size) > 0


def test_exif_rotation_precedes_region_coordinates():
    output = io.BytesIO()
    with Image.new("RGB", (2000, 1000), "red") as image:
        image.paste("blue", (1000, 0, 2000, 1000))
        exif = image.getexif()
        exif[274] = 6
        image.save(output, "JPEG", exif=exif)
    file = file_fixture(output.getvalue(), "photo.jpg")
    assert image_regions(file) == [(0, 0, 1, .5), (0, .5, 1, 1)]
    _, pixels = visual_region_content(file, 1, (0, 0, 1, .5))
    with Image.open(io.BytesIO(pixels)) as image:
        red, green, blue = image.getpixel((image.width // 2, image.height // 3))
        assert red > 200 and blue < 50


def coded_line(file_id, role, **values):
    return observation("1", "Door", file_id, source_code="DD-01", document_role=role,
                       evidence=[{"file_id": file_id, "page": 1, "text": "DD-01 Door", "source": "image"}],
                       **values)


def test_unique_project_code_combines_schedule_and_drawing_without_summing():
    result = ExtractionResult.model_validate({"lines": [
        coded_line(1, "requirements"), coded_line(2, "drawing", width_mm=300,
            dimension_evidence=[{"file_id": 2, "page": 1, "text": "宽300mm", "field": "width_mm",
                                 "value": 300, "role": "overall", "source": "image"}])]})
    result = reconcile_sources(result, [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    assert len(result.lines) == 1 and result.lines[0].quantity == 2
    assert result.lines[0].width_mm == 300 and {e.file_id for e in result.lines[0].evidence} == {1, 2}
    assert result.source_links[0].status == "merged"


def test_conflicts_cannot_be_overwritten_by_a_later_matching_view():
    result = ExtractionResult.model_validate({"lines": [
        coded_line(1, "requirements", width_mm=300), coded_line(2, "drawing", width_mm=320),
        coded_line(3, "drawing", width_mm=300)]})
    result = reconcile_sources(result, [SimpleNamespace(id=i) for i in range(1, 4)])
    assert len(result.lines) == 1 and result.lines[0].width_mm is None
    assert all(link.status == "unresolved" for link in result.source_links)


def test_same_name_and_numeric_index_do_not_prove_cross_file_identity():
    result = ExtractionResult.model_validate({"lines": [
        observation(document_role="requirements"), observation(file_id=2, document_role="drawing")]})
    result = reconcile_sources(result, [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    assert len(result.lines) == 2 and result.lines[1].quantity is None
    assert result.source_links[0].status == "unresolved"


def test_repeated_code_within_one_schedule_is_not_uniquely_linked():
    result = ExtractionResult.model_validate({"lines": [
        coded_line(1, "requirements"), coded_line(1, "requirements"), coded_line(2, "drawing")]})
    result = reconcile_sources(result, [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    assert len(result.lines) == 3 and result.lines[2].quantity is None
    assert result.source_links[0].status == "unresolved"


def test_drawing_views_across_pdf_pages_link_to_one_schedule_item():
    first, second = coded_line(2, "drawing"), coded_line(2, "drawing")
    second["evidence"][0]["page"] = 2
    result = ExtractionResult.model_validate({"lines": [
        coded_line(1, "requirements"), first, second]})
    result = reconcile_sources(result, [SimpleNamespace(id=1), SimpleNamespace(id=2)])
    assert len(result.lines) == 1 and result.lines[0].quantity == 2
    assert {(e.file_id, e.page) for e in result.lines[0].evidence} == {(1, 1), (2, 1), (2, 2)}
    assert all(link.status == "merged" for link in result.source_links)


def test_unnumbered_recheck_uses_exact_same_region_quote_without_duplicating(monkeypatch):
    first, missing = observation("", "Door"), observation("", "Exit")
    calls = []

    def handler(request):
        data = task(request)
        calls.append(data)
        if data.get("task"):
            return response(inventory(first, missing))
        return response({"lines": [first] if len(calls) == 1 else [first, missing]})

    configure_transport(monkeypatch, handler)
    result, attempts, _ = deepseek.extract([file_fixture()], "", [], "")
    assert attempts == 3
    assert [(line.product, line.quantity) for line in result.lines] == [("Door", 2), ("Exit", 2)]
    assert not any(line.source_item for line in result.lines)


def test_duplicate_attachment_hash_is_not_additional_quantity():
    result = ExtractionResult.model_validate({"lines": [
        coded_line(1, "requirements"), coded_line(2, "requirements")]})
    result = reconcile_sources(result, [SimpleNamespace(id=i, sha256="same") for i in (1, 2)])
    assert len(result.lines) == 1 and result.lines[0].quantity == 2


def test_price_reference_is_retained_as_source_and_not_customer_quantity(monkeypatch):
    reference = observation(document_role="price_reference")
    configure_transport(monkeypatch, lambda request: response(
        inventory(reference, role="price_reference") if task(request).get("task") else {"lines": [reference]}))
    result, _, _ = deepseek.extract([file_fixture()], "", [], "")
    assert not result.lines and result.source_links[0].status == "reference"
    assert all(entry.status == "non_product" for entry in result.source_coverage)


def test_cross_file_code_requires_actual_source_quote():
    from app.extraction_validation import ExtractionValidationFailure, validate_extraction
    file = SimpleNamespace(id=1, page_count=1, media_type="image/png")
    with pytest.raises(ExtractionValidationFailure):
        validate_extraction({"lines": [observation(source_code="invented")]}, [file], {(1, 1): ""}, "", "")


def test_partial_code_cannot_claim_a_longer_project_code():
    from app.visual_audit import quoted_code
    assert quoted_code("DD-01", "项目 DD - 01 Door")
    assert not quoted_code("DD-01", "项目 DD-010 Door")
    assert not quoted_code("DD-01", "项目 XDD-01 Door")


def test_pricing_inspection_can_select_one_region_of_a_merged_project(client, test_db, monkeypatch):
    from app.agent_execution import execute_tool
    from app.agent_tools import SourceInspectionInput
    uploads = []
    for index in range(3):
        output = io.BytesIO()
        with Image.new("RGB", (240, 140), (index * 50, 100, 150)) as image:
            image.save(output, "PNG")
        uploads.append(("files", (f"view{index}.png", output.getvalue())))
    response = client.post("/api/jobs", data={"title": "Scoped inspection"}, files=uploads)
    assert response.status_code == 201, response.text
    job = response.json()
    raw = line_input()
    raw["evidence"] = [{"file_id": file["id"], "page": 1, "text": "Door", "source": "image",
                        "region": [.5, 0, 1, .5]} for file in job["files"]]
    record = SimpleNamespace(id=job["id"], requirements=[raw], revision=1)
    run = SimpleNamespace(usage={"active_line_id": raw["id"]})
    selected = job["files"][2]["id"]

    def inspect(files, *args, **kwargs):
        assert [file.id for file in files] == [selected]
        assert kwargs["_page_scope"] == {(selected, 1)}
        assert kwargs["_pdf_region"] == (.5, 0, 1, .5)
        return ExtractionResult(lines=[]), 1, {"total_tokens": 10}

    monkeypatch.setattr(deepseek, "extract", inspect)
    with test_db() as db:
        result = execute_tool(db, record, run, "inspect_source",
            SourceInspectionInput(line_id=raw["id"], file_id=selected, page=1), set())
        assert result["read_only"] and run.usage["total_tokens"] == 10
        rejected = execute_tool(db, record, run, "inspect_source",
            SourceInspectionInput(line_id=raw["id"], file_id=999, page=1), set())
        assert rejected["cause"] == "recognition_review"
    assert record.requirements == [raw]


def test_region_preview_requires_auth_valid_bounds_and_visual_file(client):
    job = create_job(client)
    fid = job["files"][0]["id"]
    url = f"/api/customer-files/{fid}/pages/1/region?left=0&top=0&right=.5&bottom=.5"
    result = client.get(url)
    assert result.status_code == 200 and result.headers["content-type"] == "image/png"
    assert client.get(url.replace("right=.5", "right=0")).status_code == 422
    assert client.get(url.replace("left=0", "left=-1")).status_code == 422
    assert client.get(url.replace("/pages/1/", "/pages/2/")).status_code == 404
    assert client.get(url, headers={"X-Admin-Token": "wrong"}).status_code == 401


def test_unresolved_source_review_reaches_quote_and_requires_explicit_approval(client, test_db):
    job = create_job(client)
    raw = line_input()
    raw.update(manual_unit_price="100", manual_price_note="人工核实整件价格")
    job = save_lines(client, job, [raw])
    with test_db() as db:
        record = db.get(RequirementJob, job["id"])
        record.extraction = {"source_coverage": [{
            "file_id": job["files"][0]["id"], "page": 1, "row": 0, "region": [0, 0, 1, 1],
            "status": "unresolved", "verification": "visual", "kind": "view",
            "label": "未对应侧视图", "reason": "须核对项目"}]}
        db.commit()
    job = client.get(f'/api/jobs/{job["id"]}').json()
    result = client.post(f'/api/jobs/{job["id"]}/quotes', json={
        "revision": job["revision"], "terms": "人工核实制作范围"})
    assert result.status_code == 201, result.text
    quote = result.json()
    assert quote["payload"]["source_review"]["required"]
    request = {"note": "审核资料及价格", "confirmed_commercial_terms": True}
    url = f'/api/quotes/{quote["id"]}/approve'
    assert client.post(url, json=request).status_code == 422
    result = client.post(url, json={**request, "confirmed_source_review": True})
    assert result.status_code == 200, result.text
    assert result.json()["payload"]["source_review_confirmed"]


def test_image_background_checkpoint_never_quotes_partially_audited_pages(
        client, monkeypatch, redis_worker, test_db):
    first = observation()
    job = create_job(client)
    first["evidence"][0]["file_id"] = job["files"][0]["id"]
    monkeypatch.setattr(settings(), "agent_background", True)
    monkeypatch.setattr(settings(), "extraction_slice_batches", 1)
    calls = []

    def handler(request):
        calls.append(task(request))
        return response(inventory(first) if calls[-1].get("task") else {"lines": [first]})

    configure_transport(monkeypatch, handler)
    result = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert result.status_code == 200, result.text
    agent_worker.run_pending_once(threading.Event())
    partial = client.get(f'/api/jobs/{job["id"]}').json()
    assert partial["extraction"]["incomplete"] and not partial["requirements"]
    assert client.post(f'/api/jobs/{job["id"]}/quotes', json={
        "revision": partial["revision"], "terms": "资料审核"}).status_code == 409
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    complete = client.get(f'/api/jobs/{job["id"]}').json()
    assert not complete["extraction"].get("incomplete") and len(complete["requirements"]) == 1
    assert len(calls) == 2
    with test_db() as db:
        assert db.scalar(select(AgentRun)).status == "succeeded"


def test_mixed_excel_pdf_image_upload_combines_before_pricing(client, monkeypatch):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.active.append(["序号", "项目编码", "项目名称", "数量", "单位"])
    workbook.active.append([1, "DD-01", "Door", 2, "件"])
    output = io.BytesIO()
    workbook.save(output)
    result = client.post("/api/jobs", data={"title": "Mixed", "business_rules_version": "legacy"}, files=[
        ("files", ("schedule.xlsx", output.getvalue())),
        ("files", ("drawing.pdf", document_bytes(pages=1))),
        ("files", ("note.png", png_bytes())),
    ])
    assert result.status_code == 201, result.text
    job = result.json()
    pdf_id, note_id = job["files"][1]["id"], job["files"][2]["id"]
    drawing = coded_line(pdf_id, "drawing")
    calls = []

    def handler(request):
        data = task(request)
        calls.append(data)
        fid = data.get("file_id") or data["files"][0]["file_id"]
        if data.get("task"):
            return response(inventory(drawing, role="drawing") if fid == pdf_id else {
                "document_role": "unknown", "disposition": "content",
                "entries": [{"kind": "note", "label": "指定品牌，安装不含"}]})
        return response({"lines": [drawing] if fid == pdf_id else []})

    configure_transport(monkeypatch, handler)
    result = client.post(f'/api/jobs/{job["id"]}/extract', json={
        "revision": job["revision"], "allow_external_processing": True})
    assert result.status_code == 200, result.text
    extracted = result.json()
    assert len(extracted["requirements"]) == 1 and extracted["requirements"][0]["quantity"] == 2
    assert {e["file_id"] for e in extracted["requirements"][0]["evidence"]} == {
        job["files"][0]["id"], pdf_id}
    assert extracted["extraction"]["source_links"][0]["status"] == "merged"
    assert any("指定品牌" in question for question in extracted["extraction"]["questions"])
    assert any(entry["file_id"] == note_id for entry in extracted["extraction"]["source_coverage"])
