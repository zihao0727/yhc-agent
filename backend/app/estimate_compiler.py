"""Compile v3 semantic measurements into bounded formulas; never guess dimensions or rates."""
import re
from . import business_rules
from .pricing_errors import PricingError
from .requirements_schemas import EstimateCost

FORMULAS = {
    "item_area": ("m2", "=area_m2", {"item"}),
    "letter_area": ("m2", "=billable_area_m2", {"item", "letter_group"}),
    "letter_length": ("cm", "=letter_cm", {"item", "letter_group"}),
    "item_length": ("m", "=billing_length_mm/1000", {"item", "component"}),
    "component_area": ("m2", "=billable_area_m2", {"component"}),
    "component_count": ("piece", "=component_count", {"component"}),
    "item_count": ("piece", "=1", {"item"}),
    "labor_hours": ("hour", "=labor_hours", {"item", "component"}),
    "power_units": ("piece", "=power_units", {"item"}),
    "subtotal_ratio": ("ratio", "=unit_subtotal", {"item"}),
    "fixed": ("fixed", "=1", {"item"}),
    "source_quantity": ("source", "=source_quantity", {"item"}),
}


def with_proposed_objects(line):
    working = line.model_copy(deep=True)
    for kind in ("components", "letter_groups"):
        proposed = getattr(line.estimate, "proposed_" + kind)
        if not proposed:
            continue
        existing = getattr(line, kind)
        ids = [obj.object_id for obj in [*existing, *proposed]]
        if line.confirmed or len(ids) != len(set(ids)):
            raise PricingError("object_replacement_forbidden", "不能覆盖已有或人工确认对象，请按原对象编号补全",
                               [kind], cause="recognition_review", action="read_target")
        setattr(working, kind, [obj.model_copy(deep=True) for obj in [*existing, *proposed]])
    return working


def bind_target(line, item, path, required):
    if item.target == "item":
        return
    objects = getattr(line, "components" if item.target == "component" else "letter_groups")
    if not item.target_id:
        if required:
            raise PricingError("missing_target_id", "字组或部件费用及补全须引用读取结果中的object_id",
                               [f"{path}.target_id"], action="read_target")
        return
    index = next((i for i, obj in enumerate(objects) if obj.object_id == item.target_id), None)
    if index is None or item.target_index is not None and item.target_index != index:
        raise PricingError("stale_target", "对象已变化或索引不匹配，请重新读取对象，不能按旧序号套用",
                           [f"{path}.target_id"], cause="recognition_review", action="read_target")
    item.target_index = index


def compile_estimate(line, effective):
    plan = line.estimate.model_copy(deep=True)
    if plan.contract_version < 3:
        return plan
    effective = effective.model_copy(update={"measurement_version": 3})
    if plan.pricing_strategy == "auto":
        raise PricingError("missing_strategy", "须明确本方案的计价策略，不能由分类隐式决定计量",
                           ["estimate.pricing_strategy"], action="choose_strategy")
    for index, cost in enumerate(plan.costs):
        path = f"estimate.costs[{index}]"
        if business_rules.current(effective) and re.search(r"难度|[ABCabc]级.*加|复杂.*加成", cost.label):
            raise PricingError("duplicate_difficulty", "难度系数已由程序统一应用，不得另列难度加价",
                               [path], action="remove_duplicate_or_document_adjustment")
        bind_target(effective, cost, path, True)
        mode = cost.measurement
        if mode == "script":
            if cost.kind in ("power", "bracket", "profit") or cost.rate_unit in ("ratio", "fixed"):
                raise ValueError("电源、支架、利润和整项固定费用须使用现有计量规则")
            if cost.basis != "per_piece" or cost.quantity_formula != "=1":
                raise ValueError("脚本计算每件工程量，件数和金额由程序计算，不能另填公式")
            if cost.adjusts_cost_index is not None:
                raise ValueError("脚本不得绕过工艺系数或重复调整费用")
            continue
        if mode == "auto":
            raise PricingError("missing_measurement_basis", "须选择费用的计量口径，不再由模型自由编写公式",
                               [f"{path}.measurement"], action="choose_measurement")
        unit, formula, targets = FORMULAS[mode]
        if mode == "source_quantity":
            from .source_quantities import source_measurement
            source = source_measurement(effective, cost.source_measurement_index)
            unit = source.unit
            if unit == "m" and cost.rate_unit == "cm":
                unit, formula = "cm", "=source_quantity*100"
            if cost.basis != "total" or cost.kind in ("power", "bracket", "profit"):
                raise PricingError("source_quantity_scope", "原表工程量须按整行计费，不能再次乘成品数量或代替电源/支架口径",
                                   [f"{path}.basis", f"{path}.kind"], action="choose_measurement")
        if cost.target not in targets:
            raise PricingError("measurement_target_mismatch", "计量口径不适用于当前对象",
                               [f"{path}.measurement", f"{path}.target"], action="read_target")
        if cost.quantity_formula not in ("=1", formula):
            raise PricingError("custom_formula_forbidden", "新计价契约由程序生成公式，不能额外乘字数、件数或难度",
                               [f"{path}.quantity_formula"], action="use_generated_formula")
        if mode == "item_length" and cost.rate_unit == "cm":
            unit, formula = "cm", "=billing_length_mm/10"
        if mode == "letter_length" and cost.rate_unit == "m":
            unit, formula = "m", "=letter_cm/100"
        if mode == "item_count" and cost.rate_unit == "set":
            unit = "set"
        if cost.target == "component" and mode in ("item_length", "labor_hours"):
            formula += "*component_count"
        if mode == "subtotal_ratio":
            if cost.kind != "profit":
                raise PricingError("unsupported_adjustment", "比例费用只允许显式利润，不得再次施加工艺难度",
                                   [f"{path}.kind"])
            formula = "=order_subtotal" if cost.basis == "total" else "=unit_subtotal"
        if cost.rate_unit not in ("unknown", unit):
            raise PricingError("unit_mismatch", "费率单位与计量口径不一致，须核对来源后换算",
                               [f"{path}.rate_unit"], action="repair_unit_and_formula")
        if mode.startswith("letter_") and cost.target == "item" and not effective.letter_groups:
            if effective.character_count != 1:
                raise PricingError("ambiguous_letter_dimensions", "多字项目须提供字组单字尺寸，整件尺寸不能乘字数",
                    ["letter_groups"], cause="recognition_review", action="inspect_source")
        if mode in ("letter_area", "letter_length") and effective.pricing_family != "letter":
            raise PricingError("strategy_mismatch", "字类计量与产品分类不一致",
                               ["pricing_family", f"{path}.measurement"])
        if cost.basis == "total" and mode not in ("fixed", "subtotal_ratio", "source_quantity") and not (
                cost.kind == "outsourcing" and cost.minimum_area_scope == "order"):
            raise PricingError("order_quantity_scope", "计量费用须按每件生成，订购数量由程序统一应用",
                               [f"{path}.basis"])
        if cost.adjusts_cost_index is not None:
            raise PricingError("unsupported_adjustment", "比例调整须经人工审核，不得绕过程序系数",
                               [path], cause="catalog_gap", action="review_rate")
        cost.rate_unit, cost.quantity_formula = unit, formula
    if business_rules.current(effective) and effective.manual_unit_price is None:
        kinds = {c.kind for c in [*plan.costs, *effective.extras]}
        if effective.lighting == "yes" and effective.power_count == 0 and not effective.power_note:
            raise PricingError("unjustified_zero_power", "发光产品电源台数为零须有明确免计依据，不能作为缺项默认值",
                               ["power_count", "power_note"], cause="recognition_review", action="inspect_source")
        if effective.lighting == "yes" and "power" not in kinds:
            if business_rules.power_units(effective) is None:
                raise PricingError("missing_power_count", "发光项目缺少电源台数依据，不能假设已含电源",
                    ["power_count"], cause="recognition_review", action="inspect_source")
            plan.costs.append(EstimateCost(label="电源", kind="power", rate=200, rate_unit="piece",
                measurement="power_units", quantity_formula="=power_units", reason="按已选现场规则及明确电源台数计费"))
        if effective.installation_scene == "rooftop" and "bracket" not in kinds:
            if effective.pricing_family == "letter" and not effective.letter_groups and effective.character_count != 1:
                raise PricingError("ambiguous_letter_dimensions", "楼顶支架须核实字组面积", ["letter_groups"],
                                   cause="recognition_review", action="inspect_source")
            plan.costs.append(EstimateCost(label="支架制作", kind="bracket", rate=250, rate_unit="m2",
                measurement="letter_area" if effective.pricing_family == "letter" else "item_area",
                quantity_formula="=billable_area_m2" if effective.pricing_family == "letter" else "=area_m2",
                reason="按已选现场规则计取支架制作，不含现场安装"))
    if len(plan.costs) > 30:
        raise PricingError("too_many_costs", "自动补齐必要费用后超过30项，请拆分方案", ["estimate.costs"])
    return plan
