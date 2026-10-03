"""Provisional completions stay separate from source facts and require explicit review."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import ast

from .models import PriceItem, PricingRule, Product
from .pricing_cases import arithmetic_formula, evaluate_case
from .requirements_schemas import RequirementLine
from . import business_rules
from .estimate_validation import validate_measurement, validate_expected_amount
from .pricing_strategies import measurement_variables, strategy_snapshot, target_object

MAX_AMOUNT = Decimal("999999999999.99")
FIELD_LABELS = {
    "quantity": "数量", "width_mm": "宽(mm)", "height_mm": "高(mm)",
    "thickness_mm": "板厚(mm)", "depth_mm": "壳深(mm)", "billing_length_mm": "计价长度(mm)",
    "material": "材质", "process": "工艺", "language": "文字类型", "pricing_category": "报价类别",
    "character_count": "每件字数", "area_m2": "图纸标注面积(平方米)", "power_count": "每件电源台数",
    "pricing_family": "计价分类", "count": "字组字符数", "quantity_per_item": "每件部件用量",
    "labor_hours": "工时", "geometry": "部件形状", "text_content": "文字内容",
}


def effective_line(line):
    values = line.model_dump()
    review = []
    seen = set()
    for item in line.estimate.assumptions:
        identity = (item.target, item.target_index, item.field)
        if identity in seen:
            raise ValueError("同一对象的同一字段不能重复补全")
        seen.add(identity)
        if line.estimate.contract_version == 1 and (item.target != "item" or item.mode != "fill"):
            raise ValueError("字组/部件补全和识别纠错需使用contract_version=2")
        obj = target_object(line, item.target, item.target_index)
        target = values if item.target == "item" else values[
            "letter_groups" if item.target == "letter_group" else "components"][item.target_index]
        original = getattr(obj, item.field)
        applied = original is None or original == "" or original == "unknown"
        if item.mode == "correct":
            if line.confirmed:
                raise ValueError("人工已确认的需求不能由Agent纠正，请通过人工审核修改")
            if str(original) != item.previous_value:
                raise ValueError("纠错previous_value与当前识别值不一致，请重新读取需求")
            pages = {(e.file_id, e.page) for e in [*line.evidence, *getattr(obj, "evidence", [])]}
            if any((e.file_id, e.page) not in pages for e in item.evidence):
                raise ValueError("纠错依据不属于当前项目的原始来源")
            numeric = {"quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                       "billing_length_mm", "character_count", "area_m2", "power_count",
                       "count", "quantity_per_item", "labor_hours"}
            if item.field in numeric:
                import re
                numbers = {Decimal(n) for e in item.evidence for n in re.findall(r"\d+(?:\.\d+)?", e.text)}
                try:
                    adopted = Decimal(item.value)
                except InvalidOperation as exc:
                    raise ValueError("纠错数值格式无效，请重新核对原文") from exc
                if not adopted.is_finite() or adopted not in numbers:
                    raise ValueError("纠错数值未出现在所引用的原文，请保留缺项并重新核对资料")
            applied = True
        if applied:
            target[item.field] = item.value
        path = (item.field if item.target == "item" else
                f'{"letter_groups" if item.target == "letter_group" else "components"}'
                f'[{item.target_index}].{item.field}')
        review.append({"kind": "correction" if item.mode == "correct" else "assumption",
                       "field": item.field, "path": path, "target": item.target, "target_index": item.target_index,
                       "mode": item.mode, "label": FIELD_LABELS[item.field],
                       "original": None if original is None else str(original),
                       "value": item.value, "reason": item.reason, "applied": applied,
                       "evidence": [e.model_dump(mode="json") for e in item.evidence]})
    values["estimate"] = None
    effective = RequirementLine.model_validate(values)
    # Only a single character makes line and group dimensions unambiguous.
    if (effective.pricing_family == "letter" and len(effective.letter_groups) == 1
            and effective.letter_groups[0].count == 1
            and effective.character_count in (None, 1)):
        group = effective.letter_groups[0]
        effective.letter_groups = [group.model_copy(update={
            field: getattr(effective, field) for field in (
                "width_mm", "height_mm", "billing_length_mm")
            if getattr(group, field) is None and getattr(effective, field) is not None})]
    return effective, review


def estimate_costs(db, line, effective):
    results = []
    totals = {
        "unit_subtotal": sum((c.amount for c in effective.extras if c.basis == "per_piece"), Decimal(0)),
        "fixed_subtotal": sum((c.amount for c in effective.extras if c.basis == "total"), Decimal(0)),
    }
    for index, cost in enumerate(line.estimate.costs):
        reference = None
        if cost.source == "catalog":
            price = db.get(PriceItem, cost.reference_id) if cost.reference_id else None
            if (not price or price.status == "inactive" or price.revision != cost.reference_revision
                    or price.amount is None or price.amount != cost.rate or price.unit == "unknown"):
                raise ValueError(f"{cost.label}：引用价格不存在、已变化或单价不一致，请重新核价")
            if line.estimate.contract_version == 2 and (
                    price.status != "active" or price.price_kind not in ("standard", "suggested")):
                raise ValueError(f"{cost.label}：引用价格尚未启用或仅为起价/参考价，请由管理员核价")
            reference = {"id": price.id, "revision": price.revision, "unit": price.unit,
                         "product": db.get(Product, price.product_id).name,
                         "status": price.status, "price_kind": price.price_kind, "notes": price.notes,
                         "included_kinds": catalog_coverage(price.attributes)}
        elif cost.source == "historical":
            rule = db.get(PricingRule, cost.reference_id) if cost.reference_id else None
            if not rule or rule.revision != cost.reference_revision:
                raise ValueError(f"{cost.label}：历史案例已变化或不存在")
            reference = evaluate_case(db, rule)
            if (not reference.get("historical_unit_price")
                    or Decimal(reference["historical_unit_price"]) != cost.rate):
                raise ValueError(f"{cost.label}：历史单价不可复算或不一致")
        elif cost.reference_id is not None or cost.reference_revision is not None:
            raise ValueError("Agent估算不能伪装成报价库或历史案例引用")
        # Per-piece amounts must not multiply by the order quantity a second time.
        try:
            tree = ast.parse(cost.quantity_formula[1:], mode="eval")
        except (SyntaxError, RecursionError) as exc:
            raise ValueError("费用公式语法无效") from exc
        required = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        if line.estimate.contract_version == 1 and cost.target != "item":
            raise ValueError("字组/部件费用需使用contract_version=2")
        variables, scoped = measurement_variables(effective, cost, required)
        variables.update(totals)
        variables["order_subtotal"] = (
            totals["unit_subtotal"] * effective.quantity + totals["fixed_subtotal"]
            if effective.quantity is not None else None)
        if cost.basis == "per_piece" and any(
                isinstance(node, ast.Name) and node.id in ("quantity", "order_subtotal", "fixed_subtotal")
                for node in ast.walk(tree)):
            raise ValueError("每件费用公式不能包含整单数量或整单小计；成品数量由程序统一相乘")
        if (business_rules.current(effective) and line.estimate.contract_version == 1
                and cost.source == "catalog" and cost.kind == "finished"):
            expected = ("letter_cm" if scoped.pricing_family == "letter" else "billing_length_mm / 10"
                        ) if reference["unit"] == "cm" else (
                            "billable_area_m2" if reference["unit"] == "m2" else None)
            if expected and (cost.basis != "per_piece" or
                             ast.dump(tree) != ast.dump(ast.parse(expected, mode="eval"))):
                raise ValueError(f"{cost.label}：成品引用须按={expected}计量；其他加价须单列工序及依据")
        measure = arithmetic_formula(cost.quantity_formula, variables)
        rate_unit = validate_measurement(cost, scoped, reference, line.estimate.contract_version)
        if line.estimate.contract_version == 2:
            from .estimate_validation import validate_cost_coverage
            validate_cost_coverage(cost, index, results, reference, measure)
            if effective.extras:
                from .estimate_validation import validate_extra_coverage
                validate_extra_coverage(cost, reference, effective.extras)
        if measure < 0:
            raise ValueError("费用计量值不能为负数")
        measure, multiplier = business_rules.cost_measure(cost, scoped, measure)
        amount = cost.rate * measure * multiplier
        if amount > MAX_AMOUNT:
            raise ValueError("估算费用超出允许范围")
        amount = amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        validate_expected_amount(cost, amount)
        results.append({**cost.model_dump(mode="json"), "rate_unit": rate_unit, "measure": str(measure),
                        "amount": str(amount),
                        "difficulty_multiplier": str(multiplier),
                        "resolved_formula": f"{cost.rate} × {measure} × {multiplier} = {amount}",
                        "parameters": {k: str(v) if v is not None else None for k, v in variables.items()},
                        "reference": reference})
        totals["unit_subtotal" if cost.basis == "per_piece" else "fixed_subtotal"] += amount
    return results


def catalog_coverage(attributes):
    included = (attributes or {}).get("included_cost_kinds", [])
    if included is None:
        return []
    allowed = {"material", "process", "power", "bracket", "outsourcing"}
    if not isinstance(included, list) or any(
            not isinstance(kind, str) or kind not in allowed for kind in included):
        raise ValueError("报价库已含费用范围格式无效，请由管理员核对价格记录")
    return included


def calculate_estimate(db, line, calculate_standard):
    base = {"line_id": line.id, "product": line.product, "requirement": line.model_dump(mode="json"),
            "estimated": True, "blockers": [], "warnings": [*line.uncertainties, "Agent补全方案待审核",
                                                         *business_rules.warnings(line)],
            "amount": None, "unit_price": None, "unit_formula": "", "formula": "",
            "price": None, "fixed_charges": "0", "estimate_review": [],
            "estimate_scope": line.estimate.scope}
    try:
        effective, assumptions = effective_line(line)
        validation_line = effective.model_copy(update={"estimate": line.estimate})
        if line.estimate.contract_version == 2:
            if line.estimate.pricing_strategy == "letters" and effective.pricing_family != "letter":
                raise ValueError("逐字计价策略与pricing_family不一致，请核对来源后纠正分类")
            if line.estimate.pricing_strategy == "composite" and not effective.components:
                raise ValueError("部件组合策略缺少components，请先整理实际部件，不得复用整件面积")
        business_rules.validate_plan(validation_line)
        business_rules.validate_fees(validation_line)
        business_rules.validate_extra_amounts(effective)
        base.update(effective_requirement=effective.model_dump(mode="json"), estimate_review=assumptions)
        base["pricing_strategy"] = strategy_snapshot(line.model_copy(update={
            "pricing_family": effective.pricing_family, "components": effective.components,
            "letter_groups": effective.letter_groups}))
        # Administrator prices remain authoritative; the Agent cannot replace them.
        if effective.manual_unit_price is not None or not line.estimate.costs:
            if effective.manual_unit_price is not None and line.estimate.costs:
                base["warnings"].append("人工单价优先；补全费用方案未参与本次计价")
            standard = calculate_standard(db, effective, automatic=True)
            base.update({k: v for k, v in standard.items()
                         if k not in ("requirement", "warnings")})
            base["warnings"].extend(standard["warnings"])
        else:
            costs = estimate_costs(db, line, effective)
            base["estimate_costs"] = costs
            unit = sum((Decimal(c["amount"]) for c in costs if c["basis"] == "per_piece"), Decimal(0))
            fixed = sum((Decimal(c["amount"]) for c in costs if c["basis"] == "total"), Decimal(0))
            unit += sum((c.amount for c in effective.extras if c.basis == "per_piece"), Decimal(0))
            fixed += sum((c.amount for c in effective.extras if c.basis == "total"), Decimal(0))
            if unit > MAX_AMOUNT or fixed > MAX_AMOUNT:
                raise ValueError("估算费用超出允许范围")
            base.update(unit_price=str(unit.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                        fixed_charges=str(fixed), unit_formula=" + ".join(
                            c["amount"] for c in costs if c["basis"] == "per_piece"))
            if not effective.product:
                base["blockers"].append("产品名称")
            if effective.quantity is None:
                base["blockers"].append("请由Agent补全暂定数量")
            if not base["blockers"]:
                total = unit * effective.quantity + fixed
                if total > MAX_AMOUNT:
                    raise ValueError("金额超出允许范围")
                base.update(amount=str(total.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                            formula=f"{unit} × {effective.quantity} + {fixed} = {total}")
    except ValueError as exc:
        base["blockers"].append(str(exc)[:1500])
        base["amount"] = None
    from .pricing_diagnostics import diagnostics_for
    base["diagnostics"] = diagnostics_for(line, base["blockers"])
    return base
