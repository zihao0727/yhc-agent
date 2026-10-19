"""Provisional completions stay separate from source facts and require explicit review."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import ast

from .models import PriceItem, PricingRule, Product
from .pricing_cases import arithmetic_formula, evaluate_case
from .requirements_schemas import RequirementLine
from . import business_rules
from .estimate_validation import validate_component_coverage, validate_measurement, validate_expected_amount
from .pricing_strategies import measurement_variables, strategy_snapshot, target_object
from .pricing_errors import PricingError
from .estimate_compiler import bind_target, compile_estimate, with_proposed_objects

MAX_AMOUNT = Decimal("999999999999.99")
FIELD_LABELS = {
    "quantity": "数量", "width_mm": "宽(mm)", "height_mm": "高(mm)",
    "thickness_mm": "板厚(mm)", "depth_mm": "壳深(mm)", "billing_length_mm": "计价长度(mm)",
    "material": "材质", "process": "工艺", "language": "文字类型", "pricing_category": "报价类别",
    "character_count": "每件字数", "area_m2": "图纸标注面积(平方米)", "power_count": "每件电源台数",
    "pricing_family": "计价分类", "count": "字组字符数", "quantity_per_item": "每件部件用量",
    "labor_hours": "工时", "geometry": "部件形状", "area_basis": "用料面积口径", "text_content": "文字内容",
}


def effective_line(line):
    line = with_proposed_objects(line)
    for index, assumption in enumerate(line.estimate.assumptions):
        bind_target(line, assumption, f"estimate.assumptions[{index}]", line.estimate.contract_version >= 3)
    values = line.model_dump()
    review = [{"kind": "proposed_object", "field": kind, "path": kind, "target": "item",
               "target_index": None, "mode": "fill", "label": "新增待审核对象", "original": None,
               "value": obj.model_dump(mode="json"), "reason": "依据原始资料整理，须人工核实对象及计量",
               "applied": True, "evidence": [e.model_dump(mode="json") for e in obj.evidence]}
              for kind in ("components", "letter_groups") for obj in getattr(line.estimate, "proposed_" + kind)]
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
                       "mode": item.mode, "basis": item.basis, "label": FIELD_LABELS[item.field],
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


def estimate_costs(db, line, effective, source_text=""):
    results = []
    totals = {
        "unit_subtotal": sum((c.amount for c in effective.extras if c.basis == "per_piece"), Decimal(0)),
        "fixed_subtotal": sum((c.amount for c in effective.extras if c.basis == "total"), Decimal(0)),
    }
    for index, cost in enumerate(line.estimate.costs):
        if (line.rate_evidence_required and not line.confirmed
                and line.manual_unit_price is None and cost.source == "agent_estimate"):
            raise PricingError("unsupported_rate",
                f"{cost.label}：当前任务要求价格依据，未经确认的暂估费率不能计入报价；请引用适用价格或补充单价依据",
                [f"estimate.costs[{index}].rate"], cause="plan_repair", action="find_basis_or_estimate")
        reference = None
        from .pricing_strategies import scoped_line
        scoped = scoped_line(effective, cost)
        if cost.source == "catalog":
            price = db.get(PriceItem, cost.reference_id) if cost.reference_id else None
            from .catalog_parts import part_rate, part_source_cell, validate_part_process
            rate = part_rate(price, cost.reference_part) if price and cost.reference_part else (
                price.amount if price else None)
            if (not price or price.status == "inactive" or price.revision != cost.reference_revision
                    or rate is None or rate != cost.rate or price.unit == "unknown"):
                raise ValueError(f"{cost.label}：引用价格不存在、已变化或单价不一致，请重新核价")
            if line.estimate.contract_version >= 2 and (
                    price.status != "active" or price.price_kind not in ("standard", "suggested")):
                raise ValueError(f"{cost.label}：引用价格尚未启用或仅为起价/参考价，请由管理员核价")
            reference = {"id": price.id, "revision": price.revision, "unit": price.unit,
                         "product": db.get(Product, price.product_id).name,
                         "status": price.status, "price_kind": price.price_kind, "notes": price.notes,
                         "included_kinds": [] if cost.reference_part else catalog_coverage(price.attributes)}
            from .catalog_search import catalog_profile
            from .process_taxonomy import process_codes
            profile = catalog_profile(price)
            if profile["reference_only"] and not cost.reference_part:
                raise PricingError("catalog_reference_only",
                    f"{cost.label}：配置参考或起价尚未核定计费依据，不能作为可计算的完整单价",
                    [f"estimate.costs[{index}].reference_id"], cause="plan_repair",
                    action="find_basis_or_estimate")
            reference.update(process_codes=(
                process_codes(str((price.attributes or {}).get(cost.reference_part.removesuffix("_price"), "")))
                if cost.reference_part and cost.reference_part != "material_price" else []
                if cost.reference_part else profile["process_codes"]),
                included_process_codes=[] if cost.reference_part else profile["included_process_codes"])
            if cost.reference_part:
                if cost.reference_part == "material_price" and any(
                        kind != "material" for kind in cost.included_kinds):
                    raise ValueError("原表材料分项不包含加工等费用，不能通过included_kinds把材料列冒充成品价")
                validate_part_process(price, cost.reference_part, scoped,
                                      source_text if cost.target == "item" else "")
                reference.update(part=cost.reference_part, source_cell=part_source_cell(price, cost.reference_part),
                                 source_record_id=price.source_record_id,
                                 source_rate=str(price.attributes[cost.reference_part]),
                                 rate_precision=6, rate_rounding="ROUND_HALF_UP")
            elif "material_price" in (price.attributes or {}):
                for part in ("process_1_price", "process_2_price"):
                    process = (price.attributes or {}).get(part.removesuffix("_price"))
                    if process and str(process).strip() != "无":
                        validate_part_process(price, part, scoped,
                                              source_text if cost.target == "item" else "")
        elif cost.source == "historical":
            rule = db.get(PricingRule, cost.reference_id) if cost.reference_id else None
            if not rule or rule.revision != cost.reference_revision:
                raise PricingError("stale_reference", f"{cost.label}：历史案例已变化或不存在",
                    [f"estimate.costs[{index}].reference_id", f"estimate.costs[{index}].reference_revision"],
                    cause="plan_repair", action="refresh_reference")
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
        script_run = None
        if cost.calculation_script:
            from .calculation_scripts import parse_script, run_script
            _, required = parse_script(cost.calculation_script)
        if line.estimate.contract_version == 1 and cost.target != "item":
            raise ValueError("字组/部件费用需使用contract_version=2")
        variables, scoped = measurement_variables(effective, cost, required)
        if cost.measurement == "source_quantity":
            from .source_quantities import source_measurement, verify_source_measurement
            verify_source_measurement(db, effective, source_measurement(effective, cost.source_measurement_index))
        variables.update(totals)
        variables["order_subtotal"] = (
            totals["unit_subtotal"] * effective.quantity + totals["fixed_subtotal"]
            if effective.quantity is not None else totals["fixed_subtotal"]
            if not any(c.basis == "per_piece" for c in line.estimate.costs) else None)
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
        if cost.calculation_script:
            script_run = run_script(cost.calculation_script, variables)
            measure = Decimal(script_run["result"])
        else:
            measure = arithmetic_formula(cost.quantity_formula, variables)
        rate_unit = validate_measurement(cost, scoped, reference, line.estimate.contract_version)
        from .quote_safety import validate_area_basis
        validate_area_basis(effective, cost.model_copy(update={"rate_unit": rate_unit}), required,
                            source_text=source_text)
        if cost.source == "catalog":
            from .quote_safety import validate_catalog_target
            validate_catalog_target(price, scoped, cost, index, source_text)
        if line.estimate.contract_version >= 2:
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
        if script_run:
            results[-1]["script_run"] = script_run
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
        line = line.model_copy(update={"estimate": compile_estimate(line, effective)})
        if line.estimate.contract_version >= 3:
            effective.measurement_version = 3
        base["compiled_plan"] = line.estimate.model_dump(mode="json")
        validation_line = effective.model_copy(update={"estimate": line.estimate})
        if line.estimate.contract_version >= 2:
            if line.estimate.pricing_strategy == "letters" and effective.pricing_family != "letter":
                raise ValueError("逐字计价策略与pricing_family不一致，请核对来源后纠正分类")
            if line.estimate.pricing_strategy == "composite" and not effective.components:
                raise ValueError("部件组合策略缺少components，请先整理实际部件，不得复用整件面积")
        business_rules.validate_plan(validation_line)
        business_rules.validate_fees(validation_line)
        business_rules.validate_extra_amounts(effective)
        base.update(effective_requirement=effective.model_dump(mode="json"), estimate_review=assumptions)
        from .manufacturing import measurement_report, validate_object_measurements
        validate_object_measurements(validation_line)
        base["measurement_report"] = measurement_report(validation_line)
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
            from .models import CustomerFile
            from .pricing_proposals import source_register
            from .quote_safety import validate_source_material, validate_manufacturing, validate_area_basis
            area_cost = next((c for c in line.estimate.costs
                              if c.target == "item" and c.rate_unit == "m2"), None)
            if area_cost:
                validate_area_basis(effective, area_cost)
            source_file = None
            for evidence in [*line.evidence, *line.quantity_evidence, *line.dimension_evidence]:
                source_file = db.get(CustomerFile, evidence.file_id)
                if source_file is not None:
                    break
            if source_file:
                from types import SimpleNamespace
                sources = list(source_register(db, SimpleNamespace(id=source_file.job_id),
                                               line.model_dump(mode="json")).values())
            else:
                sources = line.evidence
            validate_source_material(effective, sources)
            source_text = " ".join([line.material, effective.material, effective.process, effective.notes,
                                    *(e.text for e in sources if e.source == "text")])
            costs = estimate_costs(db, line, effective, source_text)
            from .manufacturing import coverage_report, validate_production
            base["production_coverage"] = coverage_report(validation_line, costs)
            validate_production(validation_line, costs)
            validate_manufacturing(validation_line, costs, source_text)
            validate_component_coverage(validation_line)
            base["estimate_costs"] = costs
            if any(cost.get("script_run") for cost in costs):
                base["warnings"].append("临时脚本工程量须审核单位换算、几何假设和适用范围；算术校验不代表商业依据已确认")
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
            engineering = [c for c in costs if c["measurement"] == "source_quantity"]
            requires_count = any(c["basis"] == "per_piece" for c in costs) or any(
                c.basis == "per_piece" for c in effective.extras)
            if effective.quantity is None and (requires_count or not engineering):
                base["blockers"].append("请由Agent补全暂定数量")
            if not base["blockers"]:
                total = unit * (effective.quantity if effective.quantity is not None else 0) + fixed
                if total > MAX_AMOUNT:
                    raise ValueError("金额超出允许范围")
                base.update(amount=str(total.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                            formula=f"{unit} × {effective.quantity} + {fixed} = {total}")
                if engineering and not requires_count:
                    from .source_quantities import source_measurement
                    sources = [source_measurement(effective, c["source_measurement_index"]) for c in engineering]
                    identities = {(m.unit, m.value, tuple((e.file_id, e.page, e.sheet, e.cell)
                                                         for e in m.evidence)) for m in sources}
                    base.update(unit_price=None, unit_formula="",
                                formula=" + ".join([*(c["amount"] for c in costs),
                                                   *(str(c.amount) for c in effective.extras)]) + f" = {total}",
                                fixed_charges=str(fixed - sum((Decimal(c["amount"]) for c in engineering), Decimal(0))))
                    if len(identities) == 1:
                        source = sources[0]
                        recurring = sum((Decimal(c["amount"]) for c in engineering), Decimal(0))
                        base.update(billing_quantity=str(source.value), billing_unit=source.unit,
                                    unit_price=str((recurring / source.value).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                                    unit_formula=" + ".join(c["resolved_formula"] for c in engineering))
    except ValueError as exc:
        base["blockers"].append(str(exc)[:1500])
        base["amount"] = None
        if isinstance(exc, PricingError):
            base["diagnostics"] = [exc.diagnostic]
    from .pricing_diagnostics import diagnostics_for
    base.setdefault("diagnostics", diagnostics_for(line, base["blockers"]))
    return base
