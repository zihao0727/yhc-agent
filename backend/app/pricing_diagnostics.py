"""Actionable repair contracts and user-facing status; never invent missing source data."""
from .pricing_strategies import STRATEGIES


def diagnostics_for(line, blockers):
    result = []
    for message in blockers:
        paths, code, cause, action = [], "invalid_plan", "execution_error", "repair_estimate"
        if "字类分组缺少" in message:
            fields = ["billing_length_mm"] if "计价长度" in message else ["width_mm", "height_mm"]
            paths = [f"letter_groups[{index}].{field}" for index, group in enumerate(line.letter_groups)
                     for field in fields if getattr(group, field) is None]
            code, action = "missing_group_measurement", "complete_target"
        elif ("letter_cm" in message and line.pricing_family != "letter") or "策略与pricing_family" in message:
            paths = ["pricing_family", "estimate.costs"]
            code, action = "strategy_mismatch", "correct_classification_or_formula"
        elif "公式参数" in message or "字类缺少" in message or "计价宽" in message or "计价长度" in message:
            paths = ["billing_length_mm"] if "长度" in message or "letter_cm" in message else [
                "width_mm", "height_mm", "area_m2"]
            code, action = "missing_measurement", "complete_target"
        elif "单价单位" in message or "单价与结构化" in message:
            paths = ["estimate.costs.rate_unit", "estimate.costs.quantity_formula"]
            code, action = "unit_mismatch", "repair_unit_and_formula"
        elif "校对金额" in message:
            paths = ["estimate.costs.rate", "estimate.costs.expected_amount"]
            code, action = "amount_mismatch", "verify_rate_and_measurement"
        elif "重复" in message or "已由费用" in message:
            paths = ["estimate.costs"]
            code, action = "duplicate_coverage", "remove_duplicate_or_document_adjustment"
        elif "尚未选择价格" in message or "单价或计价单位未确认" in message:
            paths = ["selected_price_id", "estimate.costs"]
            code, cause, action = "missing_price_basis", "catalog_gap", "find_basis_or_estimate"
        elif "单件数量" in message or "暂定数量" in message:
            paths = ["quantity"]
            code, action = "missing_quantity", "review_existing_evidence_or_complete"
        elif "原始来源" in message or "纠错" in message or "人工已确认" in message:
            paths = ["estimate.assumptions"]
            code, action = "invalid_correction", "verify_evidence_and_previous_value"
        elif "validation error" in message or "数值" in message:
            code, action = "invalid_value", "submit_typed_value"
        result.append({"code": code, "cause": cause, "message": message, "paths": paths, "action": action})
    return result


def repair_contract(line, diagnostics):
    fields = []
    for diagnosis in diagnostics:
        for path in diagnosis["paths"]:
            fields.append({"path": path, "reason": diagnosis["message"], "action": diagnosis["action"]})
    evidence = [e.model_dump(mode="json") for e in line.dimension_evidence]
    return {"fields": fields, "existing_dimension_evidence": evidence,
            "allowed_strategies": [{"key": key, **value} for key, value in STRATEGIES.items()],
            "notice": "先核对已有单元格和图纸证据；字组/部件使用对象级补全；"
                      "错误识别用带来源和previous_value的correct，不把程序受阻当作客户缺资料。"}


def line_resolution(line):
    if line["amount"] is not None and not line["blockers"]:
        return "priced_review" if line.get("estimated") or line.get("warnings") else "priced"
    issue = line.get("deferred_issue")
    if issue:
        return {"customer_input": "customer_confirmation", "catalog_gap": "administrator_pricing",
                "execution_error": "execution_error"}[issue["cause"]]
    diagnoses = line.get("diagnostics", [])
    if diagnoses and all(d["cause"] == "catalog_gap" for d in diagnoses):
        return "administrator_pricing"
    return "unresolved"
