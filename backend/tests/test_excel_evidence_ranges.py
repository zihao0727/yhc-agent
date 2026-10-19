import json

import pytest

from app.customer_excel import normalize_evidence, validate_evidence
from app.deepseek import validate_dimension_evidence, validate_quantity_evidence
from app.requirements_schemas import ExtractedLine
from app.extraction_checkpoints import input_signature


def source():
    return {"sheet": "Sheet1", "header_rows": [{"row": 2, "cells": [
        {"cell": "A2", "value": "编号"}, {"cell": "B2", "value": "名称"}, {"cell": "H2", "value": "数量"}]}],
        "images": [], "rows": [
            {"row": 13, "cells": [
                {"cell": "A13", "value": "12"}, {"cell": "B13", "value": "标识"},
                {"cell": "C13", "value": "铝板\n厚度1.2mm；喷漆"},
                {"cell": "D13", "value": "1560*300"},
                {"cell": "E13", "value": "=DISPIMG(1)", "formula": "=DISPIMG(1)"},
                {"cell": "G13", "value": "个"}, {"cell": "H13", "value": "1"}]},
            {"row": 14, "cells": [
                {"cell": "A14", "value": "13"}, {"cell": "B14", "value": "标识"},
                {"cell": "C14", "value": "不锈钢"}, {"cell": "H14", "value": "2"}]}]}


def line(cell="", **changes):
    return ExtractedLine.model_validate({"source_item": "12", "product": "标识",
        "evidence": [{"file_id": 8, "page": 1, "sheet": "Sheet1", "cell": cell,
                      "text": "12 标识 1560*300 铝板制作"}], **changes})


@pytest.mark.parametrize("address", ["A13-H13", "A13:H13", "$A$13:$H$13", "'Sheet1'!A13:H13"])
def test_same_row_range_expands_actual_cell_quotes_and_retains_original_range(address):
    current, original = line(address), source()
    normalize_evidence(current, original, 8, 1)
    assert {e.cell for e in current.evidence} == {"A13", "B13", "C13", "D13", "G13", "H13"}
    assert all(e.source_range == "A13:H13" for e in current.evidence)
    assert current.evidence[2].text == "铝板\n厚度1.2mm；喷漆"
    for evidence in current.evidence:
        validate_evidence(evidence, original)
    saved = current.model_dump()
    normalize_evidence(current, original, 8, 1)
    assert current.model_dump() == saved


def test_missing_address_uses_unique_actual_item_and_product_not_array_index():
    current, original = line(), source()
    normalize_evidence(current, original, 8, 1)
    assert all(e.cell.endswith("13") for e in current.evidence)
    assert any("唯一项目序号和名称" in warning for warning in current.uncertainties)
    for evidence in current.evidence:
        validate_evidence(evidence, original)


@pytest.mark.parametrize("changes", [
    {"source_item": ""}, {"source_item": "999"}, {"product": "编造产品"},
])
def test_missing_address_without_matching_item_and_product_is_not_guessed(changes):
    current = line(**changes)
    normalize_evidence(current, source(), 8, 1)
    assert current.evidence[0].cell == ""
    with pytest.raises(ValueError):
        validate_evidence(current.evidence[0], source())


def test_repeated_item_and_product_are_ambiguous_and_not_guessed():
    original = source()
    original["rows"][1]["cells"][0]["value"] = "12"
    current = line()
    normalize_evidence(current, original, 8, 1)
    with pytest.raises(ValueError):
        validate_evidence(current.evidence[0], original)


@pytest.mark.parametrize("address", [
    "A13:H14", "A13:H999", "A13:X13", "H13:A13", "A14:H14", "Other!A13:H13",
])
def test_invalid_or_other_project_ranges_are_never_replaced(address):
    current = line(address)
    normalize_evidence(current, source(), 8, 1)
    with pytest.raises(ValueError):
        validate_evidence(current.evidence[0], source())


def test_auxiliary_range_keeps_dimension_and_quantity_bound_to_actual_cells():
    current = line("B13", quantity_evidence=[
        {"file_id": 8, "page": 1, "sheet": "Sheet1", "cell": "A13:H13", "text": "1个", "quantity": 1}],
        dimension_evidence=[{"file_id": 8, "page": 1, "sheet": "Sheet1", "cell": "A13:H13",
            "field": "thickness_mm", "value": 1.2, "text": "厚度1.2mm", "source": "text", "role": "plate_thickness"}])
    normalize_evidence(current, source(), 8, 1)
    assert current.quantity_evidence[0].cell == "H13"
    assert current.dimension_evidence[0].cell == "C13"
    assert current.dimension_evidence[0].source_range == "A13:H13"
    texts = {(8, 1): json.dumps(source(), ensure_ascii=False)}
    validate_quantity_evidence(current, texts)
    assert validate_dimension_evidence(current, texts) == {"thickness_mm"}


def test_multiline_dimension_quote_is_checked_against_decoded_cell_not_json_escapes():
    current = line("B13", dimension_evidence=[
        {"file_id": 8, "page": 1, "sheet": "Sheet1", "cell": "", "field": "thickness_mm",
         "value": 1.2, "text": "铝板\n厚度1.2mm；喷漆", "source": "text", "role": "plate_thickness"}])
    normalize_evidence(current, source(), 8, 1)
    assert current.dimension_evidence[0].cell == "C13"
    validate_dimension_evidence(current, {(8, 1): json.dumps(source(), ensure_ascii=False)})


def test_dimension_quote_in_another_cell_cannot_validate_a_wrong_cell_reference():
    current = line("B13", dimension_evidence=[
        {"file_id": 8, "page": 1, "sheet": "Sheet1", "cell": "D13", "field": "thickness_mm",
         "value": 1.2, "text": "厚度1.2mm", "source": "text", "role": "plate_thickness"}])
    normalize_evidence(current, source(), 8, 1)
    with pytest.raises(ValueError):
        validate_dimension_evidence(current, {(8, 1): json.dumps(source(), ensure_ascii=False)})


def test_range_metadata_must_include_the_real_single_cell():
    current = line("B13")
    current.evidence[0].source_range = "A14:H14"
    with pytest.raises(ValueError):
        validate_evidence(current.evidence[0], source())


def test_resume_commands_do_not_invalidate_validated_batches_but_fact_changes_do():
    before = input_signature([], "客户需求\n继续", [], "继续")
    assert input_signature([], "客户需求\n继续\n重试\n请继续报价！", [], "重试") == before
    assert input_signature([], "客户需求\n数量改为3件", [], "数量改为3件") != before
    assert input_signature([], "客户需求\n继续", [], "继续", include_execution_commands=True) != before
