"""Validate measurement contracts without deciding commercial rates."""
import ast
from decimal import Decimal
from fractions import Fraction

from . import business_rules

UNIT_LABELS = {
    "cm": "元/厘米", "m": "元/米", "m2": "元/平方米",
    "piece": "元/件", "set": "元/套", "fixed": "元/整项", "ratio": "比例",
    "hour": "元/小时",
}


def monomial(formula):
    """Normalize multiplication/division exactly, independent of today's dimensions."""
    expression = formula[1:]
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 150:
        raise ValueError("公式过于复杂")

    def visit(node):
        if isinstance(node, ast.Name):
            return Fraction(1), {node.id: 1}
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Fraction(Decimal(ast.get_source_segment(expression, node))), {}
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            scalar, names = visit(node.operand)
            return scalar * (-1 if isinstance(node.op, ast.USub) else 1), names
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Div)):
            left, names = visit(node.left)
            right, other = visit(node.right)
            sign = -1 if isinstance(node.op, ast.Div) else 1
            for name, power in other.items():
                names[name] = names.get(name, 0) + sign * power
            return (left / right if sign == -1 else left * right), {
                name: power for name, power in names.items() if power}
        raise ValueError("计量公式须使用明确工程量及单位换算；加价请单列费用")

    return visit(tree.body)


def validate_measurement(cost, line, reference, contract_version=1):
    if not business_rules.current(line) and contract_version < 2:
        return cost.rate_unit
    unit = cost.rate_unit
    if cost.source == "catalog":
        referenced_unit = reference["unit"]
        if unit != "unknown" and unit != referenced_unit:
            raise ValueError(f"{cost.label}：单价单位与报价库单位{referenced_unit}不一致")
        unit = referenced_unit
    elif cost.source == "historical":
        # Historical references are finished prices per assembled item.
        if unit not in ("unknown", "piece", "set"):
            raise ValueError(f"{cost.label}：历史成品单价须按元/件或元/套使用，不能再乘厘米或面积")
        unit = "piece" if unit == "unknown" else unit
    if unit not in UNIT_LABELS:
        raise ValueError(f"{cost.label}：缺少单价单位rate_unit，不能判断金额按件、厘米还是平方米计价")
    if cost.source == "agent_estimate" and cost.expected_amount is None:
        raise ValueError(f"{cost.label}：缺少费用校对金额expected_amount，请独立核对本项费用金额")

    expressions = {
        "cm": ["=letter_cm"] if line.pricing_family == "letter" else ["=billing_length_mm/10"],
        "m": ["=letter_cm/100"] if line.pricing_family == "letter" else ["=billing_length_mm/1000"],
        "m2": ["=billable_area_m2"] if line.pricing_family == "letter" else [
            "=area_m2", "=billable_area_m2", "=width_mm*height_mm/1000000"],
        "piece": ["=1", "=character_count"],
        "set": ["=1"],
        "fixed": ["=1"],
        "ratio": ["=unit_subtotal", "=fixed_subtotal", "=order_subtotal"],
        "hour": ["=labor_hours"],
    }[unit]
    if cost.target == "component":
        expressions = {
            "m2": ["=billable_area_m2", "=area_m2*component_count",
                   "=width_mm*height_mm/1000000*component_count"],
            "cm": ["=billing_length_mm/10*component_count"],
            "m": ["=billing_length_mm/1000*component_count"],
            "piece": ["=component_count"],
            "set": ["=component_count"],
            "hour": ["=labor_hours*component_count"],
            "fixed": ["=1"],
            "ratio": ["=unit_subtotal", "=fixed_subtotal", "=order_subtotal"],
        }[unit]
    if cost.rate_basis_amount is not None and (
            cost.rate_basis_amount != cost.rate or cost.rate_basis_unit != unit):
        raise ValueError(f"{cost.label}：单价与结构化依据金额/单位不一致，不能把按件依据改成厘米费率")
    if cost.kind == "power":
        expressions = ["=power_units"] if unit == "piece" else []
    if cost.kind == "bracket" and unit != "m2":
        expressions = []
    actual_scalar, actual_names = monomial(cost.quantity_formula)
    for expression in expressions:
        scalar, names = monomial(expression)
        if names != actual_names:
            continue
        ratio = actual_scalar / scalar
        if ratio == 1:
            return unit
        if unit in ("m2", "cm", "m") and 0 < ratio < 1:
            if not cost.measurement_note.strip():
                raise ValueError(f"{cost.label}：部分工程量须填写measurement_note说明实际部件占比")
            return unit
        if ratio > 1:
            raise ValueError(f"{cost.label}：单价单位{UNIT_LABELS[unit]}的计量公式含额外倍数，"
                             "库价已有的建议加成/双面不能重复乘；"
                             "实际多部件须按明确工程量分别列项，新增工序或差价须单列依据")
    raise ValueError(f"{cost.label}：单价单位{UNIT_LABELS[unit]}与计量公式不一致；"
                     f"请使用{' 或 '.join(expressions) or '对应单位'}，不能把整件金额再乘字长、"
                     "把尺寸档位当作最低收费面积，或把加价藏进工程量")


def validate_expected_amount(cost, amount):
    if cost.expected_amount is not None and cost.expected_amount != amount:
        raise ValueError(f"{cost.label}：费用校对金额{cost.expected_amount}与程序金额{amount}不一致，"
                         "请核对rate、rate_unit、quantity_formula及难度系数；不能直接抬高校对金额凑数")


def validate_cost_coverage(cost, index, previous, reference, measure=None):
    if cost.adjusts_cost_index is not None and cost.adjusts_cost_index >= index:
        raise ValueError(f"{cost.label}：差价只能引用此前费用，不能自引用或引用后续费用")
    included = set(cost.included_kinds)
    if reference:
        included.update(reference.get("included_kinds", []))
    for old_index, old in enumerate(previous):
        same_target = old["target"] == cost.target and old.get("target_index") == cost.target_index
        parent_coverage = old["target"] == "item" and cost.target != "item"
        new_parent_coverage = cost.target == "item" and old["target"] != "item"
        if not same_target and not parent_coverage and not new_parent_coverage:
            continue
        if cost.adjusts_cost_index == old_index:
            if len(cost.reason.strip()) < 8:
                raise ValueError(f"{cost.label}：差价须说明具体调整依据，不能用短标签绕过已含费用")
            continue
        old_included = set(old.get("included_kinds", []))
        old_included.update((old.get("reference") or {}).get("included_kinds", []))
        if ((same_target or parent_coverage) and cost.kind in old_included
                or (same_target or new_parent_coverage) and old["kind"] in included):
            raise ValueError(f"{cost.label}：该费用类别已由费用{old_index + 1}包含，"
                             "不能重复计费；真实差价须指定adjusts_cost_index并说明依据")
        same_reference = (cost.source != "agent_estimate" and old["source"] == cost.source
                          and old["reference_id"] == cost.reference_id)
        same_estimate = (cost.source == old["source"] == "agent_estimate"
                         and old["label"] == cost.label and old["kind"] == cost.kind)
        same_measure = (monomial(old["quantity_formula"]) == monomial(cost.quantity_formula)
                        or measure is not None and Decimal(old["measure"]) == measure)
        if (same_target and (same_reference or same_estimate) and same_measure
                and old["basis"] == cost.basis):
            raise ValueError(f"{cost.label}：同一对象和工程量重复列费，真实多部件须分别指定对象")


def validate_extra_coverage(cost, reference, extras):
    included = set(cost.included_kinds) | set((reference or {}).get("included_kinds", []))
    for extra in extras:
        if extra.kind in included or (
                extra.kind in {"power", "bracket"} and extra.kind == cost.kind):
            raise ValueError(f"{cost.label}：人工附加费“{extra.label}”与方案已含费用重复，"
                             "请由管理员核对并移除重复项")
