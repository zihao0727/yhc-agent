"""Durable line lifecycle; only changed dependencies can reopen exhausted work."""
from copy import deepcopy
import time

from sqlalchemy import select

from . import agent_work_state as work
from .models import AgentRun, PricingWorkItem, QuoteDraft, RequirementJob
from .pricing_contract import ENGINE_VERSION, STATE_LABELS
from .pricing_diagnostics import OWNERS
from .requirements_schemas import RequirementLine


def status_for(result, state, processing=False):
    if result.get("amount") is not None and not result.get("blockers"):
        return "priced_review" if result.get("estimated") or result.get("warnings") else "priced"
    if state.get("deferred"):
        return {"customer_input": "customer_input", "catalog_gap": "administrator_pricing",
                "recognition_review": "source_review"}.get(state.get("cause"), "system_error")
    if processing:
        return "processing"
    diagnoses = result.get("diagnostics") or []
    if any(d.get("cause") in ("execution_error", "service_unavailable", "plan_repair") for d in diagnoses):
        # An unselected candidate is still pending work, rather than an engine defect.
        if all(d.get("code") == "missing_price_basis" for d in diagnoses):
            return "pending"
        return "system_error"
    return "source_review" if diagnoses and any(
        d.get("cause") == "recognition_review" for d in diagnoses) else "pending"


def blocker_for(raw, state, result):
    detail = state.get("diagnostics") or {}
    diagnoses = detail.get("diagnostics") or result.get("diagnostics") or []
    cause = state.get("cause") if state.get("deferred") else (
        diagnoses[0].get("cause") if diagnoses else "plan_repair")
    from .quote_presentation import pending_inputs
    return {
        "cause": cause, "owner": OWNERS.get(cause, "系统核查"),
        "reason": state.get("reason") if state.get("deferred") else "；".join(result.get("blockers") or []),
        "question": state.get("question", "") if state.get("deferred") else "",
        "missing_fields": state.get("missing_fields") or list(dict.fromkeys(
            path for diagnosis in diagnoses for path in diagnosis.get("engine_paths", diagnosis.get("paths", [])))),
        "diagnostic_id": state.get("diagnostic_id"), "repair_action": state.get("repair_action"),
        "inputs": pending_inputs(raw, state if state.get("deferred") else {
            **state, "diagnostics": {"diagnostics": diagnoses}}),
        "checked_sources": deepcopy(state.get("checked_sources") or []),
        "catalog_assessment": deepcopy(state.get("catalog_assessment") or {}),
    }


def dependencies_for(db, raw, cause=None):
    from .catalog_search import search_catalog, search_rules
    matches = search_catalog(db, raw=raw).hits
    references = [(hit.price.id, hit.price.revision, hit.price.status) for hit in matches]
    rules = search_rules(db, " ".join(raw.get(key, "") or ""
                         for key in ("product", "material", "process", "notes")), limit=10000)[1]
    return {"facts": work.fingerprint(raw), "catalog": work.fingerprint(references),
            "rules": work.fingerprint([(r.id, r.revision, r.status) for r in rules]),
            "engine": ENGINE_VERSION, "cause": cause}


def sync_work_items(db, job, run, lines=None):
    from .quote_engine import calculate_requirements
    if run.usage.get("extraction_input") and not run.usage.get("prepared"):
        return
    lines = lines if lines is not None else calculate_requirements(db, job.requirements, automatic=True)
    rows = {item.line_id: item for item in db.scalars(select(PricingWorkItem).where(
        PricingWorkItem.job_id == job.id))}
    raw_by_id = {raw["id"]: raw for raw in job.requirements}
    for result in lines:
        raw = raw_by_id[result["line_id"]]
        state = work.line_state(job, run, raw["id"])
        status = status_for(result, state, run.status == "processing")
        blocker = blocker_for(raw, state, result) if result.get("amount") is None else {}
        item = rows.pop(raw["id"], None)
        dependencies = dependencies_for(db, raw, blocker.get("cause"))
        if item is None:
            item = PricingWorkItem(job_id=job.id, line_id=raw["id"], recovery={})
            db.add(item)
        item.status, item.stage = status, work.pricing_stage(job, run, raw["id"])
        item.engine_version, item.input_fingerprint = ENGINE_VERSION, work.fingerprint(raw)
        item.blocker, item.dependencies = blocker, dependencies
    for obsolete in rows.values():
        db.delete(obsolete)
    db.flush()


def changed_dependency(item, current):
    old = item.dependencies or {}
    if current["facts"] != old.get("facts"):
        return "facts_changed"
    if current["engine"] != old.get("engine"):
        return "engine_changed"
    if item.blocker.get("cause") in ("catalog_gap", "plan_repair") and any(
            current[key] != old.get(key) for key in ("catalog", "rules")):
        return "catalog_changed"
    return None


def queue_changed_job(db, job, *, force_line_ids=None, reason="依赖已更新，自动恢复受影响项目"):
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id).order_by(AgentRun.id.desc()).limit(1))
    if (not run or run.status != "waiting" or not run.usage.get("auto_resume")
            or not run.usage.get("external_processing_authorized") or job.status == "processing"
            or job.extraction.get("incomplete")):
        return []
    latest_quote = db.scalar(select(QuoteDraft).where(QuoteDraft.job_id == job.id)
                             .order_by(QuoteDraft.version.desc()).limit(1))
    if latest_quote and latest_quote.status == "approved":
        return []
    from .quote_engine import calculate_line
    items = {item.line_id: item for item in db.scalars(select(PricingWorkItem).where(
        PricingWorkItem.job_id == job.id))}
    reopened, audit = [], []
    for raw in job.requirements:
        if force_line_ids is not None and raw["id"] not in force_line_ids:
            continue
        item = items.get(raw["id"])
        if item is None:
            continue
        current = dependencies_for(db, raw, item.blocker.get("cause"))
        trigger = "administrator_retry" if force_line_ids is not None else changed_dependency(item, current)
        if not trigger:
            continue
        signature = work.fingerprint(current)
        ledger = dict((item.recovery or {}).get("attempts", {}))
        if ledger.get(signature, 0) >= 1:
            continue
        result = calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
        ledger[signature] = ledger.get(signature, 0) + 1
        item.recovery = {**(item.recovery or {}), "attempts": dict(list(ledger.items())[-30:]),
                         "trigger": trigger, "at": time.time()}
        if result["amount"] is not None and not result["blockers"]:
            # A source edit may make a saved plan valid without another model call.
            work.update_state(job, run, raw["id"], deferred=False)
        else:
            state = work.line_state(job, run, raw["id"])
            work.update_state(job, run, raw["id"], deferred=False, idle_rounds=0, calls={},
                repair_failures={}, repair_attempts=0, repair_prompted=False,
                pricing_seconds=0, started_at=time.time(), response_failures=0,
                runtime_repair_failures=0, source_view_pending=state.get("source_view_pending"),
                requires_revalidation=True, dependency_recovery=trigger)
        item.dependencies, item.engine_version = current, ENGINE_VERSION
        item.status = "pending"
        reopened.append(raw["id"])
        audit.append({"line_id": raw["id"], "trigger": trigger, "signature": signature,
                      "model_required": result["amount"] is None})
    if not reopened:
        return []
    from .services import log_change
    run.status, run.finished_at, run.message = "processing", None, reason
    run.usage = {**run.usage, "durable": True, "prepared": True, "phase": "queued",
                 "retry_at": 0, "no_progress_rounds": 0,
                 "dependency_recovery": audit, "request": {"intent": "resume", "replace": False,
                                                           "supplementary": ""},
                 "last_event_at": time.time()}
    job.status = "processing"
    log_change(db, job, "dependency_recovery", reason + "：" + "、".join(reopened))
    return reopened


def reconcile_waiting_jobs(db, limit=25):
    # Only jobs with an explicit opt-in are eligible; cancelled and approved work stays stopped.
    ids = list(db.scalars(select(RequirementJob.id).where(
        RequirementJob.status != "processing",
        select(AgentRun.id).where(AgentRun.job_id == RequirementJob.id,
                                 AgentRun.status == "waiting").exists())
        .order_by(RequirementJob.updated_at.desc()).limit(limit)))
    result = []
    for job_id in ids:
        job = db.scalar(select(RequirementJob).where(RequirementJob.id == job_id)
                        .with_for_update(skip_locked=True))
        if job is not None:
            reopened = queue_changed_job(db, job)
            if reopened:
                result.append({"job_id": job_id, "line_ids": reopened})
    return result


def line_workflow(raw, run, result, processing=False):
    state = work.line_state(type("Job", (), {"requirements": [raw]})(), run, raw["id"]) if run else {}
    status = status_for(result, state, processing)
    return {"state": status, "label": STATE_LABELS[status],
            "owner": "" if result.get("amount") is not None else blocker_for(raw, state, result)["owner"],
            "engine_version": ENGINE_VERSION, "input_fingerprint": work.fingerprint(raw),
            "valid": result.get("amount") is not None and not result.get("blockers"),
            "blocker": blocker_for(raw, state, result) if result.get("amount") is None else None}
