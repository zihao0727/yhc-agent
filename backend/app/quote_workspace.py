"""Canonical current calculations and actionable blockers; historical drafts stay immutable."""
from copy import deepcopy
from decimal import Decimal

from . import agent_work_state as work
from .pricing_contract import ENGINE_VERSION, WORKSPACE_VERSION, WORKFLOW_STATES
from .pricing_diagnostics import OWNERS
from .pricing_workflow import line_workflow
from .quote_presentation import compact_progress, pending_inputs
from .requirements_schemas import RequirementLine

BUSINESS_FIELDS = {
    "quantity", "width_mm", "height_mm", "depth_mm", "thickness_mm", "billing_length_mm",
    "material", "process", "text_content", "character_count", "area_m2", "power_count",
    "labor_hours", "manual_unit_price", "manual_price_note", "quantity_per_item", "count",
}


def issue_kind(cause):
    return "information" if cause == "customer_input" else "pricing" if cause == "catalog_gap" else "system"


def build_workspace(db, job, run=None, quotes=(), progress=None):
    from .quote_engine import calculate_requirements, annotate_lines
    from .source_reconciliation import source_review
    preparing = bool(job.extraction.get("incomplete") and not (run and run.usage.get("prepared")))
    if preparing:
        lines = [{"line_id": raw["id"], "product": raw["product"], "amount": None,
                  "unit_price": None, "blockers": ["资料仍在核对，尚未进入计价"],
                  "formula": "", "price": None, "requirement": raw} for raw in job.requirements]
    elif progress is not None:
        # The caller supplies a current SQL calculation, never an unversioned Redis snapshot.
        lines = deepcopy(progress["lines"])
    else:
        lines = calculate_requirements(db, job.requirements, automatic=True)
    if not preparing:
        annotate_lines(db, lines, automatic=True, include_component_analysis=False)
    originals = {raw["id"]: raw for raw in job.requirements}
    by_id = {}
    for line in lines:
        raw = originals[line["line_id"]]
        line["requirement"] = RequirementLine.model_validate(raw).model_dump(mode="json")
        line["workflow"] = line_workflow(raw, run, line, job.status == "processing" and not preparing)
        if line["workflow"]["blocker"]:
            line["deferred_issue"] = {**line["workflow"]["blocker"],
                                      "line_id": raw["id"], "product": raw["product"]}
        if preparing:
            line["workflow"].update(state="source_review", label="待系统核图", valid=False)
        by_id[line["line_id"]] = line
    resolved = {key for key, line in by_id.items() if line["workflow"]["valid"]}
    rows = {}

    def add_issue(ids, issue, state=None):
        original_ids = set(ids)
        ids = sorted(original_ids - resolved)
        if original_ids and not ids:
            return
        cause = issue.get("cause") or "plan_repair"
        question = issue.get("question") or issue.get("reason") or "请核对计价方案及已有依据"
        key = work.fingerprint([ids, cause, question])[:24]
        reason = issue.get("reason") or "；".join(dict.fromkeys(
            by_id[identity]["workflow"]["blocker"]["reason"] for identity in ids
            if by_id[identity]["workflow"].get("blocker")
            and by_id[identity]["workflow"]["blocker"].get("reason")))
        inputs = issue.get("inputs") or [
            entry for identity in ids for entry in pending_inputs(originals[identity], {
                **(state or {}), "missing_fields": issue.get("missing_fields") or [],
            })]
        inputs = list({(entry["line_id"], entry["key"]): entry for entry in inputs}.values())
        row = {"key": key, "kind": issue_kind(cause), "cause": cause,
               "owner": OWNERS.get(cause) or issue.get("owner") or "系统核查",
               "question": question, "reason": reason or "该确认事项尚未解决，当前报价未包含其影响。",
               "products": [{"line_id": identity, "product": originals[identity]["product"],
                             "source_item": originals[identity].get("source_item", "")} for identity in ids],
               "fields": sorted(set(issue.get("missing_fields") or []) & BUSINESS_FIELDS),
               "inputs": inputs}
        if key in rows:
            previous = rows[key]
            if not row["reason"]:
                row["reason"] = previous["reason"]
            row["fields"] = sorted(set(row["fields"]) | set(previous["fields"]))
            row["inputs"] = list({(entry["line_id"], entry["key"]): entry
                                 for entry in [*previous["inputs"], *row["inputs"]]}.values())
        rows[key] = row

    current = next((quote for quote in quotes
                    if quote.get("job_revision") == job.revision and not quote.get("outdated")), None)
    if not preparing:
        if run:
            for issue in work.deferred_lines(job, run):
                add_issue([issue["line_id"]], issue, work.line_state(job, run, issue["line_id"]))
        if current:
            for issue in current["payload"].get("confirmation_items", []):
                ids = [entry["line_id"] for entry in issue.get("lines", [])
                       if entry.get("line_id") in originals]
                add_issue(ids, issue)
            for question in current["payload"].get("review_questions", []):
                if any(row["question"] == question for row in rows.values()):
                    continue
                matched = sorted((raw for raw in originals.values()
                                  if question.startswith(raw["product"] + "：")
                                  or question.startswith(raw["product"] + ":")),
                                 key=lambda raw: len(raw["product"]), reverse=True)
                add_issue([matched[0]["id"]] if matched else [], {
                    "cause": "recognition_review", "question": question,
                    "reason": "原资料复核记录中的疑点尚未关闭，需要系统核对原件及项目归属。",
                })
            for line in current["payload"].get("lines", []):
                issue = line.get("deferred_issue")
                if issue and line["line_id"] in originals:
                    add_issue([line["line_id"]], issue)
        covered = {product["line_id"] for row in rows.values() for product in row["products"]}
        for identity, line in by_id.items():
            if identity in resolved or identity in covered:
                continue
            blocker = line["workflow"]["blocker"]
            add_issue([identity], blocker)
    count = len(lines)
    subtotal = sum((Decimal(line["amount"]) for line in lines if line["workflow"]["valid"]), Decimal(0))
    approved = bool(current and current.get("status") == "approved"
                    and len(resolved) == count and current["payload"].get("complete")
                    and current["payload"].get("total") is not None
                    and Decimal(current["payload"].get("unrounded_total")
                                or current["payload"]["total"]) == subtotal)
    if approved:
        rows.clear()
    review = source_review(job.extraction or {})
    complete = bool(lines) and len(resolved) == count and not rows and not preparing and not review["required"]
    # This aggregate counts workflow states, not claims that all pending items lack data.
    counts = {state: sum(line["workflow"]["state"] == state for line in lines) for state in WORKFLOW_STATES}
    return {"version": WORKSPACE_VERSION, "engine_version": ENGINE_VERSION,
            "revision": job.revision, "run_id": run.id if run else None,
            "preparing": preparing, "lines": compact_progress({"lines": lines})["lines"],
            "pending": list(rows.values()), "state_counts": counts,
            "summary": {"count": count, "priced": len(resolved), "pending": count - len(resolved),
                        "amount": current["payload"]["total"] if approved else
                            str(subtotal.quantize(Decimal(".01"))) if resolved else None,
                        "complete": complete, "outdated": False},
            "actions": {"can_revalidate": job.status != "processing" and not preparing,
                        "can_resume": job.status != "processing" and not complete and not preparing,
                        "auto_resume": bool(run and run.usage.get("auto_resume")),
                        "can_disable_auto_resume": bool(run and run.usage.get("auto_resume"))}}
