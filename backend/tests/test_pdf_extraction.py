import json
from decimal import Decimal
from types import SimpleNamespace

import httpx

from app import deepseek
from app.customer_files import prepare_file, pdf_page_text
from test_deepseek import configure_transport
from test_requirements import pdf_bytes


def test_pdf_pages_are_extracted_separately_with_native_text(monkeypatch):
    file = SimpleNamespace(id=41, **prepare_file(pdf_bytes(), "drawing.pdf"))
    seen = []
    monkeypatch.setattr(deepseek, "pdf_page_text", lambda f, p: f"第{p}页 原始表格内容")

    def handler(request):
        body = json.loads(request.content)
        parts = body["messages"][1]["content"]
        images = [p for p in parts if p["type"] == "image_url"]
        assert len(images) == 1
        page = len(seen) + 1
        assert any(f"第{page}页 原始表格内容" in p.get("text", "") for p in parts)
        seen.append(body)
        result = {"lines": [{"source_item": str(page), "product": f"标识{page}",
                              "evidence": [{"file_id": 41, "page": page, "text": f"第{page}页"}]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                     "message": {"content": json.dumps(result)}}],
                                        "usage": {"total_tokens": 10}})
    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert attempts == 2 and usage["total_tokens"] == 20
    assert [l.source_item for l in result.lines] == ["1", "2"]
    assert [l.evidence[0].page for l in result.lines] == [1, 2]


def test_unsupported_dimensions_are_cleared_not_used_as_price_inputs(monkeypatch):
    from test_deepseek import file_fixture
    file = file_fixture()
    monkeypatch.setattr(deepseek, "pdf_page_text", lambda f, p:
                        "1 项目形象LOGO 1925mm*650mm*50mm 边框1.2mm钢板 5mm面板 10mm背板。" * 4)

    def handler(request):
        result = {"lines": [{"source_item": "1", "product": "项目形象LOGO",
            "width_mm": 2125, "height_mm": 650, "thickness_mm": 16.2, "depth_mm": 50,
            "evidence": [{"file_id": 1, "page": 1, "text": "1925mm*650mm*50mm"}]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                     "message": {"content": json.dumps(result)}}]})
    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([file], "", [], "")
    line = result.lines[0]
    assert line.width_mm is None and line.thickness_mm is None
    assert line.height_mm == 650 and line.depth_mm == 50
    assert len(line.uncertainties) == 2


def test_image_dimension_survives_absence_from_native_text(monkeypatch):
    from test_deepseek import file_fixture
    file = file_fixture()
    monkeypatch.setattr(deepseek, "pdf_page_text", lambda f, p: "原文仅有做法但图示有尺寸。" * 20)

    def handler(request):
        result = {"lines": [{"product": "立牌", "height_mm": 2200,
            "evidence": [{"file_id": 1, "page": 1, "text": "立牌图示"}],
            "dimension_evidence": [{"file_id": 1, "page": 1, "text": "高2.2m",
                                   "field": "height_mm", "value": "2.2", "unit": "m", "source": "image"}]}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                     "message": {"content": json.dumps(result)}}]})
    configure_transport(monkeypatch, handler)
    result, _, _ = deepseek.extract([file], "", [], "")
    assert result.lines[0].height_mm == 2200
    assert not result.lines[0].uncertainties


def test_invalid_and_conflicting_dimension_evidence():
    import pytest
    from app.deepseek import validate_dimension_evidence
    from app.requirements_schemas import ExtractedLine
    raw = {"evidence": [{"file_id": 1, "page": 1, "text": "项目图"}],
           "dimension_evidence": [{"file_id": 1, "page": 1, "text": "2200",
                                  "field": "height_mm", "value": 2200}]}
    line = ExtractedLine.model_validate(raw)
    assert validate_dimension_evidence(line, {}) == {"height_mm"}
    line.dimension_evidence[0].page = 2
    with pytest.raises(ValueError):
        validate_dimension_evidence(line, {})
    line = ExtractedLine.model_validate(raw)
    line.dimension_evidence.append(line.dimension_evidence[0].model_copy(update={"text": "2300", "value": Decimal(2300)}))
    assert not validate_dimension_evidence(line, {})
    assert line.height_mm is None and line.uncertainties


def test_pdf_native_reader_handles_empty_text_and_closes_handles():
    file = SimpleNamespace(id=1, **prepare_file(pdf_bytes(), "empty-text.pdf"))
    assert pdf_page_text(file, 1) == ""
    assert pdf_page_text(file, 2) == ""


def test_source_row_coverage_and_conflicting_dimensions():
    from app.pdf_requirements import validate_rows, source_rows
    from app.requirements_schemas import ExtractionResult
    text = "19 品牌形象标识\n尺寸2386mm*570mm*40mm\n2386*320 20 提示牌\n150mm*50mm"
    assert set(source_rows(text)) == {"19", "20"}
    result = ExtractionResult.model_validate({"lines": [{
        "source_item": "19", "product": "品牌形象标识", "width_mm": 2386, "height_mm": 570,
        "evidence": [{"file_id": 1, "page": 1, "text": "2386*570"}]}]})
    checked = validate_rows(result, {(1, 1): text})
    assert checked.lines[0].width_mm is None
    assert checked.lines[0].uncertainties
    assert checked.lines[1].source_item == "20"
    assert checked.lines[1].quantity is None


def test_last_page_embedded_row_number_is_not_lost():
    from app.pdf_requirements import source_rows
    rows = source_rows("9 TM\n3mm透明亚克力切割\n表面喷漆红色，潘通185C 5 底壳\n1600mm*500mm")
    assert set(rows) == {"9", "5"}


def test_profile_section_is_not_an_overall_dimension_conflict():
    from app.pdf_requirements import validate_rows
    from app.requirements_schemas import ExtractionResult
    text = "8 史努比与朋友们\n尺寸：1040mm*650mm\n20*20*2mm不锈钢方通骨架\n1040*650\n9 TM\n3mm亚克力"
    result = ExtractionResult.model_validate({"lines": [{
        "source_item": "8", "width_mm": 1040, "height_mm": 650,
        "evidence": [{"file_id": 1, "page": 1, "text": "1040*650"}]}]})
    line = validate_rows(result, {(1, 1): text}).lines[0]
    assert line.width_mm == 1040 and line.height_mm == 650
    assert not line.uncertainties
    line.width_mm = line.height_mm = None
    line = validate_rows(result, {(1, 1): text}).lines[0]
    assert line.width_mm == 1040 and line.height_mm == 650


def test_diagram_quantity_is_used_once_for_repeated_views():
    from app.deepseek import validate_quantity_evidence
    from app.requirements_schemas import ExtractedLine
    line = ExtractedLine.model_validate({
        "evidence": [{"file_id": 1, "page": 1, "text": "8 项目"}],
        "quantity_evidence": [{"file_id": 1, "page": 1, "text": "数量：1个", "quantity": 1}] * 2})
    validate_quantity_evidence(line)
    assert line.quantity == 1
    line.quantity_evidence[1] = line.quantity_evidence[1].model_copy(
        update={"text": "数量：2个", "quantity": 2})
    validate_quantity_evidence(line)
    assert line.quantity is None and line.uncertainties


def test_quantity_evidence_must_quote_quantity_and_same_page():
    import pytest
    from app.deepseek import validate_quantity_evidence
    from app.requirements_schemas import ExtractedLine
    for evidence in [
        {"file_id": 1, "page": 1, "text": "一个图示", "quantity": 1},
        {"file_id": 1, "page": 2, "text": "数量：1个", "quantity": 1},
    ]:
        line = ExtractedLine.model_validate({
            "evidence": [{"file_id": 1, "page": 1, "text": "8 项目"}],
            "quantity_evidence": [evidence]})
        with pytest.raises(ValueError):
            validate_quantity_evidence(line)


def test_pdf_detail_crops_are_bounded():
    import io
    from PIL import Image
    from app.customer_files import pdf_detail_images
    file = SimpleNamespace(id=1, **prepare_file(pdf_bytes(), "drawing.pdf"))
    crops = pdf_detail_images(file, 1)
    assert len(crops) == 4
    for _, data in crops:
        with Image.open(io.BytesIO(data)) as image:
            assert 0 < image.width <= 1800 and 0 < image.height <= 1800
