"""Source-aware counts; a local semantic doubt must not discard a whole extraction."""
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from openpyxl.utils.cell import coordinate_to_tuple


COUNT_UNITS = r"个|件|套|组|块|只|台|张|樘|面|pcs?|pieces?|sets?"
ANNOTATION = re.compile(
    rf"(?:制作数量|采购数量|订购数量|数量|QTY|QUANTITY)\s*[:：=]?\s*"
    rf"([0-9]+(?:\.0+)?)\s*(?:({COUNT_UNITS})(?![A-Za-z0-9])|(?=$|[,，。;；\n]))",
    re.I)


def annotation_counts(text):
    return {int(match[1].split(".")[0]) for match in ANNOTATION.finditer(
        unicodedata.normalize("NFKC", text))}


def normalize_quantity_values(raw):
    """Normalize only numeric scalars/units; unknown engineering counts remain unknown."""
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list):
        return
    for line in raw["lines"]:
        if not isinstance(line, dict):
            continue
        evidence = line.get("quantity_evidence", [])
        entries = [line, *[e for e in evidence if isinstance(e, dict)]] if isinstance(
            evidence, list) else [line]
        for entry in entries:
            value = entry.get("quantity")
            if type(value) not in (int, float, str) or len(str(value)) > 100:
                continue
            text = unicodedata.normalize("NFKC", str(value)).strip()
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z0-9\u4e00-\u9fff²㎡^]*)", text)
            if not match:
                continue
            try:
                amount = Decimal(match[1])
            except InvalidOperation:
                continue
            suffix = match[2].lower()
            count_unit = not suffix or re.fullmatch(COUNT_UNITS, suffix, re.I)
            if count_unit and amount == amount.to_integral_value():
                entry["quantity"] = int(amount)
            elif amount > 0 and (count_unit or suffix in {
                    "mm", "cm", "m", "m2", "m^2", "毫米", "厘米", "米", "平方米"}):
                entry["quantity"] = None
                if entry is line and isinstance(line.get("uncertainties", []), list):
                    line["uncertainties"] = list(dict.fromkeys([*line.get("uncertainties", []),
                        f"原数量输出“{text}”不是可核实成品件数，已保留项目及原始资料"]))[:20]
                else:
                    entry["raw_quantity"] = text


class QuantityEvidenceError(ValueError):
    def __init__(self, message, code, evidence, index, recoverable=True):
        super().__init__(message)
        self.recoverable = recoverable
        self.diagnostics = {
            "code": code, "evidence_index": index, "file_id": evidence.file_id,
            "page": evidence.page, "sheet": evidence.sheet, "cell": evidence.cell,
            "submitted_quantity": evidence.quantity,
        }


def excel_source(text):
    if not isinstance(text, str) or not text.lstrip().startswith("{"):
        return None
    try:
        source = json.loads(text)
    except ValueError:
        return None
    return source if isinstance(source, dict) and all(
        key in source for key in ("sheet", "rows", "images")) else None


def count_from_source(line, evidence, text, index):
    source = excel_source(text)

    def reject(message, code, recoverable=True):
        raise QuantityEvidenceError(message, code, evidence, index, recoverable)

    def require_count():
        if evidence.quantity is None:
            reject(f"数量依据未读清或属于工程量（{evidence.raw_quantity or '未知件数'}），不能认作成品数量",
                   "unknown_piece_count")

    if source is not None:
        from .customer_excel import validate_evidence, table_quantity
        try:
            validate_evidence(evidence, source)
        except ValueError:
            reject("数量依据的Excel工作表或单元格无效", "invalid_quantity_source", False)
        rows = {coordinate_to_tuple(e.cell)[0] for e in line.evidence
                if e.file_id == evidence.file_id and e.page == evidence.page
                and e.sheet == evidence.sheet and e.cell}
        if rows and coordinate_to_tuple(evidence.cell)[0] not in rows:
            reject("数量依据引用了其他项目的Excel行", "quantity_other_row", False)
        require_count()
        count = table_quantity(evidence, source)
        if count is not None:
            if count != evidence.quantity:
                reject(f"数量识别值{evidence.quantity}与Excel单元格实际件数{count}不一致",
                       "table_count_mismatch")
            evidence.source = "text"
            return count
        image = any(item["cell"] == evidence.cell for item in source["images"])
        if image:
            # The real drawing anchor distinguishes OCR from a count-column value.
            if annotation_counts(evidence.text) != {evidence.quantity}:
                reject("图纸数量标注无法证明成品件数", "unreadable_diagram_count")
            evidence.source = "image"
            return evidence.quantity
        cells = [cell for row in source["rows"] for cell in row["cells"]
                 if cell["cell"] == evidence.cell]
        cell = next(iter(cells), None)
        if cell and not cell.get("formula") and contains_quote(cell["value"], evidence.text):
            if annotation_counts(evidence.text) == {evidence.quantity}:
                evidence.source = "text"
                return evidence.quantity
        reject("数量依据不是有效成品件数；请核对原单元格、计量单位或图纸标注",
               "non_count_measurement")
    require_count()
    if text and evidence.source == "text" and not contains_quote(text, evidence.text):
        reject("数量引用文字未出现在该页文本层", "quantity_quote_mismatch")
    if annotation_counts(evidence.text) != {evidence.quantity}:
        reject("数量依据与标注不一致", "unreadable_quantity_annotation")
    return evidence.quantity


def contains_quote(source, quote):
    quoted = "".join(unicodedata.normalize("NFKC", quote).split())
    return bool(quoted) and quoted in "".join(unicodedata.normalize("NFKC", str(source)).split())


def validate_measurement_sources(line, source_texts):
    """Engineering quantities cannot borrow another project's provenance."""
    pages = {(e.file_id, e.page) for e in line.evidence}
    for measurement in line.source_measurements:
        for evidence in measurement.evidence:
            if (evidence.file_id, evidence.page) not in pages:
                raise ValueError("工程量来源不属于本项目所在页面")
            source = excel_source(source_texts.get((evidence.file_id, evidence.page), ""))
            if source is not None:
                from .customer_excel import validate_evidence
                validate_evidence(evidence, source)
                rows = {coordinate_to_tuple(e.cell)[0] for e in line.evidence
                        if e.file_id == evidence.file_id and e.page == evidence.page
                        and e.sheet == evidence.sheet and e.cell}
                if rows and coordinate_to_tuple(evidence.cell)[0] not in rows:
                    raise ValueError("工程量来源引用了其他项目的Excel行")


def validate_quantity_evidence(line, source_texts=None, recover_semantic=False):
    if not line.quantity_evidence:
        return
    pages = {(e.file_id, e.page) for e in line.evidence}
    counts = set()
    rejected = False
    original_quantity = line.quantity
    for index, evidence in enumerate(line.quantity_evidence):
        # Model-supplied validation flags are never trusted.
        evidence.validation_status = "unchecked"
        evidence.validation_note = ""
        if (evidence.file_id, evidence.page) not in pages:
            raise QuantityEvidenceError("数量来源不属于本项目所在页面", "quantity_other_page",
                                        evidence, index, False)
        try:
            count = count_from_source(line, evidence, (source_texts or {}).get(
                (evidence.file_id, evidence.page), ""), index)
        except QuantityEvidenceError as exc:
            if not recover_semantic or not exc.recoverable:
                raise
            rejected = True
            evidence.validation_status = "rejected"
            evidence.validation_note = str(exc)[:500]
            warning = f"数量依据待核实（{evidence.sheet or f'文件{evidence.file_id}'} " \
                      f"{evidence.cell or f'第{evidence.page}页'}）：{exc}"
            line.uncertainties = list(dict.fromkeys([*line.uncertainties, warning]))[:20]
            continue
        evidence.validation_status = "verified"
        counts.add(count)
    if not counts:
        # Neither a rejected claim nor its duplicate main value is a verified quantity.
        line.quantity = None
    elif len(counts) != 1 or (
            not rejected and original_quantity is not None and original_quantity not in counts):
        line.quantity = None
        if not any("数量" in warning and "冲突" in warning for warning in line.uncertainties):
            line.uncertainties = list(dict.fromkeys([
                *line.uncertainties, "本项目数量标注存在冲突，须确认"]))[:20]
    elif any("数量" in warning and "冲突" in warning for warning in line.uncertainties):
        line.quantity = None
    else:
        line.quantity = next(iter(counts))
