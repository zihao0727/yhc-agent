"""Explicit cost columns of an imported material/process quotation."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

PART_COLUMNS = {"material_price": "H", "process_1_price": "E", "process_2_price": "G"}


def part_rate(price, part):
    if part not in PART_COLUMNS or price.unit != "m2":
        raise ValueError("成本分项只适用于明确按平方米计量的材料工艺原表")
    raw = (price.attributes or {}).get(part)
    if raw is None or isinstance(raw, bool) or not str(raw).strip():
        raise ValueError("原表成本分项为空，不能默认零元")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError("原表成本分项不是有效数值") from exc
    if not value.is_finite() or value < 0:
        raise ValueError("原表成本分项数值无效")
    # Imported formula results can exceed the engine's six-place rate contract.
    # Keep attributes untouched and use this same boundary for pricing and validation.
    if value >= Decimal("1000000000"):
        raise ValueError("原表成本分项超出计价单价范围")
    try:
        rounded = value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("原表成本分项无法转换为计价单价") from exc
    if rounded >= Decimal("1000000000"):
        raise ValueError("原表成本分项超出计价单价范围")
    return rounded if rounded != value else value


def part_source_cell(price, part):
    match = re.fullmatch(r"J([1-9]\d*)", price.source_cell or "")
    return PART_COLUMNS[part] + match[1] if match else ""


def validate_part_process(price, part, line, source_text=""):
    if part not in ("process_1_price", "process_2_price"):
        return
    process = str((price.attributes or {}).get(part.removesuffix("_price"), "")).strip()
    if process == "无":
        raise ValueError("原表该分项工艺为“无”，不能把零元空工序冒充丝印等加工费；请选择实际工序列")
    if not process:
        return
    source = ";".join([line.material, line.process, line.notes,
                       *(e.text for e in line.evidence), source_text])
    from .process_taxonomy import positive_clauses, process_codes, process_matches
    source = " ".join(positive_clauses(source))
    alternatives = re.split(r"[/／、]", process)
    requested = process_codes(source)
    matched = False
    for alternative in alternatives:
        codes = process_codes(alternative)
        matched |= (any(process_matches(code, available) for code in codes for available in requested)
                    if codes else alternative.casefold() in source.casefold())
    if not matched:
        raise ValueError(f"原表分项工艺为“{process}”，当前项目没有对应工艺依据；"
                         "不能把参考工艺冒充匹配成本。若采用类比暂估，须单列暂估费率并说明差异和依据")
