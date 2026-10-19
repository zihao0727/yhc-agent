"""Bounded model inference; all ORM access and tool execution stay on the coordinator."""
import json
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import select

from . import agent_work_state as work, deepseek, semantic_search
from .agent_context import resilient_context_messages as context_messages
from .agent_context_budget import compact_history, context_chars
from .agent_execution import Stopped, defer_requirement, event, execute_tool, locked
from .agent_tools import DeferInput, TOOLS, tools_for_stage
from .config import settings
from .models import AgentRun, RequirementJob
from .quote_engine import calculate_line
from .requirements_schemas import RequirementLine


class PricingYield(Exception):
    def __init__(self, retry_at):
        self.retry_at = retry_at


@dataclass
class Turn:
    line_id: str
    fingerprint: str
    generation: int
    messages: list
    tools: list
    context: dict
    decision_due: bool
    elapsed_seconds: float = 0.0
    time_accounted: bool = False


def input_fingerprint(job, raw):
    return work.fingerprint({"requirement": raw, "brief": job.brief,
                             "business_rules_version": job.business_rules_version,
                             "extraction": job.extraction})


def account_usage(job, run, line_id, usage):
    tokens = int(usage.get("total_tokens", 0))
    run.usage = {**run.usage, "active_line_id": line_id,
                 **{key: run.usage.get(key, 0) + int(usage.get(key, 0))
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                 "agent_tokens": run.usage.get("agent_tokens", 0) + tokens}
    if line_id:
        work.charge_line(job, run, tokens)


def model_turn(turn):
    started = time.monotonic()
    try:
        return deepseek.agent_turn(deepcopy(turn.messages), deepcopy(turn.tools))
    finally:
        turn.elapsed_seconds = time.monotonic() - started


def account_time(job, run, turn):
    if not turn.time_accounted:
        state = work.line_state(job, run, turn.line_id)
        work.update_state(job, run, turn.line_id,
                          pricing_seconds=state.get("pricing_seconds", 0) + turn.elapsed_seconds)
        turn.time_accounted = True


class PricingRequests:
    def __init__(self, concurrency):
        self.concurrency = concurrency
        self.owner = uuid.uuid4().hex
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="pricing-model")
        self.pending = {}
        self.received = {}
        self.histories = {}
        self.generation = 0
        self.current = None
        self.stale = False

    @property
    def has_pending(self):
        return bool(self.pending)

    def invalidate(self):
        self.generation += 1
        self.histories.clear()

    def clear_history(self, line_id):
        self.histories.pop(line_id, None)

    def acknowledge(self):
        self.received.clear()

    def inputs_changed(self, job):
        if self.current is None:
            return False
        raw = next((raw for raw in job.requirements if raw["id"] == self.current.line_id), None)
        return (self.current.generation != self.generation or raw is None
                or input_fingerprint(job, raw) != self.current.fingerprint
                or any(m.get("pending_reply") for m in job.messages))

    def prepare(self, db, job, run, raw, discovered, interrupted, *,
                calculated=None, source_pages=None):
        line_id = raw["id"]
        state = work.line_state(job, run, line_id)
        work.update_state(job, run, line_id, idle_rounds=state.get("idle_rounds", 0) + 1,
                          pricing_turns=state.get("pricing_turns", 0) + 1,
                          pricing_seconds=state.get("pricing_seconds", 0),
                          started_at=state.get("started_at", time.time()))
        messages = context_messages(db=db, job=job, run=run, line_id=line_id,
                                    calculated=calculated, source_pages=source_pages)
        context = json.loads(messages[1]["content"])
        stage = context["pricing_stage"]
        state = work.line_state(job, run, line_id)
        decision_due = state.get("plan_grace_used", False) or state.get("decision_due", False)
        tools = tools_for_stage(TOOLS, stage, settings().estimate_contract_version, decision_due,
                                runtime_repair=bool(context.get("self_repair")))
        if (context.get("pricing_packet") or {}).get("sources"):
            tools = [tool for tool in tools if tool["function"]["name"] not in {
                "read_requirement", "list_sources"}]
        if not semantic_search.get_key():
            tools = [tool for tool in tools if tool["function"]["name"] != "search_prices_semantic"]
        messages[0]["content"] += (
            "\n当前项目与其他项目并发计价。只处理active_requirement.id，"
            "不得修改其他项目或结束整单；整单汇总由协调器完成。")
        if decision_due:
            messages[0]["content"] += (
                "\n本轮须提交可检查的计价方案或修正失败字段，或选择已核验适用的标准价。"
                "有具体疑点可定向读原件、核图或修脚本；确实缺关键条件时defer_requirement指出具体缺项及问题。"
                "不要重复查询或编造依据。")
        if state.get("response_failures"):
            messages[0]["content"] += (
                "\n此前有响应格式未通过，未执行其中工具。保留已保存方案，"
                "本轮只调用一个工具，修正指定字段，输出完整JSON；不要在正文重复参数。")
        old = self.histories.get(line_id)
        digest = input_fingerprint(job, raw)
        if old and old[0] == digest:
            messages.extend(old[1][2:])
        compacted = compact_history(messages)
        if len(compacted) < len(messages):
            messages = compacted
            run.usage = {**run.usage,
                         "context_compactions": run.usage.get("context_compactions", 0) + 1}
        run.usage = {**run.usage, "active_line_id": line_id}
        pending_view = state.get("source_view_pending")
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
                        "notice": "原图回读失败，请换区域或工具继续查证"}, ensure_ascii=False)}]
                messages.append({"role": "user", "content": content})
        self.histories[line_id] = (digest, messages)
        run.usage = {**run.usage, "last_pricing_context_chars": context_chars(messages)}
        return Turn(line_id, digest, self.generation, messages, tools, context, decision_due)

    def dispatch(self, db, job, run, discovered, interrupted):
        capacity = self.concurrency - len(self.pending)
        if capacity <= 0:
            return
        busy = {turn.line_id for turn in self.pending.values()}
        candidates, retry_times, calculated = [], [], []
        for raw in job.requirements:
            line_id = raw["id"]
            priced = calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
            calculated.append({key: priced[key] for key in (
                "line_id", "product", "amount", "blockers", "diagnostics", "repair_contract")
                if key in priced})
            if line_id in busy:
                continue
            state = work.line_state(job, run, line_id)
            if state.get("deferred"):
                continue
            if priced["amount"] is not None:
                continue
            if state.get("retry_at", 0) > time.time():
                retry_times.append(state["retry_at"])
                continue
            if "pricing_seconds" not in state:
                work.update_state(job, run, line_id, pricing_seconds=0)
            reason = work.exhausted_reason(job, run, line_id)
            if reason:
                if (reason == "line_token_budget" and state.get("plan_grace_turns", 0) < 3
                        and state.get("repair_attempts", 0) < settings().agent_repair_attempts):
                    work.update_state(job, run, line_id, plan_grace_used=True,
                                      plan_grace_turns=state.get("plan_grace_turns", 0) + 1)
                else:
                    result = defer_requirement(job, run, DeferInput(
                        line_id=line_id, **work.pause_details(job, run, line_id, reason)))
                    event(run, "defer_requirement", {"line_id": line_id},
                          {**result, "pause_trigger": reason})
                    continue
            candidates.append(raw)
        focus = run.usage.get("pricing_focus", {})

        def priority(raw):
            state = work.line_state(job, run, raw["id"])
            # Keep a short sequence of useful turns together before rotating.
            continuing = (0 < focus.get(raw["id"], 0) < 4
                          and work.pricing_stage(job, run, raw["id"]) != "repair"
                          and not state.get("runtime_recovery"))
            return (not bool(state.get("validated_plan")), not continuing,
                    state.get("pricing_turns", 0))

        candidates.sort(key=priority)
        turns, source_pages = [], {}
        for raw in candidates[:capacity]:
            if interrupted and interrupted():
                raise Stopped()
            turns.append(self.prepare(db, job, run, raw, discovered, interrupted,
                                      calculated=calculated, source_pages=source_pages))
        run.usage = {**run.usage, "pricing_concurrency": self.concurrency,
                     "pricing_request_owner": self.owner,
                     "pricing_focus": {
                         **{line_id: focus.get(line_id, 1) for line_id in busy},
                         **{turn.line_id: focus.get(turn.line_id, 0) % 4 + 1 for turn in turns}},
                     "inflight_line_ids": [*busy, *[turn.line_id for turn in turns]]}
        if turns:
            run.message = f"后台并发计价中，最多同时处理 {self.concurrency} 项；通过校验后逐项保存。"
        db.commit()
        for turn in turns:
            # Threads receive detached request data, never a job, run or SQL session.
            future = self.pool.submit(model_turn, turn)
            self.pending[future] = turn
        if not turns and not self.pending and retry_times:
            raise PricingYield(min(retry_times))

    def next_turn(self, db, job_id, run_id, discovered, interrupted, *, refill=True):
        self.current = None
        self.stale = False
        job, run = locked(db, job_id, run_id)
        if refill or not self.pending:
            self.dispatch(db, job, run, discovered, interrupted)
        if not self.pending:
            self.current = None
            self.stale = False
            messages = context_messages(job, run, db)
            context = json.loads(messages[1]["content"])
            # Dispatch may have deferred the last items; let the existing finalizer check them.
            if context.get("active_requirement") or context["quote_progress"]["pending_count"]:
                raise PricingYield(time.time())
            run.usage = {**run.usage, "active_line_id": "", "active_product": "", "pricing_stage": None}
            db.commit()
            tools = tools_for_stage(TOOLS, None, settings().estimate_contract_version)
            return messages, tools, context, False, deepseek.agent_turn(messages, tools)
        db.commit()
        while True:
            if interrupted and interrupted():
                raise Stopped()
            ready, _ = wait(self.pending, timeout=0.2, return_when=FIRST_COMPLETED)
            # No SQL transaction is held while the provider is working.
            job, run = locked(db, job_id, run_id)
            db.commit()
            if ready:
                break
        # The lease can change while wait() is returning a completed response.
        if (interrupted and interrupted()
                or run.usage.get("pricing_request_owner") != self.owner):
            raise Stopped()
        future = next(iter(ready))
        turn = self.pending.pop(future)
        self.received[future] = turn
        self.current = turn
        account_time(job, run, turn)
        self.stale = self.inputs_changed(job)
        run.usage = {**run.usage, "active_line_id": turn.line_id,
                     "active_product": (turn.context["active_requirement"] or {}).get("product", ""),
                     "pricing_stage": turn.context["pricing_stage"],
                     "inflight_line_ids": [item.line_id for item in self.pending.values()]}
        db.commit()
        return turn.messages, turn.tools, turn.context, turn.decision_due, future.result()

    def retry(self, job, run, exc):
        line_id = self.current.line_id
        state = work.line_state(job, run, line_id)
        count = state.get("request_failures", 0) + 1
        self.clear_history(line_id)
        if count >= settings().agent_max_retries:
            result = defer_requirement(job, run, DeferInput(
                line_id=line_id, cause="service_unavailable",
                reason="本项模型连接重试已达上限，已保存方案和资料，可恢复后继续"))
            event(run, "defer_requirement", {"line_id": line_id}, result)
        else:
            delay = min(settings().agent_retry_seconds * 2 ** min(count - 1, 6), 900)
            # Even a zero-delay test configuration must not spin on rate limits.
            if exc.diagnostics.get("status_code") == 429:
                delay = max(1, delay)
            work.update_state(job, run, line_id, request_failures=count, retry_at=time.time() + delay)
            event(run, "request_retry", {"line_id": line_id}, {
                "attempt": count, "retry_seconds": delay, **exc.diagnostics})

    def close(self, db, job_id, run_id):
        # Sent requests cannot be recalled. Await them without executing their tools,
        # and keep billed usage even after a stop, lease loss or fatal provider error.
        self.pool.shutdown(wait=True, cancel_futures=True)
        db.rollback()
        db.expire_all()
        job = db.scalar(select(RequirementJob).where(RequirementJob.id == job_id).with_for_update())
        run = db.get(AgentRun, run_id)
        if job is None or run is None:
            db.rollback()
            return
        active_id = run.usage.get("active_line_id")
        for future, turn in {**self.pending, **self.received}.items():
            if future.cancelled():
                continue
            try:
                _, usage = future.result()
            except deepseek.ModelFailure as exc:
                usage = exc.usage
            except Exception:
                usage = {}
            account_time(job, run, turn)
            account_usage(job, run, turn.line_id, usage)
            event(run, "discard_inflight_response", {"line_id": turn.line_id}, {
                "executed_calls": 0, "reason": "run_interrupted"})
        run.usage = {**run.usage, "active_line_id": active_id}
        if run.usage.get("pricing_request_owner") == self.owner:
            run.usage = {key: value for key, value in run.usage.items()
                         if key != "pricing_request_owner"}
            run.usage = {**run.usage, "inflight_line_ids": []}
        db.commit()
        self.pending.clear()
        self.received.clear()
