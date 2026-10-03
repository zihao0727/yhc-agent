"""Tool execution and run-state transitions for the agent loop."""
import re
import time
from decimal import Decimal
from sqlalchemy import func, or_, select

from . import agent_work_state as work
from . import business_rules
from . import semantic_search
from .agent_tools import DeferInput
from .component_pricing import analyze_components
from .config import settings
from .estimate_evidence import validate_estimate_evidence
from .models import AgentRun, CustomerFile, PriceItem, PricingRule, Product, QuoteDraft, RequirementJob, now
from .pricing_cases import search_cases, evaluate_case, case_references
from .pricing_diagnostics import repair_contract, line_resolution
from .pricing_scope import rule_scope
from .quote_engine import (
    build_quote, calculate_line, price_source, unit_price_usable,
    confirmation_items
)
from .requirements_schemas import RequirementLine
from .services import log_change, snapshot


class Stopped(Exception):
    """Raised when the agent run is interrupted or cancelled."""
    pass


def locked(db, job_id, run_id):
    """Lock the job and run for update, raising Stopped if no longer processing."""
    db.expire_all()
    job = db.scalar(select(RequirementJob).where(RequirementJob.id == job_id).with_for_update())
    run = db.get(AgentRun, run_id)
    if run.status != "processing" or job.status != "processing":
        db.rollback()
        raise Stopped()
    return job, run


def event(run, tool, arguments, result):
    """Append a tool execution event to the run's step history."""
    run.steps = [*run.steps, {"tool": tool, "arguments": arguments, "result": result,
                            "at": now().isoformat() + "Z"}]
    run.usage = {**run.usage, "last_event_at": time.time()}


def finish(db, job, run, status, message):
    """Mark the agent run as finished with the given status and message."""
    run.status, run.message, run.finished_at = status, message, now()
    run.usage = {**run.usage, "phase": status, "retry_at": 0}
    job.status = "quoted" if status == "succeeded" else "needs_review" if status == "waiting" else "failed"
    job.messages = [*job.messages, {"role": "assistant", "text": message,
                                  "at": now().isoformat() + "Z"}][-40:]
    log_change(db, job, "agent_" + status, message)


def pending_quote(db, job, run, reason, failure_notice="", allow_partial=False):
    """Deliver every requirement, without presenting missing prices as zero."""
    if run.usage.get("durable") and not allow_partial:
        retries = run.usage.get("retry_count", 0) + 1
        if retries >= settings().agent_max_retries:
            finish(db, job, run, "waiting", f"自动重试已达上限，需要人工介入。{reason} 已保存进度，可继续报价。")
            return {"paused": True, "reason": reason}
        delay = min(settings().agent_retry_seconds * 2 ** min(retries - 1, 6), 900)
        run.message = f"报价尚未完成，进度已保留，将自动继续。{reason}"
        run.usage = {**run.usage, "retry_count": retries, "phase": "retrying",
                     "retry_at": time.time() + delay}
        log_change(db, job, "agent_checkpoint", run.message)
        return {"deferred": True, "reason": reason}
    payload = build_quote(db, job.requirements, automatic=True)
    issues = {}
    candidates = {}
    for step in run.steps:
        line_id = step.get("arguments", {}).get("line_id")
        if step["tool"] in ("evaluate_price", "select_price") and line_id:
            if step["result"].get("selected"):
                issues.pop(line_id, None)
                candidates.pop(line_id, None)
            elif step["result"].get("blockers"):
                issues[line_id] = step["result"]["blockers"]
                price = step["result"].get("price")
                if price:
                    candidates[line_id] = {k: price[k] for k in ("id", "revision", "product", "unit", "amount") if k in price}
    deferred = {line["line_id"]: line for line in work.deferred_lines(job, run)}
    for line in payload["lines"]:
        raw = line["requirement"]
        line["blockers"] = list(dict.fromkeys([
            *line["blockers"], *(issues.get(line["line_id"], []) if line["blockers"] else [])]))
        if line["blockers"]:
            line["amount"] = None
        line["candidate_reference"] = candidates.get(line["line_id"])
        line["case_references"] = case_references(db, RequirementLine.model_validate(raw))
        issue = deferred.get(line["line_id"])
        if issue and line["amount"] is None:
            line["deferred_issue"] = issue
            line["blockers"] = list(dict.fromkeys([*line["blockers"], issue["reason"]]))
    for line in payload["lines"]:
        line["resolution"] = line_resolution(line)
    payload.update(complete=False, total=None,
                   known_subtotal=str(sum((Decimal(l["amount"]) for l in payload["lines"]
                                          if l["amount"] is not None), Decimal(0))),
                   confirmation_items=confirmation_items(payload["lines"]), pending_reason=reason,
                   title=job.title, customer=job.customer, generated_by="agent", agent_run_id=run.id)
    payload["resolution_counts"] = {status: sum(line["resolution"] == status for line in payload["lines"])
                                    for status in ("priced", "priced_review", "unresolved", "customer_confirmation",
                                                   "administrator_pricing", "execution_error")}
    questions = [f"{issue['product']}：{issue['question']}" for issue in deferred.values()
                 if issue.get("question")]
    if questions:
        payload["review_questions"] = questions
    subtotal_message = (f"已计价小计 {payload['known_subtotal']} 元" if payload["priced_count"]
                        else "暂未形成可计价金额")
    finish(db, job, run, "waiting",
           f"已核出成品单价 {payload['unit_priced_count']} 项，已计金额 {payload['priced_count']} 项，"
           f"{payload['pending_count']} 项未计价；"
           f"{subtotal_message}（已包含待审核估价）。其余项目尚未生成金额，不是因待审核而排除计费。"
           f"\n本次未完成原因：{reason}"
           + ("\n未解决事项及处理问题：\n" + "\n".join(questions) if questions else "")
           + (f"\n{failure_notice}" if failure_notice else ""))
    db.flush()
    version = (db.scalar(select(func.max(QuoteDraft.version)).where(QuoteDraft.job_id == job.id)) or 0) + 1
    quote = QuoteDraft(job_id=job.id, version=version, job_revision=job.revision, status="blocked",
                       payload=payload, terms="初步核价清单，非正式报价。已核实的成品单价先列示；数量未知时不计入总价。"
                       "材料成本参考独立列示，不是成品报价，不计入成品合计。估价条件和未计费用待审核。")
    db.add(quote)
    db.flush()
    log_change(db, quote, "agent_pending", reason)
    return {"quote_id": quote.id, "pending": True, "line_count": len(payload["lines"]), "reason": reason}


def defer_requirement(job, run, args):
    """Mark a requirement as deferred with a reason and optional question."""
    raw = next((raw for raw in job.requirements if raw["id"] == args.line_id), None)
    if raw is None:
        return {"error": "需求不存在"}
    if args.cause == "customer_input" and not args.question.strip():
        return {"error": "客户信息缺失必须提出具体可回答的问题，不能只写需要补资料"}
    if args.cause == "customer_input":
        known = []
        for path in args.missing_fields:
            match = re.fullmatch(r"(letter_groups|components)\[(\d+)\]\.([a-z_]+)", path)
            if match:
                collection = raw.get(match[1], [])
                index = int(match[2])
                value = collection[index].get(match[3]) if index < len(collection) else None
            else:
                value = raw.get(path)
            if value not in (None, "", "unknown"):
                known.append(path)
        if known:
            return {"error": "所追问字段已有识别值，请先核对原始证据；若识别错误需纠正，不能当作客户未提供",
                    "known_fields": known, "next_action": "read_requirement并按对象路径纠正或补全"}
    was_deferred = work.line_state(job, run, args.line_id).get("deferred")
    work.update_state(job, run, args.line_id, deferred=True, cause=args.cause,
                      reason=args.reason, question=args.question, missing_fields=args.missing_fields)
    if not was_deferred:
        text = f"{raw['product']}：{args.reason}"
        if args.question:
            text += f"\n待补充：{args.question}"
        job.messages = [*job.messages, {"role": "assistant", "text": text,
                                      "at": now().isoformat() + "Z"}][-40:]
    run.usage = {**run.usage, "no_progress_rounds": 0}
    return {"line_id": args.line_id, "product": raw["product"], "deferred": True,
            "continue_other_items": True, "cause": args.cause, "question": args.question,
            "reason": args.reason, "next_action": "本项无金额，继续其他未计价且未暂挂的项目"}


def finish_deferred_quote(db, job, run):
    """Check if all pending items are deferred, and finish if so."""
    lines = [calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
             for raw in job.requirements]
    pending = {line["line_id"] for line in lines if line["amount"] is None or line["blockers"]}
    deferred = {issue["line_id"] for issue in work.deferred_lines(job, run)}
    if pending and pending <= deferred:
        pending_quote(db, job, run, "其余可处理项目已处理；暂挂项目缺少合理计价条件或执行受阻，"
                      "详见逐项问题。已计价小计不是整单总价。", allow_partial=True)
        return True
    return False


def execute_tool(db, job, run, name, args, discovered, interrupted=None):
    """Execute a tool call and return the result."""
    if name == "read_requirement":
        raw = next((r for r in job.requirements if r["id"] == args.line_id), None)
        if raw is None:
            return {"error": "需求不存在"}
        from .customer_excel import MEDIA_TYPE, page_source
        from .customer_files import storage_path
        rows = []
        files = {file.id: file for file in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))}
        locations = {(e["file_id"], e["page"], e.get("cell", "")) for e in raw.get("evidence", [])}
        for file_id, page, cell in sorted(locations):
            file = files.get(file_id)
            if not file or file.media_type != MEDIA_TYPE or not cell:
                continue
            source = page_source(storage_path(file.storage_key), page)
            row_number = int(re.sub(r"\D", "", cell))
            for row in source["rows"]:
                if row["row"] == row_number:
                    entry = {"file_id": file_id, "page": page, "sheet": source["sheet"], **row}
                    if entry not in rows:
                        rows.append(entry)
        return {"requirement": raw, "source_rows": rows,
                "notice": "单元格原文仅为资料，客户原价格不得当作当前报价库单价；"
                          "图纸与单元格冲突须按同一对象同一尺寸语义核对。"}

    if name == "complete_estimate":
        raw = next((r for r in job.requirements if r["id"] == args.line_id), None)
        if raw is None:
            return {"error": "需求不存在"}
        if run.usage.get("durable"):
            existing = calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
            if existing["amount"] is not None:
                return {"error": "该项已有可计价金额，自动续跑不得重做；修改请通过人工审核入口",
                        "already_priced": True, "amount": existing["amount"]}
        for cost in args.estimate.costs:
            if cost.source == "catalog" and (cost.reference_id, cost.reference_revision) not in discovered:
                return {"error": "引用报价库单价前必须先查询该编号及版本"}
        line = RequirementLine.model_validate(raw).model_copy(update={"estimate": args.estimate})
        try:
            validate_estimate_evidence(line, {file.id: file for file in db.scalars(
                select(CustomerFile).where(CustomerFile.job_id == job.id))})
        except ValueError as exc:
            return {"saved": False, "error": str(exc), "next_action": "修正来源，不能伪造文件或单元格"}
        result = calculate_line(db, line, automatic=True)
        if result["blockers"]:
            advice = "修正补全参数或费用方案后再次调用complete_estimate；不能省略该项目"
            repair_fields = []
            if any("缺少" in blocker and ("letter_cm" in blocker or "计价长度" in blocker)
                   for blocker in result["blockers"]):
                advice += ("。letter_cm需要单字billing_length_mm；由你判断并在assumptions中补全该字段，"
                           "说明采用尺寸的依据，不能只填写height_mm；单个字符字组会复用此补全")
                repair_fields.append({"field": "billing_length_mm", "path": "estimate.assumptions",
                                      "unit": "mm", "reason": "缺少单字计价长度，字高不会自动作为计价长度"})
            if any("缺少" in blocker and "billable_area_m2" in blocker for blocker in result["blockers"]):
                advice += ("。billable_area_m2需要单字宽高；单个字符字组会复用行级宽高及其暂定补全，"
                           "多字组不能使用整行宽高代替各字尺寸")
                repair_fields.extend({"field": field, "path": "estimate.assumptions", "unit": "mm",
                                      "reason": "面积需要单字宽高，缺项须有依据地暂估或询问客户"}
                                     for field in ("width_mm", "height_mm"))
            if any("电源" in blocker for blocker in result["blockers"]):
                repair_fields.append({"field": "power_count", "path": "estimate.assumptions",
                                      "reason": "核实或有依据地暂估逐字电源台数，并单列kind=power费用"})
            if any("单价单位" in blocker for blocker in result["blockers"]):
                repair_fields.append({"field": "rate_unit", "path": "estimate.costs",
                                      "reason": "明确单价按厘米/米/平方米/件/套/整项或比例，并核对公式单位"})
            if any("校对金额" in blocker for blocker in result["blockers"]):
                repair_fields.append({"field": "expected_amount", "path": "estimate.costs",
                                      "reason": "独立复核本项费用金额；修正单位、费率或工程量，不能凑数"})
            if any("额外倍数" in blocker or "最低0.1" in blocker for blocker in result["blockers"]):
                repair_fields.append({"field": "quantity_formula", "path": "estimate.costs",
                                      "reason": "去掉重复报价倍数和非外加工最低面积，按实际工程量计算"})
            advice += "；不能合理补全时用defer_requirement记录具体问题并先处理其他项目"
            return {**result, "saved": False, "next_action": advice, "repair_fields": repair_fields,
                    "repair_contract": repair_contract(line, result.get("diagnostics", []))}
        job.requirements = [line.model_dump(mode="json") if r["id"] == line.id else r for r in job.requirements]
        job.revision += 1
        log_change(db, job, "agent_estimate", f"Agent生成待审核补全方案：{line.product}")
        return {**result, "saved": True, "next_action": "继续完成其他项目，最后finish_quote生成总体待审核报价"}

    if name == "search_pricing_cases":
        return search_cases(db, args.query, args.offset)

    if name == "evaluate_pricing_case":
        rule = db.get(PricingRule, args.case_id)
        return evaluate_case(db, rule) if rule else {"error": "案例不存在"}

    if name == "analyze_components":
        raw = next((r for r in job.requirements if r["id"] == args.line_id), None)
        if raw is None:
            return {"error": "需求不存在"}
        analysis = analyze_components(db, RequirementLine.model_validate(raw))
        for component in analysis.get("components", []):
            discovered.update((reference["id"], reference["revision"])
                              for reference in component.get("references", []))
        return {"line_id": args.line_id, **analysis}

    if name == "finish_pending_quote":
        if run.usage.get("durable") and finish_deferred_quote(db, job, run):
            return {"pending": True, "complete": False}
        return pending_quote(db, job, run, args.reason)

    if name == "defer_requirement":
        raw = next((raw for raw in job.requirements if raw["id"] == args.line_id), None)
        if raw is None:
            return {"error": "需求不存在"}
        if calculate_line(db, RequirementLine.model_validate(raw), automatic=True)["amount"] is not None:
            return {"error": "该项已有可计价金额，请保留已完成方案"}
        return defer_requirement(job, run, args)

    if name == "search_prices_semantic":
        job_id, run_id = job.id, run.id

        def check_search():
            if interrupted and interrupted():
                raise Stopped()
            locked(db, job_id, run_id)
            db.commit()

        result = semantic_search.search_prices(db, args.query, args.material, args.status, args.offset,
                                              check=check_search)
        job, run = locked(db, job_id, run_id)
        usage = result.get("embedding_usage", {})
        run.usage = {**run.usage,
                     "embedding_tokens": run.usage.get("embedding_tokens", 0) + usage.get("total_tokens", 0),
                     "embedding_requests": run.usage.get("embedding_requests", 0) + usage.get("requests", 0),
                     "embedding_model": settings().siliconflow_embedding_model}
        discovered.update((price["id"], price["revision"]) for price in result.get("prices", []))
        return result

    if name == "search_prices":
        stmt = select(PriceItem).join(Product)
        if args.status != "all":
            stmt = stmt.where(PriceItem.status == args.status)
        else:
            stmt = stmt.where(PriceItem.status != "inactive")
        for term in re.split(r"\s+", args.query.strip()):
            if term:
                stmt = stmt.where(or_(*(column.icontains(term, autoescape=True) for column in (
                    Product.name, PriceItem.material, PriceItem.spec, PriceItem.process, PriceItem.notes))))
        if args.material:
            stmt = stmt.where(PriceItem.material.contains(args.material, autoescape=True))
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        prices = list(db.scalars(stmt.order_by(PriceItem.id).offset(args.offset).limit(20)))
        discovered.update((p.id, p.revision) for p in prices)
        return {"total": total, "next_offset": args.offset + 20 if args.offset + 20 < total else None,
                "prices": [{**snapshot(p), "product": db.get(Product, p.product_id).name,
                            "source": price_source(db, p)} for p in prices]}

    if name in ("evaluate_price", "select_price"):
        if (args.price_id, args.revision) not in discovered:
            return {"error": "请先查询此价格及版本，不能猜测编号"}
        raw = next((line for line in job.requirements if line["id"] == args.line_id), None)
        price = db.get(PriceItem, args.price_id)
        if not raw or not price:
            return {"error": "需求或价格不存在"}
        if name == "select_price" and run.usage.get("durable"):
            existing = calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
            if existing["amount"] is not None:
                return {"error": "该项已有可计价金额，自动续跑不得重新选价；请继续其他项目",
                        "already_priced": True, "amount": existing["amount"]}
        if raw.get("manual_unit_price") is not None:
            return {"error": "该项目已由人工补价；保留人工输入，使用 calculate_quote 继续"}
        line = RequirementLine.model_validate(raw).model_copy(update={
            "selected_price_id": args.price_id, "selected_price_revision": args.revision,
            "price_review_note": "Agent 自动核对，不代表人工确认"})
        result = calculate_line(db, line, automatic=True)
        if result.get("price") is None:
            result["price"] = {**snapshot(price), "product": db.get(Product, price.product_id).name,
                               "source": price_source(db, price)}
        # Check the entire product, not merely the model's current search page.
        alternatives = []
        for other in db.scalars(select(PriceItem).where(
                PriceItem.product_id == price.product_id, PriceItem.status == "active")):
            candidate = line.model_copy(update={"selected_price_id": other.id,
                                                "selected_price_revision": other.revision})
            if unit_price_usable(calculate_line(db, candidate, automatic=True)):
                alternatives.append(other.id)
                if len(alternatives) >= 2:
                    break
        if len(alternatives) > 1:
            result["blockers"].append("存在多个适用价格，需要确认价格策略，不能自行择价")
            result["amount"] = None
            result["unit_price"] = None
        rules = list(db.scalars(select(PricingRule).where(rule_scope(db, price)).limit(21)))
        result["rules"] = [snapshot(r) for r in rules[:20]]
        result["more_rules"] = len(rules) > 20
        if name == "select_price" and unit_price_usable(result):
            line.price_review_note += "：" + args.reason
            job.requirements = [line.model_dump(mode="json") if r["id"] == line.id else r
                                for r in job.requirements]
            log_change(db, job, "agent_select", f"Agent 关联价格 #{price.id}：{args.reason}")
            result["selected"] = True
        if unit_price_usable(result) and result["blockers"]:
            result["next_action"] = "先关联成品单价，再用complete_estimate暂定数量，costs为空沿用单价；原始数量保持为空，暂定数量计入整单并列入审核。"
        elif result["blockers"]:
            result["next_action"] = "无法直接套价时用complete_estimate记录暂定参数和完整费用方案，继续其余项目，缺项最后集中审核。"
        return result

    if name in ("calculate_quote", "finish_quote"):
        if name == "finish_quote":
            ids = [r.get("selected_price_id") for r in job.requirements if r.get("selected_price_id")]
            list(db.scalars(select(PriceItem).where(PriceItem.id.in_(ids)).order_by(PriceItem.id)
                            .with_for_update().execution_options(populate_existing=True)))
        payload = build_quote(db, job.requirements, automatic=True)
        if job.extraction.get("questions"):
            payload["review_questions"] = job.extraction["questions"]
            payload["requires_review"] = True
        # Recheck ambiguity even after previous selections or a resumed run.
        for result, raw in zip(payload["lines"], job.requirements):
            if not unit_price_usable(result) or result.get("manual_price") or result.get("estimated"):
                continue
            line = RequirementLine.model_validate(raw)
            price = db.get(PriceItem, line.selected_price_id)
            eligible = 0
            for other in db.scalars(select(PriceItem).where(
                    PriceItem.product_id == price.product_id, PriceItem.status == "active")):
                candidate = line.model_copy(update={"selected_price_id": other.id,
                                                    "selected_price_revision": other.revision})
                if unit_price_usable(calculate_line(db, candidate, automatic=True)):
                    eligible += 1
            if eligible > 1:
                result["blockers"].append("存在多个适用价格，需要人工确认")
                result["amount"] = None
                result["unit_price"] = None
        if any(line["blockers"] for line in payload["lines"]):
            payload.update(complete=False, total=None, known_subtotal=str(sum(
                (Decimal(line["amount"]) for line in payload["lines"] if line["amount"] is not None), Decimal(0))))
        if name == "finish_quote" and payload["complete"]:
            db.flush()
            version = (db.scalar(select(func.max(QuoteDraft.version)).where(QuoteDraft.job_id == job.id)) or 0) + 1
            finish(db, job, run, "succeeded",
                   "Agent 已补全并生成总体报价待审核稿，请审核补全清单，可修改后重新计价。"
                   if payload.get("has_estimates") else "Agent 已完成报价草稿，等待人工审核商业条款。")
            db.flush()
            payload.update(title=job.title, customer=job.customer, generated_by="agent", agent_run_id=run.id)
            quote = QuoteDraft(job_id=job.id, version=version, job_revision=job.revision,
                               status="draft", payload=payload,
                               terms=("Agent 自动草稿，非正式对外报价。" + business_rules.TERMS +
                                      "有效期及未确认计价策略待人工审核。") if job.business_rules_version == business_rules.VERSION
                               else "Agent 自动草稿，非正式对外报价。税费、运费、安装费及有效期待人工审核确认。")
            db.add(quote)
            db.flush()
            log_change(db, quote, "agent_calculate", "Agent 工具调用，程序确定性计价")
            return {"quote_id": quote.id, **payload}
        return payload

    if name == "ask_user":
        if run.usage.get("durable"):
            return execute_tool(db, job, run, "defer_requirement", DeferInput(
                line_id=run.usage.get("active_line_id", ""),
                cause="customer_input", reason="关键条件无法合理暂估，需要客户明确",
                question=args.question), discovered, interrupted)
        result = pending_quote(db, job, run, args.question)
        return {**result, "paused": True, "question": args.question}

    raise ValueError("Unsupported tool")
