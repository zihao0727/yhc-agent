"""Shared conversation tools, validation, draft snapshots and live replies."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from . import business_rules, deepseek
from .conversation import apply_operations, propose
from .models import AgentRun, CustomerFile, QuoteDraft, now
from .quote_engine import build_quote
from .requirements_schemas import RequirementLine
from .services import log_change, snapshot


def validate_changes(db, job, answer):
    from .config import settings
    from .estimate_evidence import validate_estimate_evidence
    rows = apply_operations(job.requirements, answer.changes, answer.operations, job.business_rules_version)
    if len(rows) > settings().max_requirement_lines:
        raise ValueError("修改后项目数量超过任务上限")
    files = {file.id: file for file in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))}
    for raw in rows:
        line = RequirementLine.model_validate(raw)
        validate_estimate_evidence(line, files)
        business_rules.validate_fees(line)
    return rows


def preview_changes(db, job, answer):
    rows = validate_changes(db, job, answer)
    before = build_quote(db, job.requirements, automatic=True, extraction=job.extraction)
    after = build_quote(db, rows, automatic=True, extraction=job.extraction)
    old = {line["line_id"]: line for line in before["lines"]}
    new = {line["line_id"]: line for line in after["lines"]}
    changes = []
    for line_id in dict.fromkeys([*old, *new]):
        a, b = old.get(line_id), new.get(line_id)
        if a == b:
            continue
        previous, current = a.get("amount") if a else None, b.get("amount") if b else None
        changes.append({"line_id": line_id, "product": (b or a)["product"],
                        "before": previous, "after": current,
                        "delta": str(Decimal(current) - Decimal(previous))
                        if current is not None and previous is not None else None,
                        "removed": b is None, "added": a is None,
                        "blockers": b.get("blockers", []) if b else []})
    return rows, {**after, "comparison": changes, "previous_total": before["total"],
                  "delta": str(Decimal(after["total"]) - Decimal(before["total"]))
                  if after["total"] is not None and before["total"] is not None else None}


def collaborate(db, job, text):
    from .agent_tools import TOOL_SPECS
    from .agent_execution import execute_tool
    from .agent_context import model_result
    job_id, revision = job.id, job.revision
    quote = db.scalar(select(QuoteDraft).where(QuoteDraft.job_id == job.id)
                      .order_by(QuoteDraft.version.desc()).limit(1))
    requirements, history = deepcopy(job.requirements), deepcopy(job.messages)
    quote_data = deepcopy(quote.payload) if quote and quote.job_revision == revision else None
    run = SimpleNamespace(id=None, usage={}, steps=[])
    discovered = {(raw["selected_price_id"], raw["selected_price_revision"]) for raw in requirements
                  if raw.get("selected_price_id") and raw.get("selected_price_revision")}
    references = {(cost["source"], cost.get("reference_id"), cost.get("reference_revision"))
                  for raw in requirements for cost in (raw.get("estimate") or {}).get("costs", [])
                  if cost.get("source") in ("catalog", "historical")}
    discovered.update((identity, revision) for source, identity, revision in references if source == "catalog")
    db.commit()

    def current():
        from .requirements_api import job_for
        db.expire_all()
        latest = job_for(db, job_id)
        if latest.revision != revision:
            raise ValueError("需求已被修改，本次方案未保存，请根据新版本重试")
        return latest

    def execute(name, arguments):
        latest = current()
        args = TOOL_SPECS[name][0].model_validate(arguments)
        result = execute_tool(db, latest, run, name, args, discovered)
        if name == "evaluate_price" and result.get("price"):
            discovered.add((result["price"]["id"], result["price"]["revision"]))
        if name == "evaluate_pricing_case" and result.get("id") and result.get("revision"):
            references.add(("historical", result["id"], result["revision"]))
        db.commit()
        return model_result(name, result)

    def check_references(answer):
        for operation in answer.operations:
            for cost in operation.estimate.costs if operation.estimate else []:
                if (cost.source == "catalog" and (cost.reference_id, cost.reference_revision) not in discovered
                        or cost.source == "historical" and
                        (cost.source, cost.reference_id, cost.reference_revision) not in references):
                    raise ValueError("新价格或案例引用须先查询并核验编号与版本")

    def preview(answer):
        check_references(answer)
        _, result = preview_changes(db, current(), answer)
        db.commit()
        return {"total": result["total"], "known_subtotal": result["known_subtotal"],
                "complete": result["complete"], "comparison": result["comparison"],
                "delta": result["delta"]}

    answer, usage = propose(requirements, quote_data, history, text, execute, preview)
    check_references(answer)
    return answer, usage


def save_answer(db, job, answer, usage, text=None, running=False):
    """Caller holds the job lock. A draft edit and its quote are one transaction."""
    rows, preview = preview_changes(db, job, answer) if answer.changes or answer.operations else (None, None)
    # Persist exactly what was validated; unchanged lines retain their user inputs.
    applied = rows is not None and answer.action == "apply"
    quote_id = None
    if applied:
        before = snapshot(job)
        from .agent_work_state import preserve_inputs
        preserve_inputs(db, job)
        job.requirements = rows
        job.revision += 1
        if not running:
            job.status = "quoted" if preview["complete"] else "needs_review"
        version = (db.scalar(select(func.max(QuoteDraft.version)).where(QuoteDraft.job_id == job.id)) or 0) + 1
        preview.update(title=job.title, customer=job.customer, generated_by="agent",
                       conversation_audit=usage.get("tool_audit", []))
        quote = QuoteDraft(job_id=job.id, version=version, job_revision=job.revision,
                           status="draft" if preview["complete"] else "blocked", payload=preview,
                           terms=("对话修改后的待审核报价。" + business_rules.TERMS
                                  if job.business_rules_version == business_rules.VERSION
                                  else "对话修改后的待审核报价，费用范围和商业条款须确认。"))
        db.add(quote)
        db.flush()
        quote_id = quote.id
        log_change(db, job, "conversation_patch", "根据用户对话修改并重新计价", before)
    response_text = answer.answer
    if preview:
        changes = "\n".join(
            f"{item['product']}：{item['before'] or '未计价'} → "
            f"{'已删除' if item['removed'] else item['after'] or '待核价'}"
            for item in preview["comparison"])
        response_text += ("\n修改已保存并生成待审核版本。" if applied else "\n方案试算，原报价未修改。")
        response_text += "\n" + changes
        response_text += (f"\n整单合计：{preview['total']} 元。" if preview["complete"]
                          else f"\n已计价小计：{preview['known_subtotal']} 元，仍有项目待核价。")
    messages = [*job.messages]
    if text is not None:
        messages.append({"role": "user", "text": text, "at": now().isoformat() + "Z"})
    messages.append({"role": "assistant", "text": response_text, "at": now().isoformat() + "Z",
                     "comparison": preview["comparison"] if preview else [],
                     "proposal": {"changes": [c.model_dump(mode="json") for c in answer.changes],
                                  "operations": [op.model_dump(mode="json") for op in answer.operations]}
                     if preview and not applied else None,
                     "tool_audit": usage.get("tool_audit", [])})
    # Assign through SQL so explanations do not increment the ORM version counter.
    from sqlalchemy import update
    from .models import RequirementJob
    db.execute(update(RequirementJob).where(RequirementJob.id == job.id).values(messages=messages[-40:]))
    log_change(db, job, "conversation", f"对话协作，tokens={usage.get('total_tokens', 0)}")
    return {"applied": applied, "quote_id": quote_id, "preview": preview,
            "changes": [] if applied else [change.model_dump() for change in answer.changes],
            "operations": [] if applied else [op.model_dump(mode="json") for op in answer.operations],
            "continue_pricing": answer.action == "apply" and answer.continue_pricing}


def consume_reply(db, job, run):
    """Consume queued replies between tool rounds; in-flight results cannot overwrite edits."""
    queued = next((m for m in job.messages if m.get("pending_reply")), None)
    if not queued:
        return False
    reply_id, text = queued["reply_id"], queued["text"]
    expected_revision = job.revision
    db.commit()
    try:
        answer, usage = collaborate(db, job, text)
        from .agent_execution import locked
        job, run = locked(db, job.id, run.id)
        if job.revision != expected_revision:
            raise ValueError("需求版本已变化，请重新回复")
        from sqlalchemy import update
        from .models import RequirementJob
        db.execute(update(RequirementJob).where(RequirementJob.id == job.id).values(messages=[
            {**m, "pending_reply": False} if m.get("reply_id") == reply_id else m for m in job.messages]))
        save_answer(db, job, answer, usage, running=True)
        run.usage = {**run.usage, **{key: run.usage.get(key, 0) + usage.get(key, 0)
                                     for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                     "agent_tokens": run.usage.get("agent_tokens", 0) + usage.get("total_tokens", 0),
                     "conversation_tokens": run.usage.get("conversation_tokens", 0) + usage.get("total_tokens", 0)}
    except (ValueError, deepseek.ModelFailure) as exc:
        from .agent_execution import locked
        job, run = locked(db, job.id, run.id)
        from sqlalchemy import update
        from .models import RequirementJob
        messages = [{**m, "pending_reply": False} if m.get("reply_id") == reply_id else m
                    for m in job.messages]
        messages.append({"role": "assistant", "text": f"补充尚未应用：{str(exc)[:400]}",
                         "at": now().isoformat() + "Z"})
        db.execute(update(RequirementJob).where(RequirementJob.id == job.id).values(messages=messages[-40:]))
    db.commit()
    return True
