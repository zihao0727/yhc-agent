"""Object-scoped measurements and process coverage, independent of monetary calculation."""
from .pricing_errors import PricingError
from .process_taxonomy import PROCESS_LABELS, process_codes, process_matches

VERSION = "manufacturing.v1"


def measurement_report(line):
    assumptions = line.estimate.assumptions if line.estimate else []
    rows = []
    for target, objects in (("item", [line]), ("component", line.components),
                            ("letter_group", line.letter_groups)):
        for index, obj in enumerate(objects):
            identity = "item" if target == "item" else obj.object_id
            facts = {}
            for field in ("quantity", "width_mm", "height_mm", "depth_mm", "thickness_mm",
                          "area_m2", "billing_length_mm", "quantity_per_item", "count", "material"):
                if not hasattr(obj, field):
                    continue
                value = getattr(obj, field)
                adopted = next((a for a in assumptions if a.target == target and a.field == field
                                and (target == "item" or a.target_id == identity
                                     or a.target_id is None and a.target_index == index)), None)
                dimensions = [e for e in line.dimension_evidence if e.field == field
                              and (target == "item" and e.target == "item"
                                   or target != "item" and e.target_id == identity)]
                facts[field] = {
                    "value": str(value) if value is not None else None,
                    "basis": adopted.basis if adopted else "confirmed" if line.confirmed else
                        "source" if dimensions or getattr(obj, "evidence", []) else "unverified",
                    "dimension_roles": sorted({e.role for e in dimensions}),
                    "sources": [e.model_dump(mode="json") for e in
                                (adopted.evidence if adopted else dimensions or getattr(obj, "evidence", []))],
                }
            rows.append({"target": target, "target_id": identity,
                         "name": getattr(obj, "name", getattr(obj, "label", line.product)),
                         "area_basis": getattr(obj, "area_basis", "unknown"), "facts": facts})
    return {"version": VERSION, "objects": rows,
            "notice": "外轮廓、局部尺寸、板厚和实际用料面积分别绑定；未核实值不自动成为来源事实。"}


def production_requirements(line):
    rows = {(req.target_id or "item", req.process_code): {
        "target_id": req.target_id or "item", "process_code": req.process_code,
        "label": PROCESS_LABELS[req.process_code], "explicit": True,
        "evidence": [e.model_dump(mode="json") for e in req.evidence],
    } for req in line.production_requirements}
    for component in line.components:
        for code in process_codes(";".join(component.processes)):
            rows.setdefault((component.object_id, code), {
                "target_id": component.object_id, "process_code": code, "label": PROCESS_LABELS[code],
                "explicit": True, "evidence": [e.model_dump(mode="json") for e in component.evidence],
            })
    return list(rows.values())


def cost_processes(cost):
    reference = cost.get("reference") or {}
    if cost.get("source") == "catalog":
        return set(reference.get("process_codes", [])) | set(reference.get("included_process_codes", []))
    return set(cost.get("process_codes", [])) | set(process_codes(cost.get("label", "")))


def coverage_report(line, costs):
    rows = []
    for requirement in production_requirements(line):
        matches = []
        for index, cost in enumerate(costs):
            target = cost.get("target_id") or "item"
            targets = {target, *cost.get("covers_component_ids", [])}
            if requirement["target_id"] not in targets:
                continue
            if cost.get("kind") in ("power", "material", "profit", "tax"):
                continue
            if any(process_matches(requirement["process_code"], code) for code in cost_processes(cost)):
                matches.append(index)
        rows.append({**requirement, "cost_indices": matches, "covered": bool(matches)})
    return {"version": VERSION, "requirements": rows, "complete": all(row["covered"] for row in rows)}


def validate_production(line, costs):
    report = coverage_report(line, costs)
    for index, cost in enumerate(costs):
        if cost.get("source") != "catalog":
            continue
        declared, supported = set(cost.get("process_codes", [])), cost_processes(cost)
        if not all(any(process_matches(code, actual) for actual in supported) for code in declared):
            raise PricingError("unsupported_process_coverage",
                f'{cost["label"]}声明的工序超出原表分项或已核定成品范围',
                [f"estimate.costs[{index}].process_codes"], action="repair_estimate")
    missing = [row for row in report["requirements"] if not row["covered"]]
    if missing:
        names = "、".join(f'{row["target_id"]}：{row["label"]}' for row in missing)
        raise PricingError("uncovered_production_requirements",
            "这些部件工序尚未由适用费用覆盖：" + names,
            ["production_requirements", "estimate.costs"], action="complete_component_costs")
    return report


def validate_object_measurements(line):
    for index, component in enumerate(line.components):
        if component.area_basis == "developed" and component.area_m2 is None:
            raise PricingError("missing_developed_area", f"{component.name}缺少展开用料面积",
                [f"components[{index}].area_m2"], cause="recognition_review", action="inspect_source")
        if component.area_basis == "rectangle" and component.geometry != "flat_rectangle":
            raise PricingError("area_geometry_mismatch", f"{component.name}不是明确的矩形平板，不能使用矩形用料口径",
                [f"components[{index}].area_basis", f"components[{index}].geometry"],
                action="repair_estimate")
    if not line.rate_evidence_required or line.confirmed:
        return
    for field in ("width_mm", "height_mm", "depth_mm"):
        value = getattr(line, field)
        evidence = [e for e in line.dimension_evidence if e.target == "item"
                    and e.field == field and e.value == value and value is not None]
        if evidence and all(e.role in ("local", "spacing", "character", "plate_thickness") for e in evidence):
            raise PricingError("dimension_role_mismatch", f"整件{field}仅有局部或间距标注，不能直接用于整件计费",
                [field, "dimension_evidence"], cause="recognition_review", action="inspect_source")
