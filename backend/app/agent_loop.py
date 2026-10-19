"""Model-directed quoting; the server executes arithmetic and records reviewable drafts."""
import json
import time
import uuid
import logging
import traceback
from copy import deepcopy

from pydantic import ValidationError
from sqlalchemy import select

from . import deepseek
from . import semantic_search
from . import agent_work_state as work
from .agent_requests import resolve_agent_intent
from .config import settings
from .extraction_checkpoints import ExtractionYield
from .models import AgentRun, CustomerFile
from .requirements_schemas import RequirementLine
from .parallel_pricing import PricingRequests, PricingYield, account_usage, input_fingerprint
from .agent_context_budget import context_chars
# Split modules; names are re-exported here for existing callers and tests.
from .agent_tools import TOOLS, TOOL_SPECS, DeferInput, AskInput, PendingInput, tools_for_stage  # noqa: F401
from .agent_context import (SYSTEM_PROMPT as SYSTEM, resilient_context_messages as context_messages,
                            discovered_prices, model_result)  # noqa: F401
from .agent_execution import (  # noqa: F401
    Stopped, locked, event, finish, pending_quote, defer_requirement, finish_deferred_quote,
    execute_tool, commit_preview)

MAX_ROUNDS = 20
MAX_TOOLS = 60
MAX_SECONDS = 600
MAX_TOKENS = 100000
MAX_SEGMENTS = 8
MAX_TOTAL_SECONDS = 1800
MAX_CONTEXT_CHARS = 60000
MAX_RESPONSE_FAILURES = 3
MAX_TOTAL_RESPONSE_FAILURES = 6
logger = logging.getLogger(__name__)


def run_agent(db, job_id, run_id, supplementary="", replace=False, intent="auto", interrupted=None):
    started = time.monotonic()
    phase = "prepare"
    extraction_usage_before = {}
    parallel = None

    def refresh_context(job, run):
        # Concurrent requests build their own current-item context just before dispatch.
        return [] if parallel else context_messages(job, run, db)

    try:
        job, run = locked(db, job_id, run_id)
        needs_extract = resolve_agent_intent(bool(job.requirements), supplementary, replace, intent) == "extract"
        if run.usage.get("durable") and run.usage.get("prepared"):
            needs_extract = False
        elif run.usage.get("durable") and run.usage.get("extraction_input"):
            needs_extract = True
        files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job_id).order_by(CustomerFile.id)))
        brief = job.brief + "\n" + "\n".join(m["text"] for m in job.messages[-10:] if m.get("role") == "user")
        previous = job.requirements
        extraction_usage_before = {field: run.usage.get(field, 0)
                                   for field in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if run.usage.get("durable") and needs_extract:
            run.usage = {**run.usage, "phase": "extracting", "last_scheduled_at": time.time()}
            run.message = "正在整理客户资料，尚未进入计价。"
        db.commit()
        if needs_extract:
            phase = "extract"
            if run.usage.get("durable") or settings().source_agent_enabled:
                from .background_extraction import extract_in_background
                result, attempts, usage = extract_in_background(
                    db, job, run, files, brief, previous, supplementary, interrupted)
            else:
                result, attempts, usage = deepseek.extract(files, brief, previous, supplementary)
            if interrupted and interrupted():
                raise Stopped()
            job, run = locked(db, job_id, run_id)
            from .background_extraction import requirement_identity
            saved_ids = {}
            if job.extraction.get("incomplete") and job.extraction.get("partial_started"):
                for raw in job.requirements:
                    saved_ids.setdefault(requirement_identity(raw), []).append(raw["id"])
            job.extraction = result.model_dump(mode="json")
            rows = []
            for index, line in enumerate(result.lines):
                raw = line.model_dump(mode="json")
                source_audit = result.source_agent or {}
                identifiers = source_audit.get("observation_ids", [])
                protected = source_audit.get("protected_lines", {}).get(
                    identifiers[index] if index < len(identifiers) else "")
                if protected:
                    rows.append(RequirementLine.model_validate(protected).model_dump(mode="json"))
                    continue
                ids = saved_ids.get(requirement_identity(raw), [])
                rows.append(RequirementLine(id=ids.pop(0) if ids else uuid.uuid4().hex[:16],
                                            business_rules_version=job.business_rules_version,
                                            **raw).model_dump(mode="json"))
            job.requirements = rows
            if run.usage.get("durable"):
                run.usage = {**run.usage, "prepared": True}
            else:
                run.usage = {**run.usage, **{field: extraction_usage_before[field] + usage.get(field, 0)
                             for field in extraction_usage_before},
                             "extraction_tokens": extraction_usage_before.get("total_tokens", 0)
                                                  + usage.get("total_tokens", 0), "prepared": True}
            event(run, "extract_requirements", {}, {"line_count": len(result.lines),
                                                   "questions": result.questions, "attempts": attempts})
            db.commit()
        if run.usage.get("extraction_only"):
            from .models import now
            from .services import log_change
            job, run = locked(db, job_id, run_id)
            run.status, run.finished_at = "succeeded", now()
            run.message = f"需求整理完成，已保存 {len(job.requirements)} 项明细，等待人工确认。"
            run.usage = {**run.usage, "phase": "succeeded"}
            job.status = "needs_review"
            job.messages = [*job.messages, {"role": "assistant", "text": run.message,
                                          "at": now().isoformat() + "Z"}][-40:]
            log_change(db, job, "extract_success", run.message)
            db.commit()
            return
        phase = "quote"
        job, run = locked(db, job_id, run_id)
        from .pricing_inventory import prepare_pricing_inventory
        if prepare_pricing_inventory(db, job):
            db.commit()
            job, run = locked(db, job_id, run_id)
        if not job.requirements and (job.extraction.get("source_agent") or {}).get("mode") == "ai":
            finish(db, job, run, "waiting", "资料核对已保存，尚无可报价项目。"
                   + "；".join(job.extraction.get("questions", []))[:1500])
            db.commit()
            return
        from .non_product_rows import exclude_non_products
        excluded = exclude_non_products(db, job)
        if excluded:
            event(run, "exclude_non_products", {}, {"count": len(excluded), "rows": [
                {k: item[k] for k in ("file_id", "page", "sheet", "row", "reason")} for item in excluded]})
            db.commit()
            job, run = locked(db, job_id, run_id)
        if run.usage.get("durable") and not needs_extract:
            work.restore_work_state(db, job, run)
        reopened = work.reopen_disabled_token_deferrals(job, run)
        if reopened:
            event(run, "resume_budget_deferred", {}, {"line_ids": reopened,
                "reason": "单项模型用量上限已关闭，保留方案和诊断后重新处理"})
            db.commit()
        if run.usage.get("durable") and settings().agent_pricing_concurrency > 1:
            parallel = PricingRequests(settings().agent_pricing_concurrency)
        messages = refresh_context(job, run)
        extraction_tokens = run.usage.get("extraction_tokens", run.usage.get("total_tokens", 0))
        run.usage = {**run.usage, "extraction_tokens": extraction_tokens,
                     "agent_tokens": run.usage.get("agent_tokens", 0),
                     "context_compactions": run.usage.get("context_compactions", 0),
                     "prepared": True, "cycles": run.usage.get("cycles", 0) + 1,
                     "reused_line_count": len(job.requirements) if not needs_extract else 0}
        if run.usage.get("durable"):
            concurrency = settings().agent_pricing_concurrency
            if parallel and run.usage.get("source_view_pending") and run.usage.get("active_line_id"):
                work.update_state(job, run, run.usage["active_line_id"],
                                  source_view_pending=run.usage["source_view_pending"])
                run.usage = {key: value for key, value in run.usage.items() if key != "source_view_pending"}
            run.message = f"后台正在计费，最多同时处理 {concurrency} 项；整单完成后提交人工审核。"
            run.usage = {**run.usage, "phase": "running", "retry_at": 0,
                         "pricing_concurrency": concurrency,
                         "last_scheduled_at": time.time()}
        db.commit()
        discovered, tool_count = discovered_prices(run) | work.discovered_references(job, run), 0
        response_failures = 0
        total_response_failures = run.usage.get("response_failures", 0)
        segment, segment_rounds, segment_tools, segment_tokens = 1, 0, 0, 0
        segment_started = time.monotonic()
        for round_number in range(MAX_ROUNDS * MAX_SEGMENTS):
            if interrupted and interrupted():
                raise Stopped()
            job, run = locked(db, job_id, run_id)
            from .collaborative_quotes import consume_reply
            if consume_reply(db, job, run):
                if parallel:
                    parallel.invalidate()
                job, run = locked(db, job_id, run_id)
                messages = refresh_context(job, run)
                db.commit()
                continue
            from .pricing_proposals import recover_catalog_precision, recover_changed_proposal
            if recover_changed_proposal(db, job, run, discovered, interrupted):
                messages = refresh_context(job, run)
                db.commit()
                continue
            if recover_catalog_precision(db, job, run, discovered, interrupted):
                messages = refresh_context(job, run)
                db.commit()
                if run.usage.get("durable"):
                    from .agent_checkpoints import save_checkpoint
                    save_checkpoint(db, job, run)
                continue
            if run.usage.get("durable") and commit_preview(db, job, run, discovered, interrupted):
                messages = refresh_context(job, run)
                db.commit()
                from .agent_checkpoints import save_checkpoint
                save_checkpoint(db, job, run)
                continue
            if not run.usage.get("durable") and work.deferred_lines(job, run):
                if finish_deferred_quote(db, job, run):
                    db.commit()
                    return
            if run.usage.get("durable"):
                if finish_deferred_quote(db, job, run):
                    db.commit()
                    return
                limit = None
                token_cap = settings().agent_max_total_tokens
                if token_cap > 0 and run.usage.get("agent_tokens", 0) >= token_cap:
                    limit = {"reason": "agent_tokens", "used": run.usage["agent_tokens"],
                             "limit": token_cap}
                    notice = f"报价模型用量已达到费用保护上限（{limit['used']} / {limit['limit']} tokens）"
                elif (parallel is None and not settings().agent_compact_plans
                      and total_response_failures >= MAX_TOTAL_RESPONSE_FAILURES):
                    limit = {"reason": "response_failures", "used": total_response_failures,
                             "limit": MAX_TOTAL_RESPONSE_FAILURES}
                    notice = f"模型返回无效响应已达到自动恢复上限（{limit['used']} / {limit['limit']} 次）"
                if limit and not (parallel and parallel.has_pending):
                    event(run, "execution_limit", {}, limit)
                    finish(db, job, run, "waiting", f"{notice}，已暂停防止继续消耗；已保存进度，可继续报价。")
                    db.commit()
                    return
                idle = run.usage.get("no_progress_rounds", 0)
                if idle >= settings().agent_max_idle_rounds and not run.usage.get("idle_warning"):
                    event(run, "progress_notice", {}, {
                        "no_progress_rounds": idle,
                        "message": "连续多轮尚无新金额；保留失败方案和诊断，先尝试修正，不能合理计价时暂挂并继续其他项目。"})
                    run.usage = {**run.usage, "idle_warning": True}
                # Yield before reserving a plan turn so the next slice can use it.
                slice_due = (round_number >= settings().agent_slice_rounds
                             or time.monotonic() - started >= settings().agent_slice_seconds)
                if round_number and slice_due and not (parallel and parallel.has_pending):
                    run.usage = {**run.usage, "phase": "queued", "retry_at": 0}
                    run.message = "本轮进度已保存，排队继续处理剩余项目。"
                    db.commit()
                    return
                active_id = run.usage.get("active_line_id")
                if (parallel is None and active_id and not work.line_state(job, run, active_id).get("deferred")
                        and (pause_reason := work.exhausted_reason(job, run, active_id))):
                    state = work.line_state(job, run, active_id)
                    if (pause_reason == "line_token_budget" and state.get("plan_grace_turns", 0) < 3
                            and state.get("repair_attempts", 0) < settings().agent_repair_attempts):
                        work.update_state(job, run, active_id, plan_grace_used=True,
                                          plan_grace_turns=state.get("plan_grace_turns", 0) + 1)
                        messages = refresh_context(job, run)
                    else:
                        result = defer_requirement(job, run, DeferInput(
                            line_id=active_id, **work.pause_details(job, run, active_id, pause_reason)))
                        result["pause_trigger"] = pause_reason
                        event(run, "defer_requirement", {"line_id": active_id}, result)
                        messages = refresh_context(job, run)
                        db.commit()
                        continue
            # Drop duplicated plans before checking the context budget.
            if parallel is None and run.steps and (run.steps[-1]["result"].get("saved")
                              or run.steps[-1]["result"].get("selected")
                              or run.steps[-1]["result"].get("continue_other_items")):
                messages = refresh_context(job, run)
            if time.monotonic() - started >= MAX_TOTAL_SECONDS:
                pending_quote(db, job, run, "已到连续执行保护时限，未能确认的报价及条件已逐项列出。")
                db.commit()
                return
            reasons = []
            if segment_rounds >= MAX_ROUNDS:
                reasons.append("rounds")
            if segment_tools >= MAX_TOOLS:
                reasons.append("tools")
            if segment_tokens >= MAX_TOKENS:
                reasons.append("tokens")
            if time.monotonic() - segment_started >= MAX_SECONDS:
                reasons.append("time")
            if parallel is None and context_chars(messages) >= MAX_CONTEXT_CHARS and segment_rounds:
                reasons.append("context")
            if reasons:
                resource_limit = any(reason not in ("context", "tokens") for reason in reasons)
                if resource_limit and segment >= MAX_SEGMENTS:
                    pending_quote(db, job, run, "已达到本次运行资源上限，报价尚未完成；已保存进度，可继续执行。")
                    db.commit()
                    return
                if resource_limit:
                    segment += 1
                    segment_rounds, segment_tools, segment_tokens = 0, 0, 0
                    segment_started = time.monotonic()
                elif "tokens" in reasons:
                    segment_tokens = 0
                if parallel is None:
                    messages = refresh_context(job, run)
                    event(run, "context_compact", {"reasons": reasons},
                          {"segment": segment, "message": "已压缩查价记录，自动继续，无需手动操作"})
                    run.usage = {**run.usage,
                                 "context_compactions": run.usage.get("context_compactions", 0) + 1}
            db.commit()
            active_id = run.usage.get("active_line_id")
            state = work.line_state(job, run, active_id) if active_id else {}
            if (parallel is None and run.usage.get("durable") and run.usage.get("no_progress_rounds", 0) >= 3
                    and not state.get("repair_prompted")):
                # Reorient once, then retain subsequent tool responses for actual repair.
                work.update_state(job, run, active_id, repair_prompted=True)
                messages = refresh_context(job, run)
                db.commit()
            try:
                if run.usage.get("durable") and parallel is None:
                    job, run = locked(db, job_id, run_id)
                    refreshed = refresh_context(job, run)
                    messages[:2] = refreshed
                    context = json.loads(messages[1]["content"])
                    active = context.get("active_requirement") or {}
                    run.usage = {**run.usage, "active_product": active.get("product", ""),
                                 "active_line_id": active.get("id", "")}
                    if active:
                        work.update_state(job, run, active["id"],
                                          idle_rounds=work.line_state(job, run, active["id"]).get("idle_rounds", 0) + 1)
                        saved_views = work.line_state(job, run, active["id"]).get("source_view_pending")
                        if saved_views and not run.usage.get("source_view_pending"):
                            run.usage = {**run.usage, "source_view_pending": saved_views}
                    db.commit()
                tools = TOOLS
                if run.usage.get("durable") and parallel is None:
                    active_id = run.usage.get("active_line_id")
                    stage = context.get("pricing_stage") if active_id else None
                    state = work.line_state(job, run, active_id)
                    decision_due = state.get("plan_grace_used", False) or state.get("decision_due", False)
                    tools = tools_for_stage(tools, stage, settings().estimate_contract_version, decision_due,
                                            runtime_repair=bool(context.get("self_repair")))
                    if decision_due:
                        messages = refresh_context(job, run)
                        submit_action = "submit_pricing_plan" if settings().agent_compact_plans else "preview_estimate"
                        messages[0]["content"] += (
                            "\n本轮须推进决策：现有资料与候选已在active_requirement/work_state/checkpoint中。"
                            f"先{submit_action}提交可检查的方案，或select_price选择已核验适用的标准价；"
                            "预检返回具体缺项后可针对性补查。若关键条件确实无法合理推导，"
                            "defer_requirement必须指出具体缺项、影响及可回答的问题，不能只写尚未形成方案。"
                            "确有具体疑点可继续定向读原件、核图或修脚本；不重复浏览相同内容，不编造工程量或价格。")
                        if settings().agent_compact_plans:
                            messages[0]["content"] += (
                                "\n本轮使用submit_pricing_plan或repair_pricing_plan执行上述决策；"
                                "不要使用旧preview_estimate格式，不要重写已有方案。")
                    run.usage = {**run.usage, "pricing_stage": stage}
                    db.commit()
                if not semantic_search.get_key():
                    tools = [tool for tool in tools if tool["function"]["name"] != "search_prices_semantic"]
                pending_view = run.usage.get("source_view_pending") if parallel is None else None
                if pending_view:
                    from .source_agent_tools import InspectRegion
                    views = [pending_view] if isinstance(pending_view, dict) else pending_view
                    for source in views:
                        try:
                            viewed = execute_tool(db, job, run, "view_source_region",
                                InspectRegion.model_validate(source), discovered, interrupted)
                            images = viewed.pop("_source_images", [])
                            content = [{"type": "text", "text": json.dumps({
                                "file_id": source["file_id"], "page": source["page"],
                                "region": source["region"], "notice": (
                                    "实际原图，仅作待核对数据" if images else
                                    viewed.get("next_action", "未获得图片，不能作为已核图依据"))},
                                ensure_ascii=False)}, *images]
                        except (ValueError, OSError, RuntimeError) as exc:
                            content = [{"type": "text", "text": json.dumps({
                                "source": source, "error": str(exc)[:600],
                                "notice": "本次原图回读失败，请换区域或工具继续查证"}, ensure_ascii=False)}]
                        messages.append({"role": "user", "content": content})
                if parallel:
                    messages, tools, context, decision_due, (message, usage) = parallel.next_turn(
                        db, job_id, run_id, discovered, interrupted, refill=not (limit or slice_due))
                    active_id = parallel.current.line_id if parallel.current else ""
                else:
                    message, usage = deepseek.agent_turn(messages, tools)
            except deepseek.AgentResponseFailure as exc:
                if interrupted and interrupted():
                    raise Stopped()
                job, run = locked(db, job_id, run_id)
                if parallel:
                    active_id = run.usage.get("active_line_id")
                    parallel.clear_history(active_id)
                    if parallel.stale:
                        account_usage(job, run, active_id, exc.usage)
                        event(run, "discard_inflight_response", {"line_id": active_id},
                              {"executed_calls": 0, "reason": "inputs_changed"})
                        db.commit()
                        parallel.acknowledge()
                        continue
                response_failures += 1
                total_response_failures += 1
                usage = exc.usage
                run.usage = {**run.usage, **{k: run.usage.get(k, 0) + int(usage.get(k, 0))
                             for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                             "agent_tokens": run.usage.get("agent_tokens", 0) + int(usage.get("total_tokens", 0)),
                             "response_failures": total_response_failures}
                if parallel:
                    work.charge_line(job, run, int(usage.get("total_tokens", 0)))
                    db.commit()
                    parallel.acknowledge()
                segment_rounds += 1
                segment_tokens += int(usage.get("total_tokens", 0))
                event(run, "response_recovery", {"attempt": response_failures}, {
                    **exc.diagnostics, "executed_calls": 0,
                    "message": "本轮响应未执行，保留已保存进度，改为逐项调用并自动重试"})
                if (settings().agent_compact_plans or parallel) and run.usage.get("durable") and active_id:
                    line_failures = work.line_state(job, run, active_id).get("response_failures", 0) + 1
                    if parallel:
                        response_failures = line_failures
                    work.update_state(job, run, active_id, response_failures=line_failures)
                    if line_failures >= MAX_RESPONSE_FAILURES:
                        result = defer_requirement(job, run, DeferInput(line_id=active_id,
                            cause="execution_error", reason="本项模型输出多次不完整，方案尚未通过程序校验；保留资料并继续其他项目",
                            missing_fields=["pricing_proposal"]))
                        event(run, "defer_requirement", {"line_id": active_id}, result)
                        response_failures = 0
                        db.commit()
                        messages = refresh_context(job, run)
                        continue
                if (response_failures >= MAX_RESPONSE_FAILURES
                        or parallel is None and not settings().agent_compact_plans
                        and total_response_failures >= MAX_TOTAL_RESPONSE_FAILURES):
                    if run.usage.get("durable") and total_response_failures >= MAX_TOTAL_RESPONSE_FAILURES:
                        finish(db, job, run, "waiting", "累计模型响应异常已达上限，需要人工介入；已保存进度，可继续报价。")
                        db.commit()
                        return
                    pending_quote(db, job, run,
                                  "模型连续返回不完整或无效工具响应，已达到自动重试上限。"
                                  "本轮异常调用未执行，已保存的需求与补全方案保留，可继续报价，无需重新上传。",
                                  failure_notice="模型响应连续异常，已达到自动恢复上限。资料和计价进度已保留，"
                                  "可点击继续查价重试，无需重新上传。")
                    db.commit()
                    return
                # Restart from committed state, without appending malformed calls or imaginary tool results.
                messages = refresh_context(job, run)
                submit_action = "submit_pricing_plan" if settings().agent_compact_plans else "complete_estimate"
                messages.append({"role": "user", "content":
                    "上一轮工具响应不完整或格式异常，整轮没有执行。请保留已成功保存的方案，"
                    "本轮只调用一个工具、只处理一个尚未完成的需求行。"
                    f"优先{submit_action}提交该行的简洁补全与费用方案，不要一次输出全部项目，"
                    "arguments必须是完整JSON对象；不要用正文重复工具参数。"})
                db.commit()
                if parallel:
                    parallel.acknowledge()
                continue
            except deepseek.ModelFailure as exc:
                if not parallel or not parallel.current or exc.diagnostics.get("retryable") is False:
                    raise
                if interrupted and interrupted():
                    raise Stopped()
                job, run = locked(db, job_id, run_id)
                account_usage(job, run, parallel.current.line_id, exc.usage)
                if parallel.stale:
                    event(run, "discard_inflight_response", {"line_id": parallel.current.line_id},
                          {"executed_calls": 0, "reason": "inputs_changed"})
                else:
                    parallel.retry(job, run, exc)
                db.commit()
                parallel.acknowledge()
                continue
            response_failures = 0
            if interrupted and interrupted():
                raise Stopped()
            job, run = locked(db, job_id, run_id)
            if parallel is None and run.usage.get("source_view_pending"):
                run.usage = {k: v for k, v in run.usage.items() if k != "source_view_pending"}
                work.update_state(job, run, run.usage.get("active_line_id"), source_view_pending=None)
            work.charge_line(job, run, int(usage.get("total_tokens", 0)))
            run.usage = {**run.usage, **{k: run.usage.get(k, 0) + int(usage.get(k, 0))
                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                         "agent_tokens": run.usage.get("agent_tokens", 0) + int(usage.get("total_tokens", 0)),
                         "no_progress_rounds": run.usage.get("no_progress_rounds", 0) + 1}
            segment_rounds += 1
            segment_tokens += int(usage.get("total_tokens", 0))
            db.commit()
            if parallel:
                parallel.acknowledge()
            if parallel and (parallel.stale or parallel.inputs_changed(job)):
                event(run, "discard_inflight_response", {"line_id": active_id},
                      {"executed_calls": 0, "reason": "inputs_changed"})
                parallel.clear_history(active_id)
                db.commit()
                continue
            if parallel and active_id:
                work.update_state(job, run, active_id, source_view_pending=None,
                                  request_failures=0, retry_at=0)
                db.commit()
            if any(m.get("pending_reply") for m in job.messages):
                messages = refresh_context(job, run)
                continue
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                action = "submit_pricing_plan提交方案或repair_pricing_plan局部修正" if settings().agent_compact_plans else "complete_estimate逐行补全"
                messages.append({"role": "user", "content": f"请只调用一个工具继续。用{action}；缺少依据用defer_requirement提出具体问题，全部完成后finish_quote。"})
                continue
            if parallel:
                allowed = {tool["function"]["name"] for tool in tools}
                if any(call["function"]["name"] not in allowed or
                       (parallel.current and
                        json.loads(call["function"]["arguments"]).get("line_id", active_id) != active_id)
                       for call in calls):
                    result = {"error": "并发计价只能操作当前分配项目及其允许的工具；整轮未执行",
                              "executed_calls": 0}
                    event(run, "parallel_scope_rejected", {"line_id": active_id}, result)
                    db.commit()
                    messages.extend({"role": "tool", "tool_call_id": call["id"],
                                     "content": json.dumps(result, ensure_ascii=False)} for call in calls)
                    continue
            for call in calls:
                if interrupted and interrupted():
                    raise Stopped()
                job, run = locked(db, job_id, run_id)
                if any(m.get("pending_reply") for m in job.messages):
                    break
                if parallel and parallel.inputs_changed(job):
                    event(run, "discard_inflight_response", {"line_id": active_id},
                          {"executed_calls": 0, "reason": "inputs_changed"})
                    db.commit()
                    parallel.clear_history(active_id)
                    break
                tool_count += 1
                segment_tools += 1
                if tool_count > MAX_TOOLS * MAX_SEGMENTS or time.monotonic() - started >= MAX_TOTAL_SECONDS:
                    pending_quote(db, job, run, "已达到总保护上限，未确认项目已保留在清单中，请核实其计价条件。")
                    db.commit()
                    return
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                raw = {}
                before_requirements = job.requirements
                previous_state = deepcopy(work.line_state(job, run, run.usage.get("active_line_id")))
                try:
                    if name not in TOOL_SPECS:
                        raise ValueError("不允许的工具")
                    if len(arguments) > (64000 if name == "run_file_script" else
                                         24000 if name in ("submit_pricing_plan", "repair_pricing_plan") else 12000):
                        raise ValueError("工具参数过长")
                    raw = json.loads(arguments)
                    if parallel and parallel.current:
                        if raw.get("line_id", active_id) != active_id:
                            raise ValueError("并发计价只能操作当前分配的项目，不能修改其他行")
                        if name not in {tool["function"]["name"] for tool in tools}:
                            raise ValueError("当前项目不允许此工具；整单汇总由协调器完成")
                    args = TOOL_SPECS[name][0].model_validate(raw)
                    previous_state = deepcopy(work.line_state(
                        job, run, raw.get("line_id") or run.usage.get("active_line_id")))
                    if (run.usage.get("durable") and decision_due and name not in ("complete_estimate", "preview_estimate")
                            and name not in {tool["function"]["name"] for tool in tools}):
                        raise ValueError("本轮须先试算、选价或提出具体缺项，不能继续重复查询")
                    repetitions = work.repeat_call(job, run, name, raw) if run.usage.get("durable") else 0
                    if repetitions >= 3:
                        line_id = raw.get("line_id") or run.usage.get("active_line_id")
                        state = work.line_state(job, run, line_id)
                        result = {
                            "error": "相同项目输入未变化，重复调用不会推进报价；"
                                     "请利用已有候选和失败方案修正参数，或defer_requirement提出具体问题并继续其他项",
                            "repeated_call": True, "repeat_count": repetitions,
                            "work_state": {key: state[key] for key in (
                                "failed_estimate", "diagnostics", "candidates", "evaluation") if key in state},
                        }
                    else:
                        result = execute_tool(db, job, run, name, args, discovered, interrupted=interrupted)
                except ValidationError as exc:
                    result = {"error": "工具参数校验失败，请修正指定字段", "type": "ValidationError",
                              "fields": [{"path": ".".join(str(p) for p in e["loc"]),
                                          "type": e["type"], "message": e["msg"][:250]}
                                         for e in exc.errors(include_input=False, include_url=False)[:12]]}
                    if name == "submit_pricing_plan":
                        from .pricing_proposals import remember_invalid_plan
                        version = remember_invalid_plan(job, run, raw)
                        if version:
                            result.update(plan_version=version,
                                          next_action="方案已保留，用repair_pricing_plan只修改报错字段")
                except ValueError as exc:
                    result = {"error": str(exc)[:1200] or "工具或参数无效，请按照 schema 重试",
                              "type": type(exc).__name__}
                except Stopped:
                    raise
                except Exception as exc:
                    diagnostic_id = uuid.uuid4().hex[:16]
                    logger.error("Tool failure %s (%s)\n%s", diagnostic_id, type(exc).__name__,
                                 "".join(traceback.format_tb(exc.__traceback__)))
                    db.rollback()
                    job, run = locked(db, job_id, run_id)
                    result = {"error": "程序执行异常，已保留此前提交进度",
                              "diagnostic_id": diagnostic_id,
                              "type": type(exc).__name__,
                              "diagnostics": [{"code": "tool_exception", "cause": "execution_error",
                                  "paths": [], "message": "程序执行异常，诊断编号：" + diagnostic_id,
                                  "action": "repair_failed_tool"}]}
                    line_id = raw.get("line_id") or run.usage.get("active_line_id")
                    if line_id and any(row["id"] == line_id for row in job.requirements):
                        work.update_state(job, run, line_id, diagnostic_id=diagnostic_id,
                                          diagnostics=result)
                from . import runtime_recovery
                recovered = False
                if name != "repair_failed_tool" and result.get("error") and result.get("type"):
                    recovered = runtime_recovery.register(job, run, name, raw, result)
                    if (not recovered and result.get("diagnostic_id")
                            and (line_id := raw.get("line_id") or run.usage.get("active_line_id"))
                            and any(row["id"] == line_id for row in job.requirements)):
                        result.update(defer_requirement(job, run, DeferInput(line_id=line_id,
                            cause="execution_error", reason="执行恢复未通过，诊断编号：" + result["diagnostic_id"])))
                runtime_recovery.observe_success(job, run, name, result, previous_state)
                result.pop("_source_images", None)
                event(run, name[:100], raw, result)
                if not result.get("repeated_call"):
                    effective_name = result.get("repaired_tool", name) if result.get("recovery_verified") else name
                    effective_args = result.get("repaired_arguments", raw) if result.get("recovery_verified") else raw
                    work.remember(job, run, effective_name, effective_args, result)
                    if work.record_repair_progress(job, run, name, raw, result):
                        run.usage = {**run.usage, "no_progress_rounds": 0}
                if (result.get("saved") or result.get("selected")) and job.requirements != before_requirements:
                    run.usage = {**run.usage, "retry_count": 0, "no_progress_rounds": 0,
                                 "idle_warning": False, "last_progress_at": time.time()}
                terminal = run.status != "processing" or (
                    result.get("deferred", False) and not result.get("continue_other_items"))
                db.commit()
                if parallel and parallel.current:
                    current_raw = next((row for row in job.requirements if row["id"] == active_id), None)
                    if current_raw:
                        parallel.current.fingerprint = input_fingerprint(job, current_raw)
                if run.usage.get("durable") and (
                        result.get("saved") or result.get("selected")
                        or result.get("continue_other_items") or terminal):
                    from .agent_checkpoints import save_checkpoint
                    save_checkpoint(db, job, run)
                if terminal:
                    return
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(model_result(name, result), ensure_ascii=False, separators=(",", ":"))})
            if run.usage.get("durable") and commit_preview(db, job, run, discovered, interrupted):
                messages = refresh_context(job, run)
                db.commit()
                from .agent_checkpoints import save_checkpoint
                save_checkpoint(db, job, run)
        job, run = locked(db, job_id, run_id)
        pending_quote(db, job, run, "已达到本次运行资源上限，报价尚未完成；已保存进度，可继续执行。")
        db.commit()
    except PricingYield as exc:
        try:
            job, run = locked(db, job_id, run_id)
        except Stopped:
            return
        run.usage = {**run.usage, "phase": "retrying", "retry_at": exc.retry_at}
        run.message = "本轮可处理项目已保存，等待受限或连接失败的项目重试。"
        db.commit()
        return
    except ExtractionYield:
        try:
            job, run = locked(db, job_id, run_id)
        except Stopped:
            return
        source_phase = run.usage.get("extraction_checkpoint", {}).get("source_agent", {}).get("phase")
        run.usage = {**run.usage, "phase": ("source_review" if source_phase == "review" else
                                          "source_reading" if source_phase else "extracting"), "retry_at": 0}
        db.commit()
        return
    except Stopped:
        return
    except Exception as exc:
        db.rollback()
        if interrupted and interrupted():
            return
        diagnostic_id = uuid.uuid4().hex[:16]
        logger.error("Agent failure %s (%s), phase=%s\n%s", diagnostic_id,
                     type(exc).__name__, phase, "".join(traceback.format_tb(exc.__traceback__)))
        try:
            job, run = locked(db, job_id, run_id)
            diagnostics = {"diagnostic_id": diagnostic_id, "phase": phase,
                           "exception_type": type(exc).__name__}
            if isinstance(exc, deepseek.ModelFailure):
                diagnostics.update(exc.diagnostics)
            elif isinstance(exc, ValidationError):
                diagnostics["invalid_fields"] = [
                    {"path": list(error["loc"]), "type": error["type"]}
                    for error in exc.errors(include_input=False, include_url=False)]
            if phase == "extract":
                usage = exc.usage if isinstance(exc, deepseek.ModelFailure) else {}
                if isinstance(exc, deepseek.ModelFailure) and not run.usage.get("durable"):
                    run.usage = {**run.usage, **{
                        field: extraction_usage_before.get(field, 0) + usage.get(field, 0)
                        for field in extraction_usage_before},
                        "extraction_tokens": extraction_usage_before.get("total_tokens", 0)
                                             + usage.get("total_tokens", 0), "agent_tokens": 0}
                event(run, "extract_requirements_failed", {
                    "attempts": exc.attempts if isinstance(exc, deepseek.ModelFailure) else 0}, {
                    "phase": "extract", "preserved_line_count": len(job.requirements),
                    "diagnostics": getattr(exc, "diagnostics", {}),
                    "message": "需求识别未通过，未进入查价；已保存需求未覆盖"})
            event(run, "execution_failure", {}, diagnostics)
            if (isinstance(exc, deepseek.ModelFailure)
                    and exc.diagnostics.get("stage") == "source_agent_budget"):
                finish(db, job, run, "waiting", f"{exc}；原件、观察和纠错记录均已保留。")
            elif (run.usage.get("durable") and isinstance(exc, deepseek.ModelFailure)
                    and exc.diagnostics.get("retryable") is False):
                if exc.diagnostics.get("stage") == "extraction_recovery":
                    finish(db, job, run, "waiting", f"{exc}；已保存原文件、成功批次和核对记录，可继续处理。")
                else:
                    finish(db, job, run, "waiting", f"{exc}；需要人工介入，修复后可继续，已保存进度不变。")
            elif isinstance(exc, (ValidationError, ValueError, TypeError, KeyError, AttributeError)):
                finish(db, job, run, "waiting",
                       f"程序执行校验异常，已暂停自动重试并保留进度；诊断编号：{diagnostic_id}。")
            elif run.usage.get("durable"):
                if phase == "extract" and isinstance(exc, deepseek.ModelFailure):
                    pending_quote(db, job, run, f"{exc}；将复用已保存的识别批次，仅重试未完成部分。")
                else:
                    pending_quote(db, job, run, "模型连接或执行暂时异常，后台将从已提交进度重试。")
            else:
                finish(db, job, run, "failed", str(exc) if isinstance(exc, deepseek.ModelFailure)
                       else "Agent 执行异常，已保存此前进度，请重试或人工检查。")
            db.commit()
        except Stopped:
            return
    finally:
        if parallel:
            parallel.close(db, job_id, run_id)
