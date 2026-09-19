"""Historical formulas are auditable references, not automatically approved prices."""
import ast
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import operator

from sqlalchemy import select

from .models import PricingRule, SourceDocument, SourceRecord

CASE_KIND = "manual_quote_v1"
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def arithmetic_formula(formula, inputs=None):
    """Evaluate bounded arithmetic only. Blank Excel references are missing, not zero."""
    if not isinstance(formula, str) or not formula.startswith("=") or len(formula) > 1000:
        raise ValueError("公式必须为有限长度的算术表达式")
    expression = formula[1:]
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("公式不符合算术语法") from exc
    if len(list(ast.walk(tree))) > 150:
        raise ValueError("公式过于复杂")
    inputs = inputs or {}

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = Decimal(ast.get_source_segment(expression, node))
        elif isinstance(node, ast.Name) and node.id in inputs:
            if inputs[node.id] is None or isinstance(inputs[node.id], bool):
                raise ValueError(f"缺少公式参数：{node.id}")
            value = Decimal(str(inputs[node.id]))
        elif isinstance(node, ast.BinOp) and type(node.op) in OPS:
            value = OPS[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("仅支持数字、已定义参数及加减乘除，不执行函数或外部引用")
        if not value.is_finite() or abs(value) > Decimal("999999999999.99"):
            raise ValueError("公式结果超出允许范围")
        return value
    try:
        return visit(tree.body)
    except (ArithmeticError, InvalidOperation, TypeError) as exc:
        raise ValueError("公式包含无效数字或除零") from exc


def case_payload(rule):
    if rule.rule_type != "case" or rule.status == "inactive":
        return None
    try:
        data = json.loads(rule.content)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("kind") != CASE_KIND:
        return None
    if not all(isinstance(data.get(k), str) for k in ("product", "source_item", "formula", "tax_rate")):
        return None
    if type(data.get("quantity")) is not int or data["quantity"] <= 0:
        return None
    if not isinstance(data.get("inputs", {}), dict):
        return None
    if not isinstance(data.get("issues", []), list) or not all(isinstance(x, str) for x in data.get("issues", [])):
        return None
    return data


def evaluate_case(db, rule):
    data = case_payload(rule)
    if not data:
        return {"error": "不是可复算的人工报价案例"}
    issues = list(data.get("issues", []))
    unit_price = None
    try:
        unit_price = arithmetic_formula(data["formula"], data.get("inputs", {}))
        if unit_price <= 0:
            issues.append("非正价须确认是否漏报、赠送或包含在其他项目")
            unit_price = None
    except ValueError as exc:
        issues.append(str(exc))
    record = db.get(SourceRecord, rule.source_record_id) if rule.source_record_id else None
    doc = db.get(SourceDocument, record.document_id) if record else None
    return {
        "id": rule.id, "revision": rule.revision, "product": data["product"],
        "source_item": data["source_item"], "formula": data["formula"],
        "historical_quantity": data["quantity"], "historical_tax_rate": data["tax_rate"],
        "historical_unit_price": str(unit_price.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) if unit_price is not None else None,
        "historical_taxed_amount": data.get("cached_taxed_amount"),
        "notes": data.get("notes", ""), "dimensions": data.get("dimensions", ""),
        "issues": list(dict.fromkeys(issues)), "reference_only": True,
        "source": {"filename": doc.filename, "sheet": record.sheet, "cell": rule.source_cell} if doc else None,
        "notice": "仅复算原项目公式。原参数、数量、工艺变更及税费口径未获确认前，不得套用于当前需求或计入报价。",
    }


def search_cases(db, query="", offset=0):
    stmt = select(PricingRule).where(PricingRule.rule_type == "case", PricingRule.status != "inactive")
    if query.strip():
        stmt = stmt.where(PricingRule.content.icontains(query.strip(), autoescape=True))
    rules = [rule for rule in db.scalars(stmt.order_by(PricingRule.id)) if case_payload(rule)]
    return {"total": len(rules), "next_offset": offset + 10 if offset + 10 < len(rules) else None,
            "cases": [evaluate_case(db, r) for r in rules[offset:offset + 10]]}


def case_references(db, line):
    if not line.product or not line.source_item:
        return []
    stmt = select(PricingRule).where(PricingRule.rule_type == "case", PricingRule.status != "inactive",
                                    PricingRule.content.icontains(line.product, autoescape=True)).order_by(PricingRule.id)
    # Names are retrieval hints only; they never constitute approval to reuse a case.
    results = []
    for rule in db.scalars(stmt):
        data = case_payload(rule)
        if data and data["source_item"] == line.source_item and line.product in data["product"]:
            results.append(evaluate_case(db, rule))
            if len(results) == 3:
                break
    return results
