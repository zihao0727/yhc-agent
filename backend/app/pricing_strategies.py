"""Reusable measurement strategies, independent of file layout and customer names."""
from decimal import Decimal

from . import business_rules

STRATEGIES = {
    "letters": {"label": "逐字计价", "units": ["cm", "m", "m2", "piece"], "fields": ["letter_groups"]},
    "area": {"label": "面积计价", "units": ["m2"], "fields": ["area_m2", "width_mm", "height_mm"]},
    "length": {"label": "长度计价", "units": ["cm", "m"], "fields": ["billing_length_mm"]},
    "item": {"label": "件/套计价", "units": ["piece", "set", "fixed"], "fields": ["quantity"]},
    "composite": {"label": "部件组合", "units": ["cm", "m", "m2", "piece", "set", "hour"], "fields": ["components"]},
    "labor": {"label": "工序/工时", "units": ["hour", "piece", "m2", "m", "cm"], "fields": ["labor_hours"]},
}
COLLECTIONS = {"letter_group": "letter_groups", "component": "components"}


def target_object(line, target, index):
    if target == "item":
        return line
    collection = getattr(line, COLLECTIONS[target])
    if index is None or index >= len(collection):
        raise ValueError(f"{COLLECTIONS[target]}[{index}]不存在，请核对对象索引")
    return collection[index]


def scoped_line(line, cost):
    obj = target_object(line, cost.target, cost.target_index)
    if cost.target == "item":
        return line
    if cost.target == "letter_group":
        return line.model_copy(update={
            "pricing_family": "letter", "letter_groups": [obj], "character_count": obj.count,
            "width_mm": obj.width_mm, "height_mm": obj.height_mm,
            "billing_length_mm": obj.billing_length_mm, "area_m2": None,
        })
    return line.model_copy(update={
        "pricing_family": "material", "letter_groups": [], "character_count": None,
        "width_mm": obj.width_mm, "height_mm": obj.height_mm, "area_m2": obj.area_m2,
        "depth_mm": obj.depth_mm,
        "billing_length_mm": obj.billing_length_mm, "thickness_mm": obj.thickness_mm,
        "labor_hours": obj.labor_hours, "material": obj.material, "process": " ".join(obj.processes),
    })


def measurement_variables(line, cost, required):
    scoped = scoped_line(line, cost)
    from .quote_safety import validate_area_basis
    validate_area_basis(line, cost, required)
    if cost.target == "component" and required.intersection(
            {"area_m2", "billable_area_m2", "width_mm", "height_mm"}):
        component = target_object(line, "component", cost.target_index)
        if (component.geometry in {"shaped", "shell"} and component.area_m2 is None
                and not cost.measurement_note.strip()):
            raise ValueError("异形/壳体部件须填写实际计价面积area_m2或工程量依据measurement_note，"
                             "不能直接把包围宽高当作展开用料")
    values = {name: getattr(scoped, name) for name in (
        "width_mm", "height_mm", "thickness_mm", "depth_mm", "billing_length_mm", "quantity", "labor_hours")}
    values["area_m2"] = business_rules.area(scoped)
    if "source_quantity" in required:
        from .source_quantities import source_measurement
        values["source_quantity"] = source_measurement(line, cost.source_measurement_index).value
    count = (target_object(line, "component", cost.target_index).quantity_per_item
             if cost.target == "component" else 1)
    values["component_count"] = Decimal(count) if count is not None else None
    values["character_count"] = (sum(g.count for g in scoped.letter_groups)
                                if scoped.letter_groups else scoped.character_count)
    values["billable_area_m2"] = values["letter_cm"] = values["power_units"] = None
    # Missing dimensions must not block an unrelated fixed or per-piece charge.
    if "letter_cm" in required:
        values["letter_cm"] = (business_rules.letter_measure(scoped, "cm")
                               if scoped.pricing_family == "letter" else None)
    if "billable_area_m2" in required:
        measured = (business_rules.letter_measure(scoped, "m2") if scoped.pricing_family == "letter"
                    else values["area_m2"])
        values["billable_area_m2"] = measured * values["component_count"] if (
            measured is not None and values["component_count"] is not None) else None
    if "power_units" in required:
        if cost.target != "item":
            raise ValueError("电源须在整件范围逐字核算，不能重复引用部件台数")
        values["power_units"] = business_rules.power_units(line)
    if line.measurement_version >= 3:
        missing = [key for key in required if key in values and values[key] is None]
        if missing:
            from .pricing_errors import PricingError
            prefix = (f'{"components" if cost.target == "component" else "letter_groups"}[{cost.target_index}].'
                      if cost.target != "item" else "")
            fields = {"component_count": ["quantity_per_item"], "billable_area_m2": ["width_mm", "height_mm"],
                      "area_m2": ["width_mm", "height_mm"], "letter_cm": ["billing_length_mm"],
                      "power_units": ["power_count"]}
            if cost.target == "component" and values["component_count"] is None:
                fields["billable_area_m2"] = (["quantity_per_item"] if values["area_m2"] is not None else
                                             ["quantity_per_item", "width_mm", "height_mm"])
            paths = list(dict.fromkeys(prefix + field for key in missing for field in fields.get(key, [key])))
            raise PricingError("missing_target_measurement", "计量对象缺少参数：" + "、".join(missing),
                               paths, cause="recognition_review", action="inspect_source")
    return values, scoped


def strategy_snapshot(line):
    explicit = line.estimate.pricing_strategy if line.estimate else "auto"
    if explicit != "auto":
        key = explicit
    elif line.components:
        key = "composite"
    elif line.pricing_family == "letter":
        key = "letters"
    elif line.labor_hours is not None:
        key = "labor"
    elif line.billing_length_mm is not None:
        key = "length"
    elif business_rules.area(line) is not None:
        key = "area"
    else:
        key = "item"
    return {"key": key, **STRATEGIES[key], "explicit": explicit != "auto",
            "version": "measurement.v2"}
