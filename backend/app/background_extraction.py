"""Persist recognition progress before yielding the background worker lease."""
import copy
import hashlib
import json
import time

from sqlalchemy import select

from .config import settings
from .agent_requests import is_resume_command
from .extraction_checkpoints import ExtractionCheckpoint, ExtractionYield, SQLBatchStore, input_signature
from .models import AgentRun
from .requirements_schemas import RequirementLine


def requirement_identity(raw):
    return (raw.get("source_item", ""), raw.get("product", ""), tuple(sorted({
        (e["file_id"], e["page"], e.get("sheet", ""), e.get("cell", ""))
        for e in raw.get("evidence", [])})))


def excel_row_keys(batch_key, rows_by_page):
    parsed = json.loads(batch_key)
    if not isinstance(parsed[0], list) or parsed[1] is not None:
        return set()
    result = set()
    for file_id, page in parsed[0]:
        available = rows_by_page.get((file_id, page), set())
        rows = available.intersection(parsed[2]) if len(parsed) == 3 else available
        result.update(f"{file_id}:{page}:{row}" for row in rows)
    return result


def extract_in_background(db, job, run, files, brief, previous, supplementary, interrupted):
    from . import deepseek
    from .customer_excel import MEDIA_TYPE
    from .customer_files import storage_path
    from .excel_manifest import load_manifest
    from .agent_execution import Stopped, locked
    started = time.monotonic()
    usage_before = {field: run.usage.get(field, 0)
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens")}
    saved_batches = 0
    previous_source_audit = job.extraction.get("source_agent") or {}
    source_run_id = run.usage.get("batch_source_run_id")
    frozen = run.usage.get("extraction_input")
    if frozen is None:
        earlier = db.scalar(select(AgentRun).where(
            AgentRun.job_id == job.id, AgentRun.id < run.id).order_by(AgentRun.id.desc()).limit(1))
        candidate = earlier.usage.get("extraction_input") if earlier else None
        if candidate and input_signature(files, brief, previous, supplementary) == earlier.usage.get("extraction_signature"):
            frozen, source_run_id = candidate, earlier.id
        source_audit = job.extraction.get("source_agent") or {}
        open_sources = (job.extraction.get("incomplete") or source_audit.get("unreviewed_pages")
                        or source_audit.get("inventory", {}).get("issues"))
        if open_sources and candidate:
            expected = input_signature(files, candidate["brief"], candidate["previous"], candidate["supplementary"])
            if expected == (job.extraction.get("signature") or source_audit.get("input_signature")) and (
                    not supplementary.strip() or is_resume_command(supplementary)):
                frozen, source_run_id = candidate, earlier.id
        if frozen is None:
            frozen = {"brief": brief, "previous": previous, "supplementary": supplementary}
        signature = input_signature(files, frozen["brief"], frozen["previous"], frozen["supplementary"])
        run.usage = {**run.usage, "extraction_input": frozen, "extraction_signature": signature,
                     "batch_source_run_id": source_run_id}
        db.commit()
    signature = run.usage["extraction_signature"]
    has_visual = any(file.media_type == "application/pdf" or (
        file.media_type.startswith("image/") and settings().visual_region_audit) for file in files)
    if (has_visual or settings().source_agent_enabled) and (
            not job.extraction.get("incomplete") or job.extraction.get("signature") != signature):
        job.extraction = {**job.extraction, "incomplete": True, "signature": signature,
                          "partial_started": False,
                          "source_agent": {"mode": "ai", "phase": "reading"} if settings().source_agent_enabled
                          else job.extraction.get("source_agent")}
        db.commit()
    cached = run.usage.get("extraction_checkpoint", {})
    if not cached and source_run_id:
        source_run = db.get(AgentRun, source_run_id)
        cached = copy.deepcopy(source_run.usage.get("extraction_checkpoint", {}))
        for recovery in cached.get("recoveries", {}).values():
            # An explicit new run can retry the saved candidate; worker turns keep their budget.
            recovery["rounds"] = 0
        if cached.get("source_agent"):
            state = cached["source_agent"]
            state["budget_start_tokens"] = state.get("total_tokens", 0)
            state["budget_start_turns"] = state.get("turns", 0)
            if not state.get("response_recovery"):
                last_source_event = next((step for step in reversed(source_run.steps)
                                          if step["tool"].startswith("source_agent.")), {})
                diagnostic = last_source_event.get("result", {}).get("diagnostics", {})
                if last_source_event.get("tool") == "source_agent.response_failed" and diagnostic.get("reason"):
                    state["response_recovery"] = {
                        "active": True, "diagnostics": diagnostic, "executed_calls": 0,
                        "next_action": "上次响应整轮未执行；先按单工具、单项目恢复，保留已保存内容"}
            if state.get("response_recovery"):
                state["response_recovery"]["consecutive_failures"] = 0
            if state.get("phase") == "complete" and previous_source_audit.get("unreviewed_pages"):
                state["phase"] = "review"
    store = SQLBatchStore(db, run.id, source_run_id) if has_visual else None
    job_id, run_id = job.id, run.id
    total_pages = sum(file.page_count for file in files)
    progress = dict(cached.get("progress", {}))
    completed_pages = set(progress.get("completed_page_keys", []))
    saved_line_count = progress.get("saved_lines", 0)
    rows_by_page = {}
    for file in files:
        if file.media_type == MEDIA_TYPE and not settings().source_agent_enabled:
            manifest = load_manifest(storage_path(file.storage_key), upgrade=True)
            rows_by_page.update({(file.id, page["page"]): {row["row"] for row in page["rows"]}
                                 for page in manifest["pages"]})
    completed_rows = set(progress.get("completed_row_keys", []))
    for key in cached.get("batches", {}):
        completed_rows.update(excel_row_keys(key, rows_by_page))
    total_rows = sum(len(rows) for rows in rows_by_page.values())
    only_excel = bool(files) and all(file.media_type == MEDIA_TYPE for file in files)

    def check():
        if interrupted and interrupted():
            raise Stopped()
        locked(db, job_id, run_id)
        db.commit()

    def persist(snapshot, tokens):
        nonlocal saved_batches, saved_line_count
        current_job, current_run = locked(db, job_id, run_id)
        from .agent_execution import event
        previous_plans = current_run.usage.get("extraction_checkpoint", {}).get("plans", {})
        for file_id, plan in snapshot.get("plans", {}).items():
            if plan == previous_plans.get(file_id):
                continue
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
        saved = checkpoint.last_saved
        recovery_event = checkpoint.recovery_event
        if recovery_event:
            event(current_run, recovery_event["tool"], {}, recovery_event["result"])
        if saved:
            saved_batches += 1
            key, result = saved
            parsed = json.loads(key)
            completed_rows.update(excel_row_keys(key, rows_by_page))
            is_agent = parsed[0] == "source_agent"
            is_page = parsed[0] in ("pdf_page", "image_page") if isinstance(parsed[0], str) else (
                len(parsed) == 2 and parsed[1] is None and len(parsed[0]) == 1)
            if is_agent:
                before_requirements = current_job.requirements
                before_audit = current_job.extraction.get("source_agent") or {}
                observation_ids = result.source_agent["observation_ids"]
                current_job.requirements = [
                    RequirementLine.model_validate(result.source_agent["protected_lines"][identifier]).model_dump(mode="json")
                    if identifier in result.source_agent.get("protected_lines", {}) else RequirementLine(
                        id=hashlib.sha256(f"{signature}:{identifier}".encode()).hexdigest()[:16],
                        business_rules_version=current_job.business_rules_version,
                        **line.model_dump()).model_dump(mode="json")
                    for identifier, line in zip(observation_ids, result.lines, strict=True)]
                saved_line_count = len(current_job.requirements)
                current_job.extraction = {**result.model_dump(mode="json"), "incomplete": True,
                    "signature": signature, "partial_started": True}
                if (before_requirements != current_job.requirements or any(
                        before_audit.get(key, []) != result.source_agent.get(key, [])
                        for key in ("source_notes", "page_reviews"))):
                    current_run.usage = {**current_run.usage, "retry_count": 0, "retry_at": 0}
            elif is_page and key not in completed_pages:
                if not current_job.extraction.get("partial_started") or current_job.extraction.get("signature") != signature:
                    current_job.requirements = []
                rows = []
                for index, line in enumerate(result.lines):
                    stable_id = hashlib.sha256(f"{signature}:{key}:{index}".encode()).hexdigest()[:16]
                    rows.append(RequirementLine(id=stable_id, business_rules_version=current_job.business_rules_version,
                                                **line.model_dump()).model_dump(mode="json"))
                current_job.requirements = [*current_job.requirements, *rows]
                saved_line_count = len(current_job.requirements)
                completed_pages.add(key)
                current_job.extraction = {"incomplete": True, "signature": signature,
                                         "partial_started": True,
                                         "questions": list(dict.fromkeys([
                                             *current_job.extraction.get("questions", []), *result.questions]))[:30]}
                if saved_line_count > settings().max_requirement_lines:
                    db.rollback()
                    raise deepseek.ModelFailure("提取明细超过任务上限，请拆分任务", diagnostics={"retryable": False})
        is_source_agent = snapshot.get("source_agent", {}).get("version") == 1
        agent_progress = dict(checkpoint.progress) if is_source_agent else {}
        checkpoint.progress.update(completed_page_keys=sorted(completed_pages),
                                   completed_pages=len(completed_pages), total_pages=total_pages,
                                   saved_lines=saved_line_count, completed_row_keys=sorted(completed_rows),
                                   completed_rows=len(completed_rows), total_rows=total_rows,
                                   completed_units=len(completed_rows) if only_excel else len(completed_pages),
                                   total_units=total_rows if only_excel else total_pages,
                                   unit="行" if only_excel else "资料段" if rows_by_page else "页")
        if is_source_agent:
            checkpoint.progress.update(
                completed_pages=agent_progress.get("completed_pages", 0),
                reviewed_pages=agent_progress.get("reviewed_pages", 0),
                completed_units=agent_progress.get("completed_pages", 0),
                total_units=total_pages, unit="页", saved_lines=saved_line_count)
        current_file = next((file for file in files if file.id == checkpoint.progress.get("file_id")), None)
        stage = checkpoint.progress.get("stage")
        if is_source_agent:
            detail = agent_progress.get("detail", "AI正在读取原件并比对")
        elif current_file and current_file.media_type == MEDIA_TYPE:
            action = ("程序直接读取单元格" if stage == "native_cells" else
                      "核对内嵌图片" if stage == "recognizing_images" else "模型核对单元格")
            detail = (f'{current_file.filename} · {checkpoint.progress.get("sheet", "")} · '
                      f'第 {checkpoint.progress.get("row_start", 1)}—{checkpoint.progress.get("row_end", 1)} 行，{action}')
        elif current_file and current_file.media_type.startswith("image/"):
            action = ("核查区域漏项" if stage == "checking_regions" else
                      "重读疑似漏项区域" if stage == "repairing_coverage" else
                      "分块识别图片" if settings().visual_region_audit else "识别图片")
            detail = f"{current_file.filename}，{action}"
        elif not current_file:
            detail = "正在分析工作表结构" if only_excel else "正在分析资料结构"
        else:
            action = ("生成预览" if stage == "rendering" else
                      "核查区域漏项" if stage == "checking_regions" else
                      "重读疑似漏项区域" if stage == "repairing_coverage" else "分块识别")
            detail = f"当前第 {checkpoint.progress.get('page', 1)} 页，{action}"
            if stage != "rendering" and checkpoint.progress.get("region_index"):
                detail += (f" · 区域 {checkpoint.progress['region_index']}"
                           f"/{checkpoint.progress.get('total_regions', '?')}")
        checkpoint.progress["detail"] = detail
        metadata = checkpoint.snapshot()
        public = {k: v for k, v in checkpoint.progress.items()
                  if k not in ("completed_page_keys", "completed_row_keys", "observation_ids")}
        current_run.usage = {**current_run.usage, **{
            field: current_run.usage.get(field, 0) + tokens.get(field, 0)
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")},
            "extraction_checkpoint": metadata, "extraction_progress": public,
            "extraction_tokens": current_run.usage.get("extraction_tokens", 0) + tokens.get("total_tokens", 0)}
        current_run.usage = {**current_run.usage, "phase": (
                            "source_review" if snapshot["source_agent"].get("phase") == "review"
                            else "source_reading") if is_source_agent else "recovering" if recovery_event else
                            "rendering" if stage == "rendering" else "extracting"}
        current_run.message = (
            f"正在整理资料：已读取 {public['completed_units']}/{public['total_units']} {public['unit']}，"
            f"已保存 {saved_line_count} 项明细。{detail}。")
        if is_source_agent:
            current_run.message = (
                f"AI正在{'复核' if snapshot['source_agent'].get('phase') == 'review' else '阅读并比对'}资料："
                f"已读 {public['completed_pages']}/{total_pages} 页，"
                f"已复核 {public.get('reviewed_pages', 0)} 页，已保存 {saved_line_count} 项。")
        elif recovery_event:
            current_run.message = "正在自动核对并修复资料识别，已保存的项目保留。"
        db.commit()
        if saved and not is_source_agent and (saved_batches >= settings().extraction_slice_batches
                      or time.monotonic() - started >= settings().agent_slice_seconds):
            raise ExtractionYield()

    checkpoint = ExtractionCheckpoint(signature, cached, persist, check, store=store, recovery_enabled=True)
    checkpoint.allow_yield = bool(run.usage.get("durable"))
    try:
        result, attempts, usage = deepseek.extract(
            files, frozen["brief"], previous if settings().source_agent_enabled else frozen["previous"],
            frozen["supplementary"], None, None, checkpoint)
    except deepseek.ModelFailure as exc:
        current_job, current_run = locked(db, job_id, run_id)
        delta = {field: max(0, usage_before[field] + exc.usage.get(field, 0) - current_run.usage.get(field, 0))
                 for field in usage_before}
        current_run.usage = {**current_run.usage, **{
            field: current_run.usage.get(field, 0) + delta[field] for field in delta},
            "extraction_tokens": current_run.usage.get("extraction_tokens", 0) + delta["total_tokens"]}
        db.commit()
        raise
    return result, attempts, usage
