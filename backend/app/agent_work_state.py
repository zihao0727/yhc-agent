"""Per-line working memory is SQL-backed and separate from customer facts."""
import hashlib
import json
import time
from copy import deepcopy

from sqlalchemy import select

from .models import AgentRun


def fingerprint(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def catalog_signature(db):
    from .models import PriceItem, PricingRule
    return fingerprint({
        "prices": [list(row) for row in db.execute(
            select(PriceItem.id, PriceItem.revision, PriceItem.status).order_by(PriceItem.id))],
        "rules": [list(row) for row in db.execute(
            select(PricingRule.id, PricingRule.revision, PricingRule.status).order_by(PricingRule.id))],
    })


def bounded_plan(plan):
    if len(json.dumps(plan, ensure_ascii=False)) <= 6000:
        return plan
    summary = {
        "contract_version": plan.get("contract_version", 1), "pricing_strategy": plan.get("pricing_strategy", "auto"),
        "assumptions": [{k: item[k] for k in ("field", "value") if k in item}
                        for item in plan.get("assumptions", [])],
        "costs": [{k: item[k] for k in (
            "label", "rate", "rate_unit", "expected_amount", "measurement_note",
            "quantity_formula", "calculation_script", "basis", "source", "kind",
                "reference_id", "reference_revision", "reference_part", "outsourcing_type", "minimum_area_scope",
                "target", "target_index", "target_id", "measurement", "reason", "included_kinds", "covers_component_ids",
                "source_measurement_index",
                "adjusts_cost_index", "rate_basis_amount", "rate_basis_unit")
                  if k in item} for item in plan.get("costs", [])],
        "scope": plan.get("scope", "")[:300], "summary_only": True,
    }
    summary["assumptions"] = [{key: item[key] for key in (
        "field", "value", "target", "target_index", "target_id", "reason", "mode",
        "previous_value", "evidence") if key in item}
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
    if state.get("fingerprint") == fingerprint(raw):
        return state
    return rebase_state(state, raw)


def rebase_state(state, raw):
    """Keep editable work after a fact edit, but never reuse an old validation."""
    previous = state.get("input_snapshot")
    if not previous:
        return {}
    identity = ("id", "product", "source_item", "source_code", "document_role", "notes",
                "evidence", "quantity_evidence", "dimension_evidence", "business_rules_version")
    if any(previous.get(key) != raw.get(key) for key in identity):
        return {}
    result = {key: deepcopy(state[key]) for key in ("candidates", "pricing_proposal", "failed_estimate")
              if state.get(key)}
    if not result:
        return {}
    old_targets, new_targets = {"item": previous}, {"item": raw}
    for kind in ("components", "letter_groups"):
        for source, targets in ((previous, old_targets), (raw, new_targets)):
            for obj in [*source.get(kind, []), *(source.get("estimate") or {}).get("proposed_" + kind, [])]:
                if obj.get("object_id"):
                    targets[obj["object_id"]] = obj
    # Compare field values rather than object positions; users can reorder components.
    changed = {target: {field for field in set(old_targets.get(target, {})) | set(obj)
                        if old_targets.get(target, {}).get(field) != obj.get(field)
                        and (target in old_targets or obj.get(field) not in (None, "", "unknown", [], {}))}
               for target, obj in new_targets.items()}
    for fields in changed.values():
        if fields & {"width_mm", "height_mm", "count", "quantity_per_item"}:
            fields.update(("area_m2", "billing_length_mm"))
    proposal = result.get("pricing_proposal")
    if proposal:
        plan = proposal["plan"]
        plan["objects"] = [obj for obj in plan.get("objects", []) if obj.get("id") not in new_targets]
        for block in plan.get("parameters", []):
            target = block.get("target", "item")
            invalid = changed.get(target, set())
            block["values"] = {key: value for key, value in block.get("values", {}).items()
                               if key not in invalid}
        plan["parameters"] = [block for block in plan.get("parameters", []) if block["values"]]
        proposal.update(version=proposal.get("version", 0) + 1,
                        input_fingerprint=fingerprint(raw), requires_revalidation=True)
    failed = result.get("failed_estimate")
    if failed:
        for obj in [*failed.get("assumptions", []), *failed.get("costs", [])]:
            kind = {"component": "components", "letter_group": "letter_groups"}.get(obj.get("target"))
            if kind and not obj.get("target_id") and obj.get("target_index") is not None:
                targets = [*previous.get(kind, []), *failed.get("proposed_" + kind, [])]
                if obj["target_index"] < len(targets):
                    obj["target_id"] = targets[obj["target_index"]].get("object_id")
                    obj["target_index"] = None
        failed["assumptions"] = [obj for obj in failed.get("assumptions", [])
            if obj.get("field") not in changed.get(obj.get("target_id") or "item", set())]
        for kind in ("components", "letter_groups"):
            failed["proposed_" + kind] = [obj for obj in failed.get("proposed_" + kind, [])
                                         if obj.get("object_id") not in new_targets]
        for cost in failed.get("costs", []):
            cost["expected_amount"] = None
    result.update(fingerprint=fingerprint(raw), input_snapshot=deepcopy(raw),
                  rebased=True, requires_revalidation=True, deferred=False, decision_due=False,
                  research_calls=0, idle_rounds=0, started_at=time.time(), repair_attempts=0)
    return result


def update_state(job, run, line_id, **changes):
    raw = next((raw for raw in job.requirements if raw["id"] == line_id), None)
    if raw is None:
        return
    state = {**line_state(job, run, line_id), **changes, "fingerprint": fingerprint(raw),
             "input_snapshot": deepcopy(raw)}
    run.usage = {**run.usage, "work_state": {
        **run.usage.get("work_state", {}), line_id: state}}


def preserve_inputs(db, job):
    """Capture proven old inputs before edits, including pre-snapshot agent runs."""
    current = {raw["id"]: raw for raw in job.requirements}
    runs = db.scalars(select(AgentRun).where(AgentRun.job_id == job.id)
                      .order_by(AgentRun.id.desc()).limit(10))
    for run in runs:
        states, changed = dict(run.usage.get("work_state", {})), False
        for line_id, state in states.items():
            raw = current.get(line_id)
            if raw is not None and not state.get("input_snapshot") and state.get("fingerprint") == fingerprint(raw):
                states[line_id] = {**state, "input_snapshot": deepcopy(raw)}
                changed = True
        if changed:
            run.usage = {**run.usage, "work_state": states}


def deferred_lines(job, run):
    return [{"line_id": raw["id"], "product": raw["product"],
             **{key: state.get(key) for key in (
                 "cause", "reason", "question", "missing_fields", "diagnostic_id",
                 "repair_attempts", "repair_action")}}
            for raw in job.requirements
            if (state := line_state(job, run, raw["id"])).get("deferred")]


def context_state(job, run, line_id):
    state = line_state(job, run, line_id)
    result = {key: state[key] for key in (
        "failed_estimate", "diagnostics", "candidates", "evaluation", "route",
        "cause", "reason", "question", "missing_fields", "restored_from_run",
        "repair_attempts", "repair_action", "line_tokens", "diagnostic_id",
        "rebased", "requires_revalidation")
            if key in state}
    if not state.get("deferred"):
        for key in ("cause", "reason", "question", "missing_fields"):
            result.pop(key, None)
    if "candidates" in result:
        result["candidates"] = [{**price, "notes": price.get("notes", "")[:400]}
                                for price in result["candidates"]]
    if isinstance(result.get("diagnostics"), dict):
        result["diagnostics"] = {key: value for key, value in result["diagnostics"].items()
                                 if key != "technical_error"}
    return result


def pricing_stage(job, run, line_id):
    state = line_state(job, run, line_id)
    if state.get("validated_plan"):
        return "save"
    if state.get("failed_estimate") is not None:
        return "repair"
    if state.get("pricing_proposal"):
        return "repair"
    if any(raw["id"] == line_id and raw.get("estimate") for raw in job.requirements):
        return "repair"
    if state.get("candidates") or state.get("evaluation"):
        return "compose"
    return "research"


def remember(job, run, name, arguments, result):
    line_id = arguments.get("line_id") or run.usage.get("active_line_id")
    if not line_id:
        return
    state = line_state(job, run, line_id)
    if name in ("read_source", "inspect_source", "view_source_region") and not result.get("error"):
        location = {key: arguments[key] for key in ("file_id", "page", "region", "image_index")
                    if key in arguments}
        if location:
            checked = [*state.get("checked_sources", []), {"tool": name, **location}]
            update_state(job, run, line_id, checked_sources=list({
                fingerprint(entry): entry for entry in checked}.values())[-100:])
    if name in ("preview_estimate", "complete_estimate"):
        update_state(job, run, line_id, research_calls=0, decision_due=False)
    elif name in ("search_prices", "search_prices_semantic", "read_requirement", "read_rules",
                  "list_sources", "read_source", "evaluate_price", "analyze_components",
                  "search_pricing_cases", "evaluate_pricing_case", "inspect_source", "view_source_region", "run_calculation",
                  "list_file_workspace", "run_file_script", "run_file_adapter", "read_workspace_artifact"):
        count = state.get("research_calls", 0) + 1
        signature = research_signature(name, arguments, result)
        seen = state.get("research_signatures", [])
        novel = bool(signature and signature not in seen)
        stagnant = 0 if novel else state.get("stagnant_research_calls", 0) + 1
        changes = {"research_calls": count, "stagnant_research_calls": stagnant,
                   "research_signatures": [*seen, signature][-200:] if novel else seen,
                   "decision_due": stagnant >= 5 or (
                       name == "evaluate_price" and result.get("amount") is not None
                       and not result.get("blockers") and not result.get("error"))}
        if novel:
            changes["idle_rounds"] = 0
        update_state(job, run, line_id, **changes)
    if name in ("complete_estimate", "preview_estimate"):
        if (result.get("preview") and result.get("amount") is not None
                and not result.get("blockers") and not result.get("error")):
            update_state(job, run, line_id, preview_passed=True, idle_rounds=0,
                         validated_plan=arguments["estimate"], preview_amount=result["amount"],
                         failed_estimate=None, diagnostics=None, route="save")
            return
        if result.get("saved"):
            update_state(job, run, line_id, deferred=False, idle_rounds=0,
                         failed_estimate=None, diagnostics=None, route="estimate",
                         validated_plan=None, preview_passed=False, calls={}, repair_prompted=False)
        else:
            diagnostic = {key: result[key] for key in (
                "error", "fields", "blockers", "next_action", "repair_fields", "diagnostics", "repair_contract",
                "technical_error")
                if key in result}
            signature = fingerprint(diagnostic)
            failures = {**state.get("repair_failures", {})}
            failures[signature] = failures.get(signature, 0) + 1
            diagnoses = result.get("diagnostics", [])
            update_state(job, run, line_id,
                         validated_plan=None, preview_passed=False, route="repair",
                         failed_estimate=bounded_plan(arguments.get("estimate", {})),
                         previous_diagnostics=state.get("diagnostics", {}),
                         diagnostics=diagnostic, diagnostic_id=result.get("diagnostic_id", signature[:16]),
                         repair_failures=dict(list(failures.items())[-20:]),
                         repair_attempts=state.get("repair_attempts", 0) + 1,
                         repair_action=diagnoses[0]["action"] if diagnoses else "submit_typed_value")
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
                    "amount", "status", "price_kind", "spec", "process", "quality", "language", "source")
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
    elif name in ("complete_estimate", "submit_pricing_plan", "repair_pricing_plan"):
        signature = [(d["code"], d["paths"]) for d in result.get("diagnostics", [])]
        previous = state.get("previous_diagnostics", {}).get("diagnostics", [])
        before = {fingerprint([d["code"], d["paths"]]) for d in previous}
        after = {fingerprint(item) for item in signature}
        if not before or not after < before:
            return False
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


def research_signature(name, arguments, result):
    if result.get("error") or result.get("repeated_call"):
        return None
    if name in ("search_prices", "search_prices_semantic"):
        prices = result.get("prices", [])
        return fingerprint(sorted((p["id"], p["revision"]) for p in prices)) if prices else None
    if name in ("list_sources", "list_file_workspace"):
        return None
    if name in ("run_file_script", "run_file_adapter"):
        if result.get("execution_error") or result.get("status") in ("execution_failed", "needs_repair"):
            return None
        return fingerprint([name, result.get("script_sha256"), result.get("status")])
    if name == "read_workspace_artifact":
        return fingerprint([name, result.get("text")]) if result.get("text") else None
    if name == "read_source":
        location = {key: arguments.get(key) for key in ("file_id", "page", "offset")}
        return fingerprint([name, location, result.get("text")]) if result.get("text") else None
    meaningful = {key: value for key, value in result.items()
                  if key not in ("notice", "next_action", "images_follow", "_source_images")}
    return fingerprint([name, meaningful]) if meaningful else None


def restore_work_state(db, job, run):
    if run.usage.get("work_state_restored"):
        if not run.usage.get("preview_history_restored"):
            restore_previews(job, run, [run])
            run.usage = {**run.usage, "preview_history_restored": True}
        return
    earlier = list(db.scalars(select(AgentRun).where(
        AgentRun.job_id == job.id, AgentRun.id < run.id).order_by(AgentRun.id.desc()).limit(10)))
    current = {raw["id"]: raw for raw in job.requirements}
    restored = {}
    reference_signature = None
    # Prefer the newest compatible state. Old audits also support pre-fix runs.
    for previous in earlier:
        for line_id, state in previous.usage.get("work_state", {}).items():
            compatible = (state if line_id in current and state.get("fingerprint") == fingerprint(current[line_id])
                          else rebase_state(state, current[line_id]) if line_id in current else {})
            if line_id in current and line_id not in restored and compatible:
                keep_gap = False
                if (compatible.get("deferred") and compatible.get("cause") == "catalog_gap"
                        and compatible.get("gap_reference_signature")):
                    if reference_signature is None:
                        reference_signature = catalog_signature(db)
                    keep_gap = compatible["gap_reference_signature"] == reference_signature
                restored[line_id] = {
                    **compatible, "deferred": keep_gap, "idle_rounds": 0, "calls": {},
                    "response_failures": 0,
                    "runtime_repair_failures": 0,
                    "repair_failures": {}, "repair_attempts": 0, "line_tokens": 0, "started_at": time.time(),
                    "pricing_seconds": 0,
                    "repair_prompted": False, "plan_grace_used": False, "plan_grace_turns": 0,
                    "research_calls": 0, "decision_due": False,
                    "restored_from_run": previous.id}
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
    restore_previews(job, run, [run, *earlier])
    run.usage = {**run.usage, "preview_history_restored": True}


def restore_previews(job, run, runs):
    """Recover pre-fix previews only when their audited source facts still match."""
    current = {raw["id"]: raw for raw in job.requirements}
    seen = set()
    for previous in runs:
        for step in reversed(previous.steps):
            if step["tool"] not in ("preview_estimate", "complete_estimate"):
                continue
            line_id = step.get("arguments", {}).get("line_id")
            if line_id not in current or line_id in seen:
                continue
            seen.add(line_id)
            state = line_state(job, run, line_id)
            if state.get("validated_plan") or current[line_id].get("estimate"):
                continue
            result = step.get("result", {})
            original = result.get("requirement")
            if (step["tool"] != "preview_estimate" or not result.get("preview")
                    or result.get("error") or result.get("blockers")
                    or result.get("amount") is None or original is None):
                continue
            original = {**original, "estimate": current[line_id].get("estimate")}
            if fingerprint(original) != fingerprint(current[line_id]):
                continue
            remember(job, run, "preview_estimate", step["arguments"], result)
            update_state(job, run, line_id, deferred=False, restored_preview_from_run=previous.id)


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


def charge_line(job, run, tokens):
    line_id = run.usage.get("active_line_id")
    if not line_id:
        return
    state = line_state(job, run, line_id)
    update_state(job, run, line_id, started_at=state.get("started_at", time.time()),
                 line_tokens=state.get("line_tokens", 0) + tokens)


def exhausted_reason(job, run, line_id):
    from .config import settings
    state, config = line_state(job, run, line_id), settings()
    if config.agent_line_max_tokens > 0 and state.get("line_tokens", 0) >= config.agent_line_max_tokens:
        return "line_token_budget"
    elapsed = state.get("pricing_seconds", time.time() - state.get("started_at", time.time()))
    if elapsed >= config.agent_line_max_seconds:
        return "line_time_budget"
    if max(state.get("repair_failures", {}).values(), default=0) >= config.agent_repair_attempts:
        return "repeated_failure"
    if state.get("idle_rounds", 0) >= config.agent_stall_rounds:
        return "no_progress"
    return None


def reopen_disabled_token_deferrals(job, run):
    """Retry previously budget-deferred items once after disabling that budget."""
    from .config import settings
    if settings().agent_line_max_tokens:
        return []
    latest = {}
    for step in run.steps:
        if step.get("tool") == "defer_requirement":
            result = step.get("result", {})
            if result.get("deferred"):
                latest[result.get("line_id")] = result
    reopened = []
    for line_id, result in latest.items():
        state = line_state(job, run, line_id)
        if (result.get("pause_trigger") == "line_token_budget" and state.get("deferred")
                and state.get("reason") == result.get("reason")
                and not state.get("token_budget_reopened")):
            update_state(job, run, line_id, deferred=False, token_budget_reopened=True,
                         started_at=time.time())
            reopened.append(line_id)
    return reopened


def pause_details(job, run, line_id, trigger=None):
    state = line_state(job, run, line_id)
    detail = state.get("diagnostics", {})
    diagnoses = detail.get("diagnostics") or []
    cause = diagnoses[0].get("cause", "plan_repair") if diagnoses else "plan_repair"
    reasons = detail.get("blockers") or [detail.get("error") or "尚未提交可试算方案"]
    if trigger:
        label = {"line_token_budget": "单项模型用量达到上限",
                 "line_time_budget": "单项处理时间达到上限",
                 "no_progress": "连续操作未推进计价",
                 "repeated_failure": "相同计价校验多次未通过"}.get(trigger, trigger)
        reasons = [label, *reasons]
        # Exhaustion proves a technical stop, never the absence of customer facts or a rate.
        cause = "plan_repair" if diagnoses and trigger == "repeated_failure" else "execution_error"
        evaluation = state.get("evaluation", {})
        if evaluation.get("blockers") and not detail:
            reasons.extend("最近候选检查：" + value for value in evaluation["blockers"])
    paths = list(dict.fromkeys(path for d in diagnoses for path in d.get("paths", [])))
    return {"cause": cause, "reason": "；".join(reasons)[:1800], "missing_fields": paths[:20]}
