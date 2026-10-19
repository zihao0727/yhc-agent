"""Actionable repair contracts and user-facing status; never invent missing source data."""
from .pricing_strategies import STRATEGIES

RESOLUTIONS = ("priced", "priced_review", "unresolved", "customer_confirmation",
               "administrator_pricing", "estimate_repair", "recognition_review",
               "service_unavailable", "execution_error")
CAUSE_RESOLUTION = {"customer_input": "customer_confirmation", "catalog_gap": "administrator_pricing",
                    "plan_repair": "estimate_repair", "recognition_review": "recognition_review",
                    "service_unavailable": "service_unavailable", "execution_error": "execution_error"}
OWNERS = {"customer_input": "需求方", "catalog_gap": "报价管理员", "plan_repair": "系统核价",
          "recognition_review": "系统核图", "service_unavailable": "系统运维", "execution_error": "系统运维"}


def validation_failure(exc):
    fields, diagnoses = [], []
    for error in exc.errors(include_input=False, include_url=False)[:10]:
        path = "/" + "/".join(str(part) for part in error["loc"])
        context = error.get("ctx", {})
        message = {
            "decimal_max_places": f"小数最多保留 {context.get('decimal_places', 6)} 位",
            "decimal_max_digits": f"数值最多包含 {context.get('max_digits', 15)} 位有效数字",
            "decimal_whole_digits": "数值的整数部分超出计价范围",
            "finite_number": "数值必须是有限数字",
            "missing": "缺少必填字段",
            "extra_forbidden": "包含当前方案不支持的字段",
        }.get(error["type"], error["msg"])
        fields.append({"path": path, "message": message})
        diagnoses.append({"code": error["type"], "cause": "plan_repair",
                          "message": f"{path}：{message}", "paths": [path],
                          "action": "submit_typed_value", "owner": OWNERS["plan_repair"]})
    blockers = [diagnosis["message"] for diagnosis in diagnoses]
    return {"saved": False, "error": "；".join(blockers), "blockers": blockers,
            "fields": fields, "diagnostics": diagnoses, "technical_error": str(exc)[:3000]}


def diagnostics_for(line, blockers):
    result = []
    for message in blockers:
        paths, code, cause, action = [], "invalid_plan", "plan_repair", "repair_estimate"
        if "字类分组缺少" in message:
            fields = ["billing_length_mm"] if "计价长度" in message else ["width_mm", "height_mm"]
            paths = [f"letter_groups[{index}].{field}" for index, group in enumerate(line.letter_groups)
                     for field in fields if getattr(group, field) is None]
            code, action = "missing_group_measurement", "complete_target"
            cause = "recognition_review"
        elif ("letter_cm" in message and line.pricing_family != "letter") or "策略与pricing_family" in message:
            paths = ["pricing_family", "estimate.costs"]
            code, action = "strategy_mismatch", "correct_classification_or_formula"
        elif "每件字数" in message or "字母数" in message:
            paths, code, cause, action = ["letter_groups"], "missing_character_count", "recognition_review", "inspect_source"
        elif "公式参数" in message or "字类缺少" in message or "计价宽" in message or "计价长度" in message:
            paths = ["billing_length_mm"] if "长度" in message or "letter_cm" in message else [
                "width_mm", "height_mm", "area_m2"]
            code, action = "missing_measurement", "complete_target"
            cause = "recognition_review"
        elif "缺少components" in message:
            paths, code, cause, action = ["components"], "missing_components", "recognition_review", "inspect_source"
        elif "电源" in message:
            paths, code, action = ["power_count", "estimate.costs"], "missing_power", "complete_power"
        elif "已变化或不存在" in message or "引用价格" in message:
            paths, code, cause, action = ["estimate.costs"], "stale_reference", "plan_repair", "refresh_reference"
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
            code, cause, action = "missing_price_basis", "plan_repair", "find_basis_or_estimate"
        elif "单件数量" in message or "暂定数量" in message:
            paths = ["quantity"]
            code, action = "missing_quantity", "review_existing_evidence_or_complete"
            cause = "recognition_review"
        elif "原始来源" in message or "纠错" in message or "人工已确认" in message:
            paths = ["estimate.assumptions"]
            code, action = "invalid_correction", "verify_evidence_and_previous_value"
        elif "validation error" in message or "数值" in message:
            code, action = "invalid_value", "submit_typed_value"
        result.append({"code": code, "cause": cause, "message": message, "paths": paths, "action": action,
                       "owner": OWNERS[cause]})
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
        return CAUSE_RESOLUTION.get(issue["cause"], "estimate_repair")
    diagnoses = line.get("diagnostics", [])
    if (not line.get("requirement", {}).get("estimate")
            and not line.get("requirement", {}).get("selected_price_id")
            and any(d["code"] == "missing_price_basis" for d in diagnoses)):
        return "unresolved"
    if diagnoses and all(d["cause"] == "catalog_gap" for d in diagnoses):
        return "administrator_pricing"
    if diagnoses:
        return CAUSE_RESOLUTION.get(diagnoses[0]["cause"], "estimate_repair")
    return "unresolved"
