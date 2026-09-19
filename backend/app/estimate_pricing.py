"""Provisional completions stay separate from source facts and require explicit review."""
from decimal import Decimal, ROUND_HALF_UP
import ast

from .models import PriceItem, PricingRule, Product
from .pricing_cases import arithmetic_formula, evaluate_case
from .requirements_schemas import RequirementLine

MAX_AMOUNT = Decimal("999999999999.99")
FIELD_LABELS = {
    "quantity": "数量", "width_mm": "宽(mm)", "height_mm": "高(mm)",
    "thickness_mm": "板厚(mm)", "depth_mm": "壳深(mm)", "billing_length_mm": "计价长度(mm)",
    "material": "材质", "process": "工艺", "language": "文字类型", "pricing_category": "报价类别",
}


def effective_line(line):
    values = line.model_dump()
    review = []
    seen = set()
    for item in line.estimate.assumptions:
        if item.field in seen:
            raise ValueError("同一字段不能重复补全")
        seen.add(item.field)
        original = values[item.field]
        applied = original is None or original == "" or original == "unknown"
        if applied:
            values[item.field] = item.value
        review.append({"kind": "assumption", "field": item.field, "label": FIELD_LABELS[item.field],
                       "original": None if original is None else str(original),
                       "value": item.value, "reason": item.reason, "applied": applied})
    values["estimate"] = None
    return RequirementLine.model_validate(values), review


def estimate_costs(db, line, effective):
    results = []
    variables = {name: getattr(effective, name) for name in (
        "width_mm", "height_mm", "thickness_mm", "depth_mm", "billing_length_mm", "quantity")}
    variables["area_m2"] = (effective.width_mm * effective.height_mm / Decimal(1_000_000)
                            if effective.width_mm and effective.height_mm else None)
    variables["unit_subtotal"] = sum(
        (c.amount for c in effective.extras if c.basis == "per_piece"), Decimal(0))
    variables["fixed_subtotal"] = sum(
        (c.amount for c in effective.extras if c.basis == "total"), Decimal(0))
    for cost in line.estimate.costs:
        variables["order_subtotal"] = (
            variables["unit_subtotal"] * effective.quantity + variables["fixed_subtotal"]
            if effective.quantity is not None else None)
        reference = None
        if cost.source == "catalog":
            price = db.get(PriceItem, cost.reference_id) if cost.reference_id else None
            if (not price or price.status == "inactive" or price.revision != cost.reference_revision
                    or price.amount is None or price.amount != cost.rate or price.unit == "unknown"):
                raise ValueError(f"{cost.label}：引用价格不存在、已变化或单价不一致，请重新核价")
            reference = {"id": price.id, "revision": price.revision, "unit": price.unit,
                         "product": db.get(Product, price.product_id).name,
                         "status": price.status, "price_kind": price.price_kind, "notes": price.notes}
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
        if cost.basis == "per_piece" and any(
                isinstance(node, ast.Name) and node.id in ("quantity", "order_subtotal", "fixed_subtotal")
                for node in ast.walk(tree)):
            raise ValueError("每件费用公式不能包含整单数量或整单小计；成品数量由程序统一相乘")
        measure = arithmetic_formula(cost.quantity_formula, variables)
        if measure < 0:
            raise ValueError("费用计量值不能为负数")
        amount = cost.rate * measure
        if amount > MAX_AMOUNT:
            raise ValueError("估算费用超出允许范围")
        amount = amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        variables["unit_subtotal" if cost.basis == "per_piece" else "fixed_subtotal"] += amount
        results.append({**cost.model_dump(mode="json"), "measure": str(measure),
                        "amount": str(amount),
                        "reference": reference})
    return results


def calculate_estimate(db, line, calculate_standard):
    base = {"line_id": line.id, "product": line.product, "requirement": line.model_dump(mode="json"),
            "estimated": True, "blockers": [], "warnings": [*line.uncertainties, "Agent补全方案待审核"],
            "amount": None, "unit_price": None, "unit_formula": "", "formula": "",
            "price": None, "fixed_charges": "0", "estimate_review": [],
            "estimate_scope": line.estimate.scope}
    try:
        effective, assumptions = effective_line(line)
        base.update(effective_requirement=effective.model_dump(mode="json"), estimate_review=assumptions)
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
    return base
