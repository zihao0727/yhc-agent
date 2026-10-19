"""Read-only shadow comparison. Never adopt or persist a migrated quotation."""
from collections import Counter
from decimal import Decimal

from .quote_engine import calculate_line
from .quote_review import review_signals
from .requirements_schemas import RequirementLine


def shadow_line(db, raw):
    line = RequirementLine.model_validate(raw)
    legacy = calculate_line(db, line, automatic=True)
    review_signals([legacy])
    candidate = line.model_copy(deep=True)
    candidate.measurement_version = 3
    if candidate.estimate and candidate.estimate.contract_version < 3:
        plan = candidate.estimate
        plan.contract_version = 3
        if plan.pricing_strategy == "auto":
            # Choosing a business strategy is not a migration operation.
            return {"line_id": line.id, "product": line.product, "status": "needs_review",
                    "old_amount": legacy["amount"], "shadow_amount": None,
                    "diagnostics": [{"code": "missing_strategy", "cause": "plan_repair",
                        "message": "旧方案未明确计价策略，需审核选择，不能自动迁移"}],
                    "review_signals": legacy.get("review_signals", [])}
        for item in [*plan.assumptions, *plan.costs]:
            if item.target != "item" and item.target_index is not None:
                objects = candidate.components if item.target == "component" else candidate.letter_groups
                if item.target_index < len(objects):
                    item.target_id = objects[item.target_index].object_id
        for cost in plan.costs:
            formulas = {"=area_m2": "item_area", "=1": "fixed" if cost.rate_unit == "fixed" else "item_count",
                        "=letter_cm": "letter_length", "=power_units": "power_units",
                        "=billing_length_mm/1000": "item_length", "=billing_length_mm/10": "item_length",
                        "=labor_hours": "labor_hours", "=component_count": "component_count"}
            mode = formulas.get(cost.quantity_formula, "auto")
            if cost.quantity_formula == "=billable_area_m2":
                mode = ("component_area" if cost.target == "component" else
                        "letter_area" if candidate.pricing_family == "letter" else "item_area")
            cost.measurement = mode
            # Only exact known formulas can be compiled; unsupported ones remain blocked.
            if mode != "auto":
                cost.quantity_formula = "=1"
    shadow = calculate_line(db, candidate, automatic=True)
    old, new = legacy["amount"], shadow["amount"]
    return {"line_id": line.id, "product": line.product,
            "status": "blocked" if new is None else "unchanged" if old == new else "changed",
            "old_amount": old, "shadow_amount": new,
            "difference": str(Decimal(new) - Decimal(old)) if new is not None and old is not None else None,
            "protected": bool(line.confirmed or line.manual_unit_price is not None),
            "diagnostics": shadow.get("diagnostics", []), "blockers": shadow.get("blockers", []),
            "review_signals": legacy.get("review_signals", [])}


def run_metrics(run):
    counts = Counter(step["tool"] for step in run.steps)
    errors = Counter(d["code"] for step in run.steps for d in step.get("result", {}).get("diagnostics", [])
                     if isinstance(d, dict) and "code" in d)
    return {"run_id": run.id, "status": run.status, "tool_calls": sum(counts.values()),
            "repeated_calls": sum(bool(s["result"].get("repeated_call")) for s in run.steps),
            "saved_items": sum(bool(s["result"].get("saved") or s["result"].get("selected")) for s in run.steps),
            "tokens": run.usage.get("total_tokens", 0), "error_counts": dict(errors),
            "deferred_items": sum(bool(s["result"].get("deferred")) for s in run.steps
                                  if s["tool"] == "defer_requirement")}
