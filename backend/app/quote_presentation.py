"""Enrich legacy stalled-item display from its audit, without modifying saved quotations."""
from copy import deepcopy
from decimal import Decimal
import re
import time

from .pricing_diagnostics import OWNERS, RESOLUTIONS, diagnostics_for, line_resolution
from .requirements_schemas import RequirementLine

DISPLAY_FIELDS = ("source_item", "product", "quantity", "quantity_unit", "source_measurements", "width_mm", "height_mm", "depth_mm",
                  "thickness_mm", "material", "process", "billing_length_mm", "area_m2",
                  "power_count", "character_count", "labor_hours", "pricing_family")
INPUT_FIELDS = set(DISPLAY_FIELDS) - {"source_item", "product", "pricing_family", "quantity_unit", "source_measurements"}
INPUT_FIELDS.update(("count", "quantity_per_item", "text_content"))


def sql_progress(db, job):
    from .quote_engine import calculate_requirements
    from .source_reconciliation import source_review
    lines = calculate_requirements(db, job.requirements, automatic=True)
    review = source_review(getattr(job, "extraction", {}) or {})
    return {"revision": job.revision, "line_count": len(lines),
            "priced_count": sum(line["amount"] is not None for line in lines),
            "known_subtotal": str(sum((Decimal(line["amount"]) for line in lines if line["amount"] is not None), Decimal(0))),
            "complete": bool(lines) and not review["required"]
                and all(line["amount"] is not None and not line["blockers"] for line in lines),
            "saved_at": time.time(), "lines": lines}


def compact_progress(progress):
    """Poll saved calculations without repeating uploads or source evidence for every fee."""
    result = {key: value for key, value in progress.items() if key != "lines"}
    rows = []
    for line in progress.get("lines", []):
        row = {key: value for key, value in line.items()
               if key not in ("requirement", "effective_requirement", "component_analysis", "case_references")}
        effective = line.get("effective_requirement")
        if effective:
            row["effective_requirement"] = {key: effective.get(key) for key in DISPLAY_FIELDS}
            for kind in ("components", "letter_groups"):
                row["effective_requirement"][kind] = [
                    {key: value for key, value in obj.items() if key not in ("evidence", "uncertainties")}
                    for obj in effective.get(kind, [])]
        if "estimate_review" in row:
            row["estimate_review"] = [{key: value for key, value in item.items() if key != "evidence"}
                                      for item in row["estimate_review"]]
        rows.append(row)
    result["lines"] = rows
    return result


def pending_inputs(raw, state):
    """Expose business fields and object names instead of model-facing JSON pointers."""
    detail = state.get("diagnostics") or {}
    plan = (state.get("pricing_proposal") or {}).get("plan") or {}
    paths = [path for diagnosis in detail.get("diagnostics", [])
             for path in diagnosis.get("engine_paths", diagnosis.get("paths", []))]
    paths.extend(field.get("engine_path", field.get("path", ""))
                 for field in detail.get("repair_contract", {}).get("fields", []))
    paths.extend(state.get("missing_fields") or [])
    legacy = state.get("failed_estimate") or raw.get("estimate") or {}
    objects, original_ids = {}, {}
    for kind, target in (("components", "component"), ("letter_groups", "letter_group")):
        values = {obj["object_id"]: obj for obj in raw.get(kind, []) if obj.get("object_id")}
        original_ids[kind] = set(values)
        for obj in legacy.get("proposed_" + kind, []):
            if obj.get("object_id"):
                values.setdefault(obj["object_id"], obj)
        for obj in plan.get("objects", []):
            if obj.get("kind") == target:
                values.setdefault(obj["id"], {"object_id": obj["id"], "name": obj["name"],
                                             "label": obj["name"], "count": obj.get("count")})
        objects[kind] = list(values.values())
    inputs = {}
    for path in paths:
        target, identity, name, original, field = "item", None, raw["product"], raw, path
        match = re.fullmatch(r"(components|letter_groups)\[(\d+)\]\.(\w+)", path)
        if match:
            kind, index, field = match[1], int(match[2]), match[3]
            if index >= len(objects[kind]):
                continue
            original = objects[kind][index]
            target = "component" if kind == "components" else "letter_group"
            identity = original.get("object_id")
            name = original.get("name", original.get("label", "")) or "待确认部件"
        elif path.startswith("/parameters/"):
            match = re.fullmatch(r"/parameters/(\d+)/values/(\w+)", path)
            if not match or int(match[1]) >= len(plan.get("parameters", [])):
                continue
            block, field = plan["parameters"][int(match[1])], match[2]
            identity = block.get("target", "item")
            if identity == "item":
                identity = None
            else:
                found = next(((kind, obj) for kind, values in objects.items() for obj in values
                              if obj.get("object_id") == identity), None)
                if found is None:
                    continue
                kind, original = found
                target = "component" if kind == "components" else "letter_group"
                name = original.get("name", original.get("label", ""))
        if field not in INPUT_FIELDS:
            continue
        # Include known companion dimensions for review, without marking them as missing.
        companions = [field]
        if target != "item":
            companions.extend(("width_mm", "height_mm", "quantity_per_item" if target == "component" else "count"))
        for companion in dict.fromkeys(companions):
            value = original.get(companion)
            basis = "original" if target == "item" or identity in original_ids[kind] else "assumption"
            if value is None or value == "":
                for block in plan.get("parameters", []):
                    if block.get("target", "item") == (identity or "item") and companion in block.get("values", {}):
                        value, basis = block["values"][companion], block.get("basis", "assumption")
                for assumption in legacy.get("assumptions", []):
                    if (value in (None, "") and assumption.get("field") == companion
                            and assumption.get("target", "item") == target
                            and (target == "item" or assumption.get("target_id") == identity
                                 or not assumption.get("target_id")
                                 and assumption.get("target_index") == objects[kind].index(original))):
                        value, basis = assumption.get("value"), "assumption"
            key = f"{target}:{identity or 'item'}:{companion}"
            previous = inputs.get(key)
            inputs[key] = {"key": key, "line_id": raw["id"], "target": target,
                           "target_id": identity, "target_name": name, "field": companion,
                           "value": None if value is None else str(value), "basis": basis,
                           "missing": companion == field or bool(previous and previous["missing"])}
    return list(inputs.values())


def enrich_legacy_issues(payload, run):
    result = deepcopy(payload)
    if not run:
        return result
    changed = False
    for line in result.get("lines", []):
        issue = line.get("deferred_issue")
        if (not issue or issue.get("cause") != "execution_error"
                or not issue.get("reason", "").startswith("该项连续多轮未形成可计价方案")):
            continue
        state = run.usage.get("work_state", {}).get(line["line_id"], {})
        detail = state.get("diagnostics") or {}
        messages = detail.get("blockers") or [detail.get("error") or "尚未形成有效计价方案，需核对候选及计量对象"]
        diagnoses = diagnostics_for(RequirementLine.model_validate(line["requirement"]), messages)
        line["diagnostics"] = diagnoses
        cause = diagnoses[0]["cause"] if diagnoses else "plan_repair"
        issue.update(cause=cause, reason="；".join(messages), owner=OWNERS[cause],
                     legacy_cause="execution_error", presentation_only=True)
        issue["repair_attempts"] = sum(s["tool"] in ("complete_estimate", "submit_pricing_plan", "repair_pricing_plan")
            and s.get("arguments", {}).get("line_id") == line["line_id"] for s in run.steps)
        line["resolution"] = line_resolution(line)
        changed = True
    if not changed:
        return result
    result["resolution_counts"] = {s: sum(line.get("resolution") == s for line in result.get("lines", []))
                                   for s in RESOLUTIONS}
    from .quote_engine import confirmation_items
    result["confirmation_items"] = confirmation_items(result.get("lines", []))
    return result
