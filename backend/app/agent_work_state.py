"""Per-line working memory is SQL-backed and separate from customer facts."""
import hashlib
import json

from sqlalchemy import select

from .models import AgentRun


def fingerprint(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def bounded_plan(plan):
    if len(json.dumps(plan, ensure_ascii=False)) <= 6000:
        return plan
    summary = {
        "contract_version": plan.get("contract_version", 1), "pricing_strategy": plan.get("pricing_strategy", "auto"),
        "assumptions": [{k: item[k] for k in ("field", "value") if k in item}
                        for item in plan.get("assumptions", [])],
        "costs": [{k: item[k] for k in (
            "label", "rate", "rate_unit", "expected_amount", "measurement_note",
            "quantity_formula", "basis", "source", "kind",
                "reference_id", "reference_revision", "outsourcing_type", "minimum_area_scope",
                "target", "target_index", "included_kinds", "adjusts_cost_index", "rate_basis_amount", "rate_basis_unit")
                  if k in item} for item in plan.get("costs", [])],
        "scope": plan.get("scope", "")[:300], "summary_only": True,
    }
    summary["assumptions"] = [{key: item[key] for key in (
        "field", "value", "target", "target_index", "mode", "previous_value", "evidence") if key in item}
        for item in plan.get("assumptions", [])]
    for key in ("costs", "assumptions"):
        count = len(summary[key])
        while len(json.dumps(summary, ensure_ascii=False)) > 5800 and summary[key]:
            summary[key].pop()
        if len(summary[key]) < count:
            summary[f"omitted_{key}"] = count - len(summary[key])
    return summary


def line_state(job, run, line_id):
    raw = next((raw for raw in job.requirements if raw["id"] == line_id), None)
    if raw is None:
        return {}
    state = run.usage.get("work_state", {}).get(line_id, {})
    return state if state.get("fingerprint") == fingerprint(raw) else {}


def update_state(job, run, line_id, **changes):
    raw = next((raw for raw in job.requirements if raw["id"] == line_id), None)
    if raw is None:
        return
    state = {**line_state(job, run, line_id), **changes, "fingerprint": fingerprint(raw)}
    run.usage = {**run.usage, "work_state": {
        **run.usage.get("work_state", {}), line_id: state}}


def deferred_lines(job, run):
    return [{"line_id": raw["id"], "product": raw["product"],
             **{key: state.get(key) for key in (
                 "cause", "reason", "question", "missing_fields")}}
            for raw in job.requirements
            if (state := line_state(job, run, raw["id"])).get("deferred")]


def context_state(job, run, line_id):
    state = line_state(job, run, line_id)
    return {key: state[key] for key in (
        "failed_estimate", "diagnostics", "candidates", "evaluation", "route",
        "cause", "reason", "question", "missing_fields", "restored_from_run")
            if key in state}


def remember(job, run, name, arguments, result):
    line_id = arguments.get("line_id") or run.usage.get("active_line_id")
    if not line_id:
        return
    state = line_state(job, run, line_id)
    if name == "complete_estimate":
        if result.get("saved"):
            update_state(job, run, line_id, deferred=False, idle_rounds=0,
                         failed_estimate=None, diagnostics=None, route="estimate",
                         calls={}, repair_prompted=False)
        else:
            update_state(job, run, line_id,
                         failed_estimate=bounded_plan(arguments.get("estimate", {})),
                         diagnostics={key: result[key] for key in (
                             "error", "fields", "blockers", "next_action", "repair_fields", "diagnostics", "repair_contract")
                                      if key in result})
    elif name == "select_price" and result.get("selected"):
        update_state(job, run, line_id, deferred=False, idle_rounds=0,
                     route="catalog", calls={}, repair_prompted=False)
    elif name in ("search_prices", "search_prices_semantic", "evaluate_price") and not result.get("error"):
        candidates = {str(price["id"]): price for price in state.get("candidates", [])}
        prices = result.get("prices", []) if name.startswith("search_") else [result.get("price")]
        for price in prices:
            if not price:
                continue
            candidates[str(price["id"])] = {
                **{key: price[key] for key in (
                    "id", "revision", "product", "material", "thickness_mm", "unit",
                    "amount", "status", "price_kind", "spec", "process", "quality", "language")
                   if key in price},
                "notes": price.get("notes", "")[:1200],
            }
        changes = {"candidates": list(candidates.values())[-20:]}
        if name == "evaluate_price":
            changes["evaluation"] = {
                "price_id": arguments.get("price_id"),
                **{key: result[key] for key in ("blockers", "warnings", "next_action") if key in result}}
        update_state(job, run, line_id, **changes)


def record_repair_progress(job, run, name, arguments, result):
    """Permit bounded evidence/diagnostic progress, never infinite search or plan churn."""
    line_id = arguments.get("line_id") or run.usage.get("active_line_id")
    if not line_id or result.get("error") or result.get("repeated_call") or result.get("saved"):
        return False
    state = line_state(job, run, line_id)
    if state.get("repair_progress_count", 0) >= 3:
        return False
    if name in ("search_prices", "search_prices_semantic"):
        signature = sorted((price["id"], price["revision"]) for price in result.get("prices", []))
    elif name == "complete_estimate":
        signature = [(d["code"], d["paths"]) for d in result.get("diagnostics", [])]
    else:
        return False
    if not signature:
        return False
    digest = fingerprint({"tool": name, "signature": signature})
    previous = state.get("progress_signatures", [])
    if digest in previous:
        return False
    update_state(job, run, line_id, progress_signatures=[*previous, digest],
                 repair_progress_count=state.get("repair_progress_count", 0) + 1,
                 idle_rounds=0)
    return True


def restore_work_state(db, job, run):
    if run.usage.get("work_state_restored"):
        return
    earlier = list(db.scalars(select(AgentRun).where(
        AgentRun.job_id == job.id, AgentRun.id < run.id).order_by(AgentRun.id.desc()).limit(10)))
    current = {raw["id"]: raw for raw in job.requirements}
    restored = {}
    # Prefer the newest compatible state. Old audits also support pre-fix runs.
    for previous in earlier:
        for line_id, state in previous.usage.get("work_state", {}).items():
            if (line_id in current and line_id not in restored
                    and state.get("fingerprint") == fingerprint(current[line_id])):
                restored[line_id] = {
                    **state, "deferred": False, "idle_rounds": 0, "calls": {},
                    "repair_prompted": False, "restored_from_run": previous.id}
    run.usage = {**run.usage, "work_state": {
        **restored, **run.usage.get("work_state", {})}}
    for previous in reversed(earlier):
        compatible = set()
        for step in previous.steps:
            line_id = step.get("arguments", {}).get("line_id")
            if line_id not in current or line_id in restored:
                continue
            original = step.get("result", {}).get("requirement")
            if original is not None:
                # Audited trial requirements contain the attempted plan/selection.
                original = {**original, **{key: current[line_id].get(key) for key in (
                    "estimate", "selected_price_id", "selected_price_revision", "price_review_note")}}
                if fingerprint(original) != fingerprint(current[line_id]):
                    continue
                compatible.add(line_id)
            else:
                continue
            remember(job, run, step["tool"], step.get("arguments", {}), step["result"])
        active_id = previous.usage.get("active_line_id")
        if active_id in compatible and active_id not in restored:
            for step in previous.steps:
                if step["tool"] in ("search_prices", "search_prices_semantic"):
                    remember(job, run, step["tool"], {"line_id": active_id}, step["result"])
    run.usage = {**run.usage, "work_state_restored": True}


def discovered_references(job, run):
    return {(price["id"], price["revision"])
            for raw in job.requirements for price in line_state(job, run, raw["id"]).get("candidates", [])
            if "id" in price and "revision" in price}


def repeat_call(job, run, name, arguments):
    """Only identical calls against unchanged inputs count as repeated work."""
    line_id = arguments.get("line_id") or run.usage.get("active_line_id")
    if not line_id or name in ("defer_requirement", "ask_user", "finish_quote", "finish_pending_quote"):
        return 0
    state = line_state(job, run, line_id)
    key = hashlib.sha256(json.dumps([name, arguments], sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()
    calls = {**state.get("calls", {})}
    count = calls.get(key, 0) + 1
    calls[key] = count
    update_state(job, run, line_id, calls=dict(list(calls.items())[-60:]))
    return count
