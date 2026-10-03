"""Model-directed quoting; the server executes arithmetic and records reviewable drafts."""
import json
import time
import uuid

from pydantic import ValidationError
from sqlalchemy import select

from . import deepseek
from . import semantic_search
from . import agent_work_state as work
from .agent_requests import resolve_agent_intent
from .config import settings
from .models import AgentRun, CustomerFile
from .requirements_schemas import RequirementLine
# Split modules; names are re-exported here for existing callers and tests.
from .agent_tools import TOOLS, TOOL_SPECS, DeferInput, AskInput, PendingInput  # noqa: F401
from .agent_context import SYSTEM_PROMPT as SYSTEM, context_messages, discovered_prices, model_result  # noqa: F401
from .agent_execution import (  # noqa: F401
    Stopped, locked, event, finish, pending_quote, defer_requirement, finish_deferred_quote, execute_tool)

MAX_ROUNDS = 20
MAX_TOOLS = 60
MAX_SECONDS = 600
MAX_TOKENS = 100000
MAX_SEGMENTS = 8
MAX_TOTAL_SECONDS = 1800
MAX_CONTEXT_CHARS = 60000
MAX_RESPONSE_FAILURES = 3
MAX_TOTAL_RESPONSE_FAILURES = 6


def run_agent(db, job_id, run_id, supplementary="", replace=False, intent="auto", interrupted=None):
    started = time.monotonic()
    phase = "prepare"
    extraction_usage_before = {}
    try:
        job, run = locked(db, job_id, run_id)
        needs_extract = resolve_agent_intent(bool(job.requirements), supplementary, replace, intent) == "extract"
        if run.usage.get("durable") and run.usage.get("prepared"):
            needs_extract = False
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
            if run.usage.get("durable"):
                from .extraction_checkpoints import ExtractionCheckpoint, input_signature
                signature = input_signature(files, brief, previous, supplementary)
                cached = run.usage.get("extraction_checkpoint", {})
                if not cached:
                    earlier = db.scalar(select(AgentRun).where(
                        AgentRun.job_id == job_id, AgentRun.id < run_id).order_by(AgentRun.id.desc()).limit(1))
                    cached = earlier.usage.get("extraction_checkpoint", {}) if earlier else {}
                db.commit()

                def check_extraction():
                    if interrupted and interrupted():
                        raise Stopped()
                    locked(db, job_id, run_id)
                    db.commit()

                def persist_extraction(checkpoint, tokens):
                    current_job, current_run = locked(db, job_id, run_id)
                    previous_plans = current_run.usage.get("extraction_checkpoint", {}).get("plans", {})
                    for file_id, plan in checkpoint.get("plans", {}).items():
                        if plan != previous_plans.get(file_id):
                            for item in plan["sheets"]:
                                if item.get("created"):
                                    event(current_run, "create_excel_skill", {"file_id": int(file_id)},
                                          {"skill": item["skill"], "revision": item["skill_revision"],
                                           "layout": item["layout"], "sheet": item["sheet"]})
                            event(current_run, "select_excel_skills", {"file_id": int(file_id)}, {
                                "sheets": [{"sheet": item["sheet"], "skill": item["skill"],
                                            "layout": item["layout"], "reason": item["reason"],
                                            "origin": item["skill_origin"], "revision": item["skill_revision"]}
                                           for item in plan["sheets"]]})
                    current_run.usage = {**current_run.usage, **{
                        field: current_run.usage.get(field, 0) + tokens.get(field, 0)
                        for field in ("prompt_tokens", "completion_tokens", "total_tokens")},
                        "extraction_checkpoint": checkpoint,
                        "extraction_tokens": current_run.usage.get("extraction_tokens", 0)
                                             + tokens.get("total_tokens", 0)}
                    current_run.message = (f"正在按Excel解析Skill整理客户资料，已保存 {len(checkpoint['batches'])} 批识别结果；尚未进入计价。"
                                           if checkpoint.get("plans") else
                                           f"正在整理客户资料，已保存 {len(checkpoint['batches'])} 批识别结果；尚未进入计价。")
                    db.commit()

                checkpoint = ExtractionCheckpoint(signature, cached, persist_extraction, check_extraction)
                result, attempts, usage = deepseek.extract(files, brief, previous, supplementary,
                                                          None, None, checkpoint)
            else:
                result, attempts, usage = deepseek.extract(files, brief, previous, supplementary)
            if interrupted and interrupted():
                raise Stopped()
            job, run = locked(db, job_id, run_id)
            job.extraction = result.model_dump(mode="json")
            job.requirements = [RequirementLine(id=uuid.uuid4().hex[:16],
                                                business_rules_version=job.business_rules_version,
                                                **r.model_dump()).model_dump(mode="json")
                                for r in result.lines]
            run.usage = {**run.usage, **{field: extraction_usage_before[field] + usage.get(field, 0)
                         for field in extraction_usage_before},
                         "extraction_tokens": extraction_usage_before.get("total_tokens", 0)
                                              + usage.get("total_tokens", 0),
                         "prepared": True}
            event(run, "extract_requirements", {}, {"line_count": len(result.lines),
                                                   "questions": result.questions, "attempts": attempts})
            db.commit()
        phase = "quote"
        job, run = locked(db, job_id, run_id)
        if run.usage.get("durable") and not needs_extract:
            work.restore_work_state(db, job, run)
        messages = context_messages(job, run, db)
        extraction_tokens = run.usage.get("extraction_tokens", run.usage.get("total_tokens", 0))
        run.usage = {**run.usage, "extraction_tokens": extraction_tokens,
                     "agent_tokens": run.usage.get("agent_tokens", 0),
                     "context_compactions": run.usage.get("context_compactions", 0),
                     "prepared": True, "cycles": run.usage.get("cycles", 0) + 1,
                     "reused_line_count": len(job.requirements) if not needs_extract else 0}
        if run.usage.get("durable"):
            run.message = "后台正在逐项计费，已完成项保留在Redis；整单完成后提交人工审核。"
            run.usage = {**run.usage, "phase": "running", "retry_at": 0,
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
                elif total_response_failures >= MAX_TOTAL_RESPONSE_FAILURES:
                    limit = {"reason": "response_failures", "used": total_response_failures,
                             "limit": MAX_TOTAL_RESPONSE_FAILURES}
                    notice = f"模型返回无效响应已达到自动恢复上限（{limit['used']} / {limit['limit']} 次）"
                if limit:
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
                active_id = run.usage.get("active_line_id")
                if (active_id and not work.line_state(job, run, active_id).get("deferred")
                        and work.line_state(job, run, active_id).get("idle_rounds", 0) >= settings().agent_stall_rounds):
                    result = defer_requirement(job, run, DeferInput(
                        line_id=active_id, cause="execution_error",
                        reason="该项连续多轮未形成可计价方案，已停止无进展调用；"
                               "需检查保存的失败方案和诊断，不代表客户资料或价格库一定不足。"))
                    event(run, "defer_requirement", {"line_id": active_id}, result)
                    messages = context_messages(job, run, db)
                    db.commit()
                    continue
                # Yield only between complete tool batches, preserving the model protocol.
                if round_number and (round_number >= settings().agent_slice_rounds
                                     or time.monotonic() - started >= settings().agent_slice_seconds):
                    run.usage = {**run.usage, "phase": "queued", "retry_at": 0}
                    run.message = "本轮进度已保存，排队继续处理剩余项目。"
                    db.commit()
                    return
            # Drop duplicated plans before checking the context budget.
            if run.steps and (run.steps[-1]["result"].get("saved")
                              or run.steps[-1]["result"].get("selected")
                              or run.steps[-1]["result"].get("continue_other_items")):
                messages = context_messages(job, run, db)
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
            if len(json.dumps(messages, ensure_ascii=False)) >= MAX_CONTEXT_CHARS and segment_rounds:
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
                messages = context_messages(job, run, db)
                event(run, "context_compact", {"reasons": reasons},
                      {"segment": segment, "message": "已压缩查价记录，自动继续，无需手动操作"})
                run.usage = {**run.usage,
                             "context_compactions": run.usage.get("context_compactions", 0) + 1}
            db.commit()
            active_id = run.usage.get("active_line_id")
            state = work.line_state(job, run, active_id) if active_id else {}
            if (run.usage.get("durable") and run.usage.get("no_progress_rounds", 0) >= 3
                    and not state.get("repair_prompted")):
                # Reorient once, then retain subsequent tool responses for actual repair.
                work.update_state(job, run, active_id, repair_prompted=True)
                messages = context_messages(job, run, db)
                db.commit()
            try:
                if run.usage.get("durable"):
                    job, run = locked(db, job_id, run_id)
                    context = json.loads(messages[1]["content"])
                    active = context.get("active_requirement") or {}
                    run.usage = {**run.usage, "active_product": active.get("product", ""),
                                 "active_line_id": active.get("id", "")}
                    if active:
                        work.update_state(job, run, active["id"],
                                          idle_rounds=work.line_state(job, run, active["id"]).get("idle_rounds", 0) + 1)
                    db.commit()
                tools = TOOLS
                if not semantic_search.get_key():
                    tools = [tool for tool in tools if tool["function"]["name"] != "search_prices_semantic"]
                message, usage = deepseek.agent_turn(messages, tools)
            except deepseek.AgentResponseFailure as exc:
                if interrupted and interrupted():
                    raise Stopped()
                job, run = locked(db, job_id, run_id)
                response_failures += 1
                total_response_failures += 1
                usage = exc.usage
                run.usage = {**run.usage, **{k: run.usage.get(k, 0) + int(usage.get(k, 0))
                             for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                             "agent_tokens": run.usage.get("agent_tokens", 0) + int(usage.get("total_tokens", 0)),
                             "response_failures": total_response_failures}
                segment_rounds += 1
                segment_tokens += int(usage.get("total_tokens", 0))
                event(run, "response_recovery", {"attempt": response_failures}, {
                    **exc.diagnostics, "executed_calls": 0,
                    "message": "本轮响应未执行，保留已保存进度，改为逐项调用并自动重试"})
                if (response_failures >= MAX_RESPONSE_FAILURES
                        or total_response_failures >= MAX_TOTAL_RESPONSE_FAILURES):
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
                messages = context_messages(job, run, db)
                messages.append({"role": "user", "content":
                    "上一轮工具响应不完整或格式异常，整轮没有执行。请保留已成功保存的方案，"
                    "本轮只调用一个工具、只处理一个尚未完成的需求行。"
                    "优先complete_estimate提交该行的简洁补全与费用方案，不要一次输出全部项目，"
                    "arguments必须是完整JSON对象；不要用正文重复工具参数。"})
                db.commit()
                continue
            response_failures = 0
            if interrupted and interrupted():
                raise Stopped()
            job, run = locked(db, job_id, run_id)
            run.usage = {**run.usage, **{k: run.usage.get(k, 0) + int(usage.get(k, 0))
                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                         "agent_tokens": run.usage.get("agent_tokens", 0) + int(usage.get("total_tokens", 0)),
                         "no_progress_rounds": run.usage.get("no_progress_rounds", 0) + 1}
            segment_rounds += 1
            segment_tokens += int(usage.get("total_tokens", 0))
            db.commit()
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                messages.append({"role": "user", "content": "请只调用一个工具继续。普通缺项用complete_estimate逐行补全，全部完成后finish_quote。"})
                continue
            for call in calls:
                if interrupted and interrupted():
                    raise Stopped()
                job, run = locked(db, job_id, run_id)
                tool_count += 1
                segment_tools += 1
                if tool_count > MAX_TOOLS * MAX_SEGMENTS or time.monotonic() - started >= MAX_TOTAL_SECONDS:
                    pending_quote(db, job, run, "已达到总保护上限，未确认项目已保留在清单中，请核实其计价条件。")
                    db.commit()
                    return
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                raw = {}
                before_requirements = job.requirements
                try:
                    if name not in TOOL_SPECS:
                        raise ValueError("不允许的工具")
                    if len(arguments) > 12000:
                        raise ValueError("工具参数过长")
                    raw = json.loads(arguments)
                    args = TOOL_SPECS[name][0].model_validate(raw)
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
                except ValueError as exc:
                    result = {"error": "工具或参数无效，请按照 schema 重试", "type": type(exc).__name__}
                event(run, name[:100], raw, result)
                if not result.get("repeated_call"):
                    work.remember(job, run, name, raw, result)
                    if work.record_repair_progress(job, run, name, raw, result):
                        run.usage = {**run.usage, "no_progress_rounds": 0}
                if (result.get("saved") or result.get("selected")) and job.requirements != before_requirements:
                    run.usage = {**run.usage, "retry_count": 0, "no_progress_rounds": 0,
                                 "idle_warning": False, "last_progress_at": time.time()}
                terminal = run.status != "processing" or (
                    result.get("deferred", False) and not result.get("continue_other_items"))
                db.commit()
                if run.usage.get("durable") and (
                        result.get("saved") or result.get("selected")
                        or result.get("continue_other_items") or terminal):
                    from .agent_checkpoints import save_checkpoint
                    save_checkpoint(db, job, run)
                if terminal:
                    return
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(model_result(name, result), ensure_ascii=False, separators=(",", ":"))})
        job, run = locked(db, job_id, run_id)
        pending_quote(db, job, run, "已达到本次运行资源上限，报价尚未完成；已保存进度，可继续执行。")
        db.commit()
    except Stopped:
        return
    except Exception as exc:
        db.rollback()
        if interrupted and interrupted():
            return
        try:
            job, run = locked(db, job_id, run_id)
            if phase == "extract":
                usage = exc.usage if isinstance(exc, deepseek.ModelFailure) else {}
                if isinstance(exc, deepseek.ModelFailure):
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
            if (run.usage.get("durable") and isinstance(exc, deepseek.ModelFailure)
                    and exc.diagnostics.get("retryable") is False):
                finish(db, job, run, "waiting", f"{exc}；需要人工介入，修复后可继续，已保存进度不变。")
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
