from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
from pydantic import ValidationError
import pytest

from app import deepseek
from app.customer_files import prepare_file
from app.extraction_checkpoints import ExtractionCheckpoint
from app.extraction_contract import normalize_extraction_response
from app.requirements_schemas import ExtractionResult
from test_customer_excel import workbook_bytes
from test_deepseek import configure_transport
from test_large_pdf import document_bytes
from test_requirements import png_bytes


@pytest.mark.parametrize("extra,expected", [
    ({"characters": 4}, {"character_count": 4}),
    ({"characters": "EXIT"}, {"text_content": "EXIT"}),
    ({"characters": ["E", "X", "I", "T"]}, {"text_content": "EXIT"}),
    ({"characters": {"text": "A B", "count": 2}},
     {"text_content": "A B", "character_count": 2}),
    ({"characters": {"text_content": "12"}}, {"text_content": "12"}),
    ({"char_count": "4", "letter_count": 4}, {"character_count": 4}),
    ({"letter_text": "出口", "qty": "2件"}, {"text_content": "出口", "quantity": 2}),
    ({"characters": 4, "character_count": "4"}, {"character_count": 4}),
])
def test_explicit_aliases_preserve_meaning_and_are_idempotent(extra, expected):
    raw = {"lines": [{"product": "Sign", **extra}]}
    assert normalize_extraction_response(raw)
    deepseek.normalize_quantity_values(raw)
    result = ExtractionResult.model_validate(raw)
    for field, value in expected.items():
        assert getattr(result.lines[0], field) == value
    assert result.lines[0].quantity is None or "qty" in extra
    assert not normalize_extraction_response(raw)


@pytest.mark.parametrize("extra", [
    {"characters": True}, {"characters": -1}, {"characters": 0}, {"characters": 10001},
    {"characters": 1.5}, {"characters": "12"}, {"characters": "12字"},
    {"characters": "12 characters"}, {"characters": "12mm"}, {"characters": None},
    {"characters": {"text": "ABC", "count": 2}},
    {"characters": {"text": "ABC", "count": 3, "price": 99}},
    {"characters": [{"character": "A", "height_mm": 100}]},
    {"characters": 3, "character_count": 4},
    {"characters": "ABC", "text_content": "XYZ"},
    {"characters": {"text": "ABC", "count": 3}, "character_count": 4},
    {"char_count": True}, {"char_count": -1}, {"char_count": "1.5"},
    {"letter_count": 4, "character_count": 5},
    {"qty": 2, "quantity": 3},
    {"price": 99}, {"manual_unit_price": 1}, {"confirmed": True},
])
def test_ambiguous_conflicting_and_business_fields_are_never_silently_discarded(extra):
    raw = {"lines": [deepcopy(extra)]}
    assert normalize_extraction_response(raw) == []
    assert raw["lines"][0] == extra
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(raw)


def test_letter_group_aliases_keep_dimension_targets_and_evidence():
    raw = {"lines": [{"letter_groups": [{
        "characters": {"text": "EXIT", "count": 4}, "count_basis": "characters",
        "evidence": [{"file_id": 1, "page": 2, "text": "EXIT"}]}],
        "dimension_evidence": [{"file_id": 1, "page": 2, "text": "100mm",
            "field": "height_mm", "value": 100, "target": "letter_group", "target_index": 0}]}]}
    normalize_extraction_response(raw)
    result = ExtractionResult.model_validate(raw)
    group = result.lines[0].letter_groups[0]
    assert group.count == 4 and group.text_content == "EXIT"
    assert group.evidence[0].page == 2
    assert result.lines[0].dimension_evidence[0].target_id == group.object_id
    assert result.lines[0].character_count is None


def test_aliases_do_not_relax_character_count_contract():
    raw = {"lines": [{"letter_groups": [{"character_count": 2, "text": "EXIT",
                                        "count_basis": "characters"}]}]}
    normalize_extraction_response(raw)
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(raw)


def reply(raw, tokens=10):
    content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
        "message": {"content": content}}], "usage": {"total_tokens": tokens}})


@pytest.mark.parametrize("kind", ["image", "pdf", "excel"])
def test_all_formats_share_adapter_validation_and_paid_checkpoint_reuse(monkeypatch, kind):
    data, name = {"image": (png_bytes(), "sign.png"),
                  "pdf": (document_bytes(), "sign.pdf"),
                  "excel": (workbook_bytes(rows=1, drawing=False), "sign.xlsx")}[kind]
    file = SimpleNamespace(id=41, **prepare_file(data, name))
    calls, saved = [], []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        evidence = {"file_id": 41, "page": 1, "text": "Sign"}
        if kind == "excel":
            parts = payload["messages"][1]["content"]
            source = next(json.loads(part["text"].split(":\n", 1)[1]) for part in parts
                          if "原始文本/单元格" in part.get("text", ""))
            cell = next(cell for row in source["rows"] for cell in row["cells"]
                        if cell["cell"].startswith("B") and row["row"] > 1)
            evidence.update(sheet=source["sheet"], cell=cell["cell"], text=cell["value"])
        return reply({"lines": [{"product": evidence["text"], "characters": 4,
                                 "evidence": [evidence]}]})

    configure_transport(monkeypatch, handler)
    checkpoint = ExtractionCheckpoint("aliases", {},
        lambda cache, usage: saved.append((deepcopy(cache), dict(usage))), lambda: None)
    options = {"_page_scope": {(41, 1)}, "_excel_plans": {}}
    if kind == "pdf":
        options["_pdf_region"] = (0, 0, 1, 0.5)
    result, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=checkpoint, **options)
    assert attempts == len(calls) == 1 and usage["total_tokens"] == 10
    assert result.lines[0].character_count == 4
    assert result.lines[0].quantity != 4
    assert "characters" not in saved[-1][0]["batches"][next(iter(saved[-1][0]["batches"]))]["lines"][0]
    restored = ExtractionCheckpoint("aliases", saved[-1][0], lambda *args: None, lambda: None)
    again, attempts, usage = deepseek.extract([file], "", [], "", _checkpoint=restored, **options)
    assert again.model_dump() == result.model_dump()
    assert attempts == 0 and usage == {} and len(calls) == 1


def test_repair_has_failed_response_and_precise_errors_without_losing_rows(monkeypatch):
    file = SimpleNamespace(id=1, **prepare_file(png_bytes(), "sign.png"))
    invalid = {"lines": [
        {"source_item": "1", "product": "First"},
        {"source_item": "2", "product": "Second",
         "characters": [{"text": "EXIT", "height_mm": 100}]}]}
    fixed = {"lines": [
        invalid["lines"][0],
        {"source_item": "2", "product": "Second", "letter_groups": [{
            "count": 4, "text_content": "EXIT", "height_mm": 100, "count_basis": "characters"}]}]}
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 2:
            messages = payload["messages"]
            assert messages[-2]["role"] == "assistant"
            assert json.loads(messages[-2]["content"]) == invalid
            assert "lines.1.characters" in messages[-1]["content"]
            assert "extra_forbidden" in messages[-1]["content"]
            assert "text_content" in messages[-1]["content"]
        return reply(invalid if len(calls) == 1 else fixed)

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert attempts == 2 and usage["total_tokens"] == 20
    assert [line.source_item for line in result.lines] == ["1", "2"]
    assert result.lines[1].letter_groups[0].height_mm == 100


@pytest.mark.parametrize("fixed", [
    {"lines": []},
    {"lines": [{"source_item": "1", "product": "First"}]},
    {"lines": [{"source_item": "1", "product": "First"},
               {"source_item": "3", "product": "Third"}]},
    {"lines": [{"source_item": "1", "product": "First"},
               {"source_item": "1", "product": "First"}]},
])
def test_schema_repair_cannot_bypass_validation_by_dropping_or_replacing_rows(monkeypatch, fixed):
    file = SimpleNamespace(id=1, **prepare_file(png_bytes(), "sign.png"))
    invalid = {"lines": [{"source_item": "1", "product": "First"},
                         {"source_item": "2", "product": "Second", "characters": "12"}]}
    calls = []

    def handler(request):
        calls.append(1)
        return reply(invalid if len(calls) == 1 else fixed)

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "")
    assert len(calls) == failure.value.attempts == 2
    assert failure.value.usage["total_tokens"] == 20
    assert failure.value.diagnostics["retryable"] is False
    assert failure.value.diagnostics["failures"][-1]["stage"] == "repair_coverage"


def test_non_json_response_is_included_in_bounded_repair_context(monkeypatch):
    file = SimpleNamespace(id=1, **prepare_file(png_bytes(), "sign.png"))
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 2:
            assert payload["messages"][-2] == {"role": "assistant", "content": '{"lines":'}
        return reply('{"lines":' if len(calls) == 1 else {"lines": [{"product": "Sign"}]})

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert result.lines[0].product == "Sign" and attempts == 2 and usage["total_tokens"] == 20


def test_row_validator_reason_reaches_repair_and_new_source_item_is_allowed(monkeypatch):
    file = SimpleNamespace(id=1, **prepare_file(png_bytes(), "sign.png"))
    invalid = {"lines": [{"product": "Sign", "letter_groups": [
        {"count": 2, "text_content": "EXIT", "count_basis": "characters"}]}]}
    fixed = deepcopy(invalid)
    fixed["lines"][0]["source_item"] = "1"
    fixed["lines"][0]["letter_groups"][0]["count"] = 4
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 2:
            assert "非空白字符数" in payload["messages"][-1]["content"]
            assert json.loads(payload["messages"][-2]["content"]) == invalid
        return reply(invalid if len(calls) == 1 else fixed)

    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file], "", [], "")
    assert result.lines[0].letter_groups[0].count == 4
    assert result.lines[0].source_item == "1"
    assert attempts == 2 and usage["total_tokens"] == 20


def test_normalized_alias_still_requires_real_sources(monkeypatch):
    file = SimpleNamespace(id=1, **prepare_file(png_bytes(), "sign.png"))
    configure_transport(monkeypatch, lambda request: reply({"lines": [{
        "product": "Sign", "characters": 4,
        "evidence": [{"file_id": 99, "page": 1, "text": "EXIT"}]}]}))
    with pytest.raises(deepseek.ModelFailure) as failure:
        deepseek.extract([file], "", [], "")
    assert failure.value.attempts == 2
    assert failure.value.diagnostics["failures"][-1]["stage"] == "file_source"
