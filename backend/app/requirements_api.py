import csv
import io
import uuid
import time
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from . import deepseek
from .config import settings
from .conversation import ConfirmChanges
from .customer_files import FileRejected, cleanup_file, prepare_file, storage_path
from .database import get_db
from .models import AgentRun, CustomerFile, ExtractionRun, PriceItem, PricingRule, QuoteDraft, RequirementJob, now
from .pricing_scope import rule_scope
from .quote_engine import build_quote, candidate_prices, missing_requirements, quote_stale
from .requirements_schemas import ApprovalInput, ModelKeyInput, QuoteInput, RequirementLine, RequirementsUpdate, RunInput
from .schemas import DeleteInput
from .services import log_change, snapshot

router = APIRouter(prefix="/api")
DB = Annotated[Session, Depends(get_db)]


def job_for(db, job_id, lock=False):
    stmt = select(RequirementJob).where(RequirementJob.id == job_id)
    if lock:
        stmt = stmt.with_for_update()
    job = db.scalar(stmt)
    if not job:
        raise HTTPException(404, "需求任务不存在")
    return job


def check_revision(job, revision):
    if job.revision != revision:
        raise HTTPException(409, "需求版本已变化，请刷新后重试")
    if job.status == "processing":
        raise HTTPException(409, "任务正在识别，请等待完成")


def public_file(file):
    data = snapshot(file)
    data.pop("storage_key")
    return data


def view_quote(db, quote, job):
    return {**snapshot(quote), "outdated": quote_stale(db, quote, job)}


def view_job(db, job, detail=False):
    data = snapshot(job)
    data["line_count"] = len(job.requirements)
    data["file_count"] = db.scalar(select(func.count()).select_from(CustomerFile).where(CustomerFile.job_id == job.id))
    data["pending_count"] = sum(bool(missing_requirements(RequirementLine.model_validate(line))) for line in job.requirements)
    if detail:
        data["pricing_progress"] = None
        if settings().agent_background:
            from .agent_checkpoints import read_checkpoint
            try:
                progress = read_checkpoint(job.id)
                if progress and progress["revision"] == job.revision:
                    data["pricing_progress"] = progress
            except Exception:
                pass  # SQL remains available while the worker repairs Redis.
        data["agent_runs"] = [snapshot(r) for r in db.scalars(select(AgentRun).where(
            AgentRun.job_id == job.id).order_by(AgentRun.id.desc()).limit(10))]
        data["files"] = [public_file(f) for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id))]
        data["runs"] = [snapshot(r) for r in db.scalars(select(ExtractionRun).where(ExtractionRun.job_id == job.id).order_by(ExtractionRun.id.desc()).limit(20))]
        data["quotes"] = [view_quote(db, q, job) for q in db.scalars(select(QuoteDraft).where(QuoteDraft.job_id == job.id).order_by(QuoteDraft.version.desc()))]
    else:
        for key in ("extraction", "requirements", "messages", "brief"):
            data.pop(key)
    return data


@router.get("/jobs/{job_id}/progress")
def job_progress(job_id: int, db: DB, run_id: int = 0, after: int = Query(default=0, ge=0)):
    """Polling does not transfer files, quote versions, or historical tool payloads."""
    from .agent_loop import model_result
    job = job_for(db, job_id)
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id).order_by(AgentRun.id.desc()).limit(1))
    progress = None
    if settings().agent_background:
        from .agent_checkpoints import read_checkpoint
        try:
            candidate = read_checkpoint(job.id)
            if candidate and candidate["revision"] == job.revision:
                progress = candidate
        except Exception:
            pass
    if progress is None:
        from decimal import Decimal
        from .quote_engine import calculate_line
        lines = [calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
                 for raw in job.requirements]
        progress = {"line_count": len(lines),
                    "priced_count": sum(line["amount"] is not None for line in lines),
                    "known_subtotal": str(sum((Decimal(line["amount"]) for line in lines
                                              if line["amount"] is not None), Decimal(0))),
                    "complete": bool(lines) and all(line["amount"] is not None and not line["blockers"] for line in lines),
                    "saved_at": job.updated_at.timestamp()}
    result = {"id": job.id, "revision": job.revision, "status": job.status,
              "pricing_progress": progress, "run": None, "server_time": time.time()}
    if run:
        usage = run.usage
        phase = usage.get("phase", "queued") if run.status == "processing" else run.status
        offset = min(after, len(run.steps)) if run.id == run_id else 0
        latest = run.steps[-1] if run.steps else None
        active_id = latest.get("arguments", {}).get("line_id") if latest else None
        result["run"] = {
            "id": run.id, "status": run.status, "message": run.message,
            "created_at": run.created_at.isoformat() + "Z", "usage": usage,
            "phase": phase, "step_count": len(run.steps), "offset": offset,
            "active_product": usage.get("active_product") or next(
                (line["product"] for line in job.requirements if line["id"] == active_id), ""),
            "steps": [{**step, "arguments": {k: v for k, v in step["arguments"].items()
                                           if k in ("line_id", "query", "price_id", "case_id")},
                       "result": model_result(step["tool"], step["result"])}
                      for step in run.steps[offset:offset + 50]],
        }
    return result


@router.get("/jobs/{job_id}/agent-runs/{run_id}/steps/{index}")
def agent_step(job_id: int, run_id: int, index: int, db: DB):
    run = db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.job_id == job_id))
    if run is None or not 0 <= index < len(run.steps):
        raise HTTPException(404, "执行记录不存在")
    return run.steps[index]


@router.get("/model-config")
def model_config():
    return {"configured": bool(deepseek.get_key()), "model": settings().deepseek_model,
            "provider": "DeepSeek", "environment_managed": bool(settings().deepseek_api_key)}


@router.put("/model-config")
def configure_model(data: ModelKeyInput):
    if settings().deepseek_api_key:
        raise HTTPException(409, "密钥由环境变量管理，请修改后端配置")
    deepseek.save_key(data.api_key)
    return model_config()


@router.delete("/model-config")
def clear_model():
    if settings().deepseek_api_key:
        raise HTTPException(409, "密钥由环境变量管理")
    deepseek.key_path().unlink(missing_ok=True)
    return model_config()


@router.get("/jobs")
def jobs(db: DB, q: str = Query("", max_length=150), page: int = Query(1, ge=1)):
    stmt = select(RequirementJob)
    if q.strip():
        stmt = stmt.where(or_(RequirementJob.title.contains(q.strip(), autoescape=True),
                             RequirementJob.customer.contains(q.strip(), autoescape=True)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    records = db.scalars(stmt.order_by(RequirementJob.updated_at.desc(), RequirementJob.id.desc()).offset((page - 1) * 20).limit(20))
    return {"items": [view_job(db, job) for job in records], "total": total}


@router.post("/jobs", status_code=201)
def create_job(db: DB, title: Annotated[str, Form(max_length=150)],
               customer: Annotated[str, Form(max_length=150)] = "",
               brief: Annotated[str, Form(max_length=8000)] = "",
               files: Annotated[list[UploadFile], File()] = []):
    if not title.strip() or len(files) > 6 or (not files and not brief.strip()):
        raise HTTPException(422, "填写任务名称，并提供需求文字或图片/PDF，文件最多 6 个")
    prepared = []
    try:
        total_size, total_pages = 0, 0
        hashes = set()
        for upload in files:
            data = upload.file.read(settings().max_upload_bytes + 1)
            result = prepare_file(data, upload.filename or "customer-file")
            prepared.append(result)
            total_size += result["byte_size"]
            total_pages += result["page_count"]
            if total_size > 50 * 1024 * 1024 or total_pages > settings().max_job_pages:
                raise FileRejected("任务文件合计不能超过 50MB 或 20 页")
            if result["sha256"] in hashes:
                raise FileRejected("同一任务不能重复上传相同文件")
            hashes.add(result["sha256"])
        job = RequirementJob(title=title.strip(), customer=customer.strip(), brief=brief.strip())
        db.add(job)
        db.flush()
        for item in prepared:
            db.add(CustomerFile(job_id=job.id, **item))
        log_change(db, job, "create", "创建客户需求，文件仅本地保存")
        db.commit()
        return view_job(db, job, True)
    except Exception as exc:
        db.rollback()
        for item in prepared:
            cleanup_file(item["storage_key"])
        if isinstance(exc, FileRejected):
            raise HTTPException(422, str(exc)) from exc
        raise
    finally:
        for upload in files:
            upload.file.close()


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: DB):
    return view_job(db, job_for(db, job_id), True)


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, data: DeleteInput, db: DB):
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    for model in (AgentRun, ExtractionRun):
        if db.scalar(select(model.id).where(model.job_id == job_id, model.status == "processing").limit(1)):
            raise HTTPException(409, "会话仍有运行中的任务，请停止或恢复任务后再删除")
    keys = list(db.scalars(select(CustomerFile.storage_key).where(CustomerFile.job_id == job_id)))
    if settings().agent_background:
        from .agent_checkpoints import checkpoint_key, redis_client
        client = redis_client()
        prefix = checkpoint_key(job_id)
        try:
            cached = list(client.scan_iter(match=f"{prefix}:revision:*"))
            client.delete(prefix, *cached)
        except Exception as exc:
            raise HTTPException(503, "暂时无法清理会话缓存，请稍后重试删除") from exc
    log_change(db, job, "delete", data.reason, snapshot(job))
    for model in (QuoteDraft, AgentRun, ExtractionRun, CustomerFile):
        db.execute(delete(model).where(model.job_id == job_id))
    db.delete(job)
    db.commit()
    for key in keys:
        cleanup_file(key)
    return {"deleted": job_id}


@router.post("/jobs/{job_id}/files")
def append_files(job_id: int, db: DB, revision: Annotated[int, Form()],
                 files: Annotated[list[UploadFile], File()]):
    prepared = []
    try:
        job = job_for(db, job_id, True)
        check_revision(job, revision)
        existing = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job_id)))
        if not files or len(existing) + len(files) > 6:
            raise FileRejected("每个会话累计最多 6 个文件")
        hashes = {f.sha256 for f in existing}
        size, pages = sum(f.byte_size for f in existing), sum(f.page_count for f in existing)
        for upload in files:
            item = prepare_file(upload.file.read(settings().max_upload_bytes + 1), upload.filename or "customer-file")
            prepared.append(item)
            size += item["byte_size"]
            pages += item["page_count"]
            if item["sha256"] in hashes:
                raise FileRejected("该文件已在当前会话中")
            if size > 50 * 1024 * 1024 or pages > settings().max_job_pages:
                raise FileRejected("会话文件合计不能超过 50MB 或 20 页")
            hashes.add(item["sha256"])
        before = snapshot(job)
        added = [CustomerFile(job_id=job_id, **item) for item in prepared]
        db.add_all(added)
        db.flush()
        job.messages = [*job.messages, {"role": "user", "text": "补充资料",
                                       "file_ids": [f.id for f in added], "at": now().isoformat() + "Z"}][-40:]
        job.status = "needs_review"
        # New source material invalidates previous price selections and quotes.
        job.requirements = []
        job.extraction = {}
        log_change(db, job, "append_files", "客户在对话中补充资料，需重新整理需求", before)
        db.commit()
        return view_job(db, job, True)
    except Exception as exc:
        db.rollback()
        for item in prepared:
            cleanup_file(item["storage_key"])
        if isinstance(exc, FileRejected):
            raise HTTPException(422, str(exc)) from exc
        raise
    finally:
        for upload in files:
            upload.file.close()


@router.get("/customer-files/{file_id}/pages/{page}")
def preview(file_id: int, page: int, db: DB):
    file = db.get(CustomerFile, file_id)
    if not file or not 1 <= page <= file.page_count:
        raise HTTPException(404, "文件页不存在")
    path = storage_path(file.storage_key) / f"page-{page}.png"
    if not path.exists():
        raise HTTPException(404, "预览文件已丢失")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/jobs/{job_id}/extract")
def extract_job(job_id: int, data: RunInput, db: DB):
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    if not data.allow_external_processing:
        raise HTTPException(422, "需确认将客户图片和补充需求发送给 DeepSeek")
    if job.requirements and not data.replace_existing:
        raise HTTPException(409, "重新识别将替换需求并清除价格选择，请明确确认")
    if not deepseek.get_key():
        raise HTTPException(503, "尚未配置 DeepSeek API Key，可先手动整理需求")
    files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id)))
    if sum(f.page_count for f in files) > settings().max_job_pages:
        raise HTTPException(422, "文件页数超过限制")
    before = snapshot(job)
    run = ExtractionRun(job_id=job.id, model=settings().deepseek_model, input_revision=job.revision)
    db.add(run)
    job.status = "processing"
    log_change(db, job, "extract_start", "授权向 DeepSeek 发送图片和需求", before)
    db.commit()
    run_id = run.id
    previous = job.requirements
    brief = job.brief + "\n" + "\n".join(m["text"] for m in job.messages[-10:] if m.get("role") == "user")
    try:
        result, attempts, usage = deepseek.extract(files, brief, previous, data.supplementary_text)
    except Exception as exc:
        db.expire_all()
        job = job_for(db, job_id, True)
        run = db.get(ExtractionRun, run_id)
        if run.status != "processing":
            raise HTTPException(409, "此次识别已失效，结果未覆盖当前需求") from exc
        job.status = "failed"
        run.status = "failed"
        run.error = str(exc) if isinstance(exc, deepseek.ModelFailure) else "文件处理或识别发生异常，未覆盖原需求"
        run.attempts = exc.attempts if isinstance(exc, deepseek.ModelFailure) else 0
        run.usage = exc.usage if isinstance(exc, deepseek.ModelFailure) else {}
        run.finished_at = now()
        log_change(db, job, "extract_failed", run.error)
        db.commit()
        raise HTTPException(502, run.error) from exc
    db.expire_all()
    job = job_for(db, job_id, True)
    run = db.get(ExtractionRun, run_id)
    if run.status != "processing":
        raise HTTPException(409, "此次识别已失效，结果未覆盖当前需求")
    job.extraction = result.model_dump(mode="json")
    job.requirements = [RequirementLine(id=uuid.uuid4().hex[:16], **line.model_dump()).model_dump(mode="json")
                        for line in result.lines]
    messages = list(job.messages)
    if data.supplementary_text:
        messages.append({"role": "user", "text": data.supplementary_text, "at": now().isoformat() + "Z"})
    messages.append({"role": "assistant", "text": "\n".join(result.questions) or result.summary,
                     "at": now().isoformat() + "Z"})
    job.messages = messages[-40:]
    job.status = "needs_review"
    run.status = "succeeded"
    run.attempts, run.usage, run.finished_at = attempts, usage, now()
    log_change(db, job, "extract_success", "模型整理完成，所有需求均待人工确认")
    db.commit()
    return view_job(db, job, True)


@router.post("/jobs/{job_id}/recover")
def recover_job(job_id: int, data: DeleteInput, db: DB):
    job = job_for(db, job_id, True)
    if job.revision != data.revision or job.status != "processing":
        raise HTTPException(409, "任务状态已变化")
    agent = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id, AgentRun.status == "processing"))
    if agent:
        if agent.usage.get("durable"):
            raise HTTPException(409, "后台任务会自动恢复；如需中断，请使用停止按钮")
        from .agent_loop import MAX_TOTAL_SECONDS, finish
        if now() - agent.created_at < timedelta(seconds=MAX_TOTAL_SECONDS + settings().deepseek_timeout * 2 + 90):
            raise HTTPException(409, "Agent 仍在允许时间内，可使用停止按钮")
        finish(db, job, agent, "failed", "管理员恢复中断的 Agent 任务")
        db.commit()
        return view_job(db, job, True)
    run = db.scalar(select(ExtractionRun).where(ExtractionRun.job_id == job_id).order_by(ExtractionRun.id.desc()).limit(1))
    if run and now() - run.created_at < timedelta(seconds=settings().deepseek_timeout * 2 + 90):
        raise HTTPException(409, "识别仍在允许时间内，请稍后再试")
    before = snapshot(job)
    job.status = "failed"
    if run:
        run.status, run.error, run.finished_at = "failed", "识别中断，由管理员恢复", now()
    log_change(db, job, "recover", data.reason, before)
    db.commit()
    return view_job(db, job, True)


@router.post("/jobs/{job_id}/agent")
def agent_job(job_id: int, data: RunInput, db: DB):
    from .agent_loop import run_agent
    from .agent_requests import resolve_agent_intent
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    if not data.allow_external_processing:
        raise HTTPException(422, "需授权将客户资料、需求及候选报价库数据发送给 DeepSeek")
    if not deepseek.get_key():
        raise HTTPException(503, "尚未配置 DeepSeek API Key")
    if settings().agent_background:
        from .agent_checkpoints import redis_client
        try:
            redis_client().ping()
        except Exception:
            raise HTTPException(503, "Redis 未连接，未启动报价；请检查服务后重试") from None
    try:
        intent = resolve_agent_intent(bool(job.requirements), data.supplementary_text,
                                      data.replace_existing, data.intent)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if intent == "extract" and job.requirements and not data.replace_existing:
        raise HTTPException(409, "补充识别会替换需求，请确认")
    run = AgentRun(job_id=job_id, model=settings().deepseek_model)
    if settings().agent_background:
        run.usage = {"durable": True, "phase": "queued", "request": {
            "supplementary": data.supplementary_text,
            "replace": data.replace_existing if intent == "extract" else False,
            "intent": intent}}
        run.message = "后台报价已启动，逐项存入Redis，全部计费后提交人工审核。"
    db.add(run)
    job.status = "processing"
    if data.supplementary_text:
        job.messages = [*job.messages, {"role": "user", "text": data.supplementary_text,
                                       "at": now().isoformat() + "Z"}][-40:]
    log_change(db, job, "agent_start",
               f"复用 {len(job.requirements)} 项已保存需求继续报价，不重新识别资料"
               if intent == "resume" else "授权 Agent 整理需求、查询报价库并生成草稿")
    db.commit()
    if settings().agent_background:
        return view_job(db, job, True)
    run_agent(db, job_id, run.id, data.supplementary_text,
              data.replace_existing if intent == "extract" else False, intent)
    db.expire_all()
    return view_job(db, job_for(db, job_id), True)


@router.post("/jobs/{job_id}/agent/stop")
def stop_agent(job_id: int, data: DeleteInput, db: DB):
    from .agent_loop import finish
    job = job_for(db, job_id, True)
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id, AgentRun.status == "processing"))
    if not run:
        raise HTTPException(409, "没有正在执行的 Agent")
    # A stop targets the current run even if polling has not seen its latest revision.
    finish(db, job, run, "cancelled", "管理员停止 Agent，已保存进度；在途模型响应不会覆盖结果。")
    db.commit()
    return view_job(db, job, True)


@router.put("/jobs/{job_id}/requirements")
def update_requirements(job_id: int, data: RequirementsUpdate, db: DB):
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    if len({line.id for line in data.lines}) != len(data.lines):
        raise HTTPException(422, "需求行编号不能重复")
    valid_files = {f.id: f.page_count for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))}
    for line in data.lines:
        for evidence in [*line.evidence, *line.quantity_evidence, *line.dimension_evidence,
                         *(e for component in line.components for e in component.evidence)]:
            if evidence.file_id not in valid_files or evidence.page > valid_files[evidence.file_id]:
                raise HTTPException(422, "需求引用了不属于当前任务的来源")
    before = snapshot(job)
    job.requirements = [line.model_dump(mode="json") for line in data.lines]
    job.status = "ready" if data.lines and all(not missing_requirements(line) for line in data.lines) else "needs_review"
    job.revision += 1
    log_change(db, job, "requirements_update", data.reason, before)
    db.commit()
    return view_job(db, job, True)


@router.post("/jobs/{job_id}/conversation")
def converse(job_id: int, data: RunInput, db: DB):
    from .conversation import propose
    job = job_for(db, job_id)
    check_revision(job, data.revision)
    if not data.allow_external_processing:
        raise HTTPException(422, "需授权将需求与报价发送给模型")
    if not data.supplementary_text.strip() or not job.requirements:
        raise HTTPException(422, "请先整理需求并输入问题")
    quote = db.scalar(select(QuoteDraft).where(QuoteDraft.job_id == job.id).order_by(QuoteDraft.version.desc()).limit(1))
    requirements, history = job.requirements, job.messages
    quote_data = view_quote(db, quote, job) if quote else None
    db.commit()
    try:
        answer, usage = propose(requirements, quote_data, history, data.supplementary_text)
    except (ValueError, deepseek.ModelFailure) as exc:
        raise HTTPException(422, "回复未通过校验或模型暂不可用，需求未修改，请重试") from exc
    db.expire_all()
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    messages = [*job.messages,
                {"role": "user", "text": data.supplementary_text, "at": now().isoformat() + "Z"},
                {"role": "assistant", "text": answer.answer, "at": now().isoformat() + "Z"}][-40:]
    # Conversation text is not a pricing revision: do not invalidate approved quotes.
    db.execute(update(RequirementJob).where(RequirementJob.id == job.id).values(messages=messages))
    log_change(db, job, "conversation", f"只读报价问答，tokens={usage.get('total_tokens', 0)}")
    db.commit()
    db.expire_all()
    return {"job": view_job(db, job_for(db, job_id), True),
            "changes": [change.model_dump() for change in answer.changes]}


@router.post("/jobs/{job_id}/changes")
def confirm_changes(job_id: int, data: ConfirmChanges, db: DB):
    from .conversation import changed_requirements
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    try:
        lines = changed_requirements(job.requirements, data.changes)
    except ValueError as exc:
        raise HTTPException(422, "修改字段、值或项目编号无效") from exc
    before = snapshot(job)
    job.requirements = lines
    job.status = "needs_review"
    job.revision += 1
    job.messages = [*job.messages, {"role": "assistant", "text": "局部需求修改已确认保存，已保留其他项目与费用方案。新报价需要重新审核。",
                                   "at": now().isoformat() + "Z"}][-40:]
    log_change(db, job, "requirement_patch", "用户逐项确认局部修改：" + "；".join(
        f"{change.line_id}.{change.field}={change.value}" for change in data.changes)[:1800], before)
    db.commit()
    return view_job(db, job, True)


@router.get("/jobs/{job_id}/lines/{line_id}/candidates")
def candidates(job_id: int, line_id: str, db: DB, q: str = Query("", max_length=150)):
    job = job_for(db, job_id)
    raw = next((line for line in job.requirements if line["id"] == line_id), None)
    if not raw:
        raise HTTPException(404, "需求行不存在，请先保存")
    line = RequirementLine.model_validate(raw)
    prices = candidate_prices(db, line, q)
    price_items = list(db.scalars(select(PriceItem).where(PriceItem.id.in_([p["id"] for p in prices]))))
    relevant_rules = list(db.scalars(select(PricingRule).where(
        or_(*(rule_scope(db, price) for price in price_items))
    ).order_by(PricingRule.id).limit(250))) if price_items else []
    return {"prices": prices, "rules": [snapshot(r) for r in relevant_rules],
            "warning": "候选匹配不是适用性确认；需核对规格档位、工艺附加费、税费及原始规则"}


@router.post("/jobs/{job_id}/quotes", status_code=201)
def calculate_quote(job_id: int, data: QuoteInput, db: DB):
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    if not job.requirements:
        raise HTTPException(422, "请先整理至少一条需求")
    has_estimates = any(line.get("estimate") for line in job.requirements)
    payload = build_quote(db, job.requirements, automatic=has_estimates)
    payload.update(customer=job.customer, title=job.title)
    if has_estimates:
        payload["generated_by"] = "agent"
    if has_estimates and payload["complete"]:
        job.status = "quoted"
        log_change(db, job, "estimate_recalculate", "按修订后的补全与费用方案重新计算整单报价，待审核")
    version = (db.scalar(select(func.max(QuoteDraft.version)).where(QuoteDraft.job_id == job.id)) or 0) + 1
    quote = QuoteDraft(job_id=job.id, version=version, job_revision=job.revision,
                       status="draft" if payload["complete"] else "blocked", payload=payload, terms=data.terms)
    db.add(quote)
    log_change(db, quote, "calculate", "确定性计算报价草稿")
    db.commit()
    return view_quote(db, quote, job)


@router.post("/quotes/{quote_id}/approve")
def approve_quote(quote_id: int, data: ApprovalInput, db: DB):
    quote = db.scalar(select(QuoteDraft).where(QuoteDraft.id == quote_id).with_for_update())
    if not quote:
        raise HTTPException(404, "报价不存在")
    job = job_for(db, quote.job_id, True)
    if quote.status != "draft" or not quote.payload["complete"]:
        raise HTTPException(409, "仅完整、未批准的报价草稿可以批准")
    if quote_stale(db, quote, job):
        raise HTTPException(409, "需求或价格已变化，请重新计算")
    if not data.confirmed_commercial_terms:
        raise HTTPException(422, "请确认税费、运费、安装费及报价条款")
    if quote.payload.get("has_estimates") and not data.confirmed_estimates:
        raise HTTPException(422, "请逐项审核并明确确认Agent补全参数、估算费用和报价范围")
    if quote.payload.get("generated_by") == "agent" and not data.terms:
        raise HTTPException(422, "Agent 草稿需补充正式税费、运费、安装费及有效期条款后批准")
    before = snapshot(quote)
    if data.terms:
        quote.terms = data.terms
    quote.status, quote.approval_note, quote.approved_at = "approved", data.note, now()
    log_change(db, quote, "approve", data.note, before)
    db.commit()
    return view_quote(db, quote, job)


def csv_safe(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@router.get("/quotes/{quote_id}/export")
def export_quote(quote_id: int, db: DB):
    quote = db.get(QuoteDraft, quote_id)
    if not quote or quote.status != "approved":
        raise HTTPException(409, "仅已批准的报价可以导出")
    job = job_for(db, quote.job_id)
    if quote_stale(db, quote, job):
        raise HTTPException(409, "需求或价格已变化，请重新计算并批准后导出")
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["报价编号", f"Q{quote.id:06d}", "版本", quote.version])
    writer.writerow(["项目", csv_safe(quote.payload["title"]), "客户", csv_safe(quote.payload["customer"])])
    writer.writerow(["产品", "文字内容", "材质", "规格(mm)", "数量", "金额(CNY)"])
    for line in quote.payload["lines"]:
        requirement = line.get("effective_requirement", line["requirement"])
        writer.writerow([csv_safe(line["product"]), csv_safe(requirement["text_content"]),
                         csv_safe(requirement["material"]),
                         f'{requirement["width_mm"] or ""} x {requirement["height_mm"] or ""}',
                         requirement["quantity"], line["amount"]])
    writer.writerow(["合计(CNY)", quote.payload["total"]])
    writer.writerow(["报价条款", csv_safe(quote.terms)])
    if quote.payload.get("has_estimates"):
        writer.writerow(["已审核的补全与估价清单"])
        for item in quote.payload.get("review_items", []):
            writer.writerow([csv_safe(item["product"]), csv_safe(item["label"]),
                             csv_safe(item.get("value", item.get("amount", ""))),
                             csv_safe(item.get("source", "")), csv_safe(item.get("reason", ""))])
    return Response(out.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="quote-{quote.id}-v{quote.version}.csv"',
                             "Cache-Control": "no-store"})
