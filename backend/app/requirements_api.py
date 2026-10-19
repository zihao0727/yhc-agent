import csv
import io
import json
import uuid
import time
from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
import redis
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
from .requirements_schemas import ApprovalInput, ModelKeyInput, QuoteInput, RequirementLine, RequirementsUpdate, RunInput, PricingPolicyInput
from . import business_rules
from .customer_excel import MEDIA_TYPE as EXCEL_MEDIA_TYPE
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
    if file.media_type == EXCEL_MEDIA_TYPE:
        try:
            from .excel_manifest import load_manifest
            manifest = load_manifest(storage_path(file.storage_key))
            data["parser_version"] = manifest.get("parser_version", 1)
            data["resource_summary"] = manifest.get("resource_summary")
            data["conversion"] = manifest.get("conversion")
        except (OSError, ValueError):
            data["resource_summary"] = None
            data["parser_version"] = 0
    return data


@router.get("/jobs/{job_id}/file-workspace")
def file_workspace(job_id: int, db: DB):
    from .file_workspace import workspace_info
    job_for(db, job_id)
    files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job_id)))
    result = workspace_info(job_id, files)
    result.pop("extraction_schema", None)
    result.pop("instructions", None)
    return result


@router.get("/jobs/{job_id}/file-workspace/{artifact_id}")
def file_workspace_artifact(job_id: int, artifact_id: str, db: DB, offset: int = Query(default=0, ge=0),
                            section: Literal["result", "script"] = "result"):
    from .file_workspace import ArtifactInput, artifact_page
    job_for(db, job_id)
    try:
        return artifact_page(job_id, ArtifactInput(artifact_id=artifact_id, offset=offset, section=section))
    except ValueError as exc:
        raise HTTPException(404, "工作区记录不存在") from exc


@router.get("/jobs/{job_id}/pipeline-audit")
def pipeline_audit(job_id: int, db: DB):
    from .pipeline_audit import shadow_line, run_metrics
    job = job_for(db, job_id)
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id).order_by(AgentRun.id.desc()).limit(1))
    rows = [shadow_line(db, raw) for raw in job.requirements]
    return {"job_id": job.id, "revision": job.revision, "read_only": True,
            "engine": "measurement.v3", "lines": rows,
            "coverage": job.extraction.get("source_coverage", []),
            "source_links": job.extraction.get("source_links", []),
            "source_agent": job.extraction.get("source_agent"),
            "files": [public_file(f) for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job_id))],
            "metrics": run_metrics(run) if run else None,
            "requires_review": sum(row["status"] != "unchanged" or bool(row.get("review_signals")) for row in rows)}


@router.post("/customer-files/{file_id}/reparse-preview")
def reparse_preview(file_id: int, db: DB):
    import tempfile
    from pathlib import Path
    from .customer_documents import prepare_normalized
    file = db.get(CustomerFile, file_id)
    if not file or file.media_type != EXCEL_MEDIA_TYPE:
        raise HTTPException(404, "Excel文件不存在")
    with tempfile.TemporaryDirectory(prefix="firefly-parse-audit-") as temporary:
        directory = Path(temporary)
        stored = storage_path(file.storage_key)
        source = stored / ("normalized.xlsx" if (stored / "normalized.xlsx").is_file() else "original")
        conversion = (json.loads((stored / "conversion.json").read_text(encoding="utf-8"))
                      if (stored / "conversion.json").is_file() else None)
        prepare_normalized(source.read_bytes(), directory, conversion)
        manifest = json.loads((directory / "excel.json").read_text(encoding="utf-8"))
        return {"read_only": True, "file_id": file.id, "parser_version": manifest["parser_version"],
                "page_count": len(manifest["pages"]), "resource_summary": manifest["resource_summary"],
                "unresolved": [a for page in manifest["pages"] for a in page["resource_audit"]
                               if a["status"] != "resolved"]}


def view_quote(db, quote, job):
    from .quote_presentation import enrich_legacy_issues
    run = db.get(AgentRun, quote.payload["agent_run_id"]) if quote.payload.get("agent_run_id") else None
    return {**snapshot(quote), "outdated": quote_stale(db, quote, job),
            "payload": enrich_legacy_issues(quote.payload, run)}


def public_usage(usage, job=None, run=None):
    data = {key: value for key, value in usage.items()
            if key not in ("extraction_checkpoint", "extraction_input", "work_state",
                           "pricing_request_owner")}
    if job is not None and run is not None:
        from .agent_work_state import deferred_lines, line_state
        from .quote_presentation import pending_inputs
        raw_by_id = {raw["id"]: raw for raw in job.requirements}
        data["deferred_requirements"] = [
            {**issue, "inputs": pending_inputs(raw_by_id[issue["line_id"]],
                                             line_state(job, run, issue["line_id"]))}
            for issue in deferred_lines(job, run)]
    return data


def view_job(db, job, detail=False):
    data = snapshot(job)
    data["line_count"] = len(job.requirements)
    data["file_count"] = db.scalar(select(func.count()).select_from(CustomerFile).where(CustomerFile.job_id == job.id))
    data["pending_count"] = sum(bool(missing_requirements(RequirementLine.model_validate(line))) for line in job.requirements)
    if detail:
        data["requirements"] = [RequirementLine.model_validate(raw).model_dump(mode="json")
                                for raw in job.requirements]
        runs = list(db.scalars(select(AgentRun).where(
            AgentRun.job_id == job.id).order_by(AgentRun.id.desc()).limit(10)))
        data["pricing_progress"] = None
        if settings().agent_background:
            from .agent_checkpoints import read_checkpoint
            from .quote_presentation import compact_progress
            try:
                progress = read_checkpoint(job.id, include_lines=True)
                if (progress and progress["revision"] == job.revision
                        and (not runs or progress.get("run_id") == runs[0].id)):
                    data["pricing_progress"] = compact_progress(progress)
            except (redis.RedisError, ValueError, KeyError, TypeError):
                pass  # SQL remains available while the worker repairs Redis.
        extracting = bool(runs and runs[0].usage.get("extraction_input") and not runs[0].usage.get("prepared"))
        if extracting:
            data["pricing_progress"] = {"line_count": len(job.requirements), "priced_count": 0,
                                        "known_subtotal": "0", "complete": False,
                                        "saved_at": job.updated_at.timestamp(), "lines": []}
        if not extracting and data["pricing_progress"] is None and any(
                raw.get("estimate") or raw.get("manual_unit_price") is not None or raw.get("selected_price_id")
                for raw in job.requirements):
            from .quote_presentation import compact_progress, sql_progress
            data["pricing_progress"] = compact_progress(sql_progress(db, job))
        data["agent_runs"] = [{**snapshot(r), "usage": public_usage(r.usage, job, r)} for r in runs]
        data["files"] = [public_file(f) for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id))]
        data["runs"] = [snapshot(r) for r in db.scalars(select(ExtractionRun).where(ExtractionRun.job_id == job.id).order_by(ExtractionRun.id.desc()).limit(20))]
        data["quotes"] = [view_quote(db, q, job) for q in db.scalars(select(QuoteDraft).where(QuoteDraft.job_id == job.id).order_by(QuoteDraft.version.desc()))]
        from .quote_workspace import build_workspace
        data["quote_workspace"] = build_workspace(db, job, runs[0] if runs else None, data["quotes"])
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
            candidate = read_checkpoint(job.id, include_lines=True)
            if (candidate and candidate["revision"] == job.revision
                    and (not run or candidate.get("run_id") == run.id)):
                progress = candidate
        except (redis.RedisError, ValueError, KeyError, TypeError):
            pass  # Fall back to recomputing from SQL.
    if run and run.usage.get("extraction_input") and not run.usage.get("prepared"):
        progress = {"line_count": len(job.requirements), "priced_count": 0, "known_subtotal": "0",
                    "complete": False, "saved_at": job.updated_at.timestamp(), "lines": []}
    if progress is None:
        from .quote_presentation import sql_progress
        progress = sql_progress(db, job)
    from .quote_presentation import compact_progress
    progress = compact_progress(progress)
    result = {"id": job.id, "revision": job.revision, "status": job.status,
              "messages": [{k: v for k, v in m.items() if k != "tool_audit"} for m in job.messages],
              "pricing_progress": progress, "run": None, "server_time": time.time()}
    from .quote_workspace import build_workspace
    from .quote_presentation import sql_progress
    current_quotes = [view_quote(db, quote, job) for quote in db.scalars(
        select(QuoteDraft).where(QuoteDraft.job_id == job.id).order_by(QuoteDraft.version.desc()).limit(1))]
    result["quote_workspace"] = build_workspace(db, job, run, current_quotes,
                                               sql_progress(db, job) if not job.extraction.get("incomplete") else None)
    if run:
        usage = public_usage(run.usage, job, run)
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


@router.get("/jobs/{job_id}/requirements-page")
def requirements_page(job_id: int, db: DB, offset: int = Query(default=0, ge=0),
                      limit: int = Query(default=50, ge=1, le=100)):
    job = job_for(db, job_id)
    return {"revision": job.revision, "total": len(job.requirements),
            "incomplete": bool(job.extraction.get("incomplete")),
            "lines": job.requirements[offset:offset + limit]}


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


@router.get("/upload-config")
def upload_config():
    config = settings()
    return {"max_files": 6, "max_file_bytes": config.max_upload_bytes,
            "max_pdf_bytes": config.max_pdf_upload_bytes, "max_job_bytes": config.max_job_upload_bytes,
            "max_pages": config.max_job_pages, "max_lines": config.max_requirement_lines}


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


@router.get("/embedding-config")
def embedding_config():
    from .semantic_search import get_key
    return {"configured": bool(get_key()), "model": settings().siliconflow_embedding_model,
            "provider": "硅基流动", "environment_managed": bool(settings().siliconflow_api_key)}


@router.put("/embedding-config")
def configure_embeddings(data: ModelKeyInput):
    from .semantic_search import save_key
    if settings().siliconflow_api_key:
        raise HTTPException(409, "向量密钥由环境变量管理，请修改后端配置")
    save_key(data.api_key)
    return embedding_config()


@router.delete("/embedding-config")
def clear_embeddings():
    from .semantic_search import key_path
    if settings().siliconflow_api_key:
        raise HTTPException(409, "向量密钥由环境变量管理，请修改后端配置")
    key_path().unlink(missing_ok=True)
    return embedding_config()


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
               files: Annotated[list[UploadFile], File()] = [],
               business_rules_version: Annotated[Literal["legacy", "2026-09-23.v1"], Form()] = business_rules.VERSION):
    if not title.strip() or len(files) > 6 or (not files and not brief.strip()):
        raise HTTPException(422, "填写任务名称，并提供需求文字或图片/PDF/XLSX，文件最多 6 个")
    prepared = []
    try:
        total_size, total_pages = 0, 0
        hashes = set()
        for upload in files:
            result = prepare_file(upload.file, upload.filename or "customer-file")
            prepared.append(result)
            total_size += result["byte_size"]
            if result["media_type"] != EXCEL_MEDIA_TYPE:
                total_pages += result["page_count"]
            if total_size > settings().max_job_upload_bytes or total_pages > settings().max_job_pages:
                raise FileRejected(f"任务文件合计不能超过{settings().max_job_upload_bytes // (1024 * 1024)}MB；"
                                   f"图片/PDF合计不能超过{settings().max_job_pages}页（不含Excel自动分页）")
            if result["sha256"] in hashes:
                raise FileRejected("同一任务不能重复上传相同文件")
            hashes.add(result["sha256"])
        job = RequirementJob(title=title.strip(), customer=customer.strip(), brief=brief.strip(),
                             business_rules_version=business_rules_version)
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


@router.put("/jobs/{job_id}/pricing-policy")
def adopt_policy(job_id: int, data: PricingPolicyInput, db: DB):
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    before = snapshot(job)
    job.business_rules_version = business_rules.VERSION
    job.requirements = [{**line, "business_rules_version": business_rules.VERSION, "confirmed": False}
                        for line in job.requirements]
    job.revision += 1
    job.status = "needs_review"
    log_change(db, job, "pricing_policy", data.reason, before)
    db.commit()
    return view_job(db, job, True)


@router.get("/pricing-policy")
def pricing_policy():
    return business_rules.snapshot()


@router.get("/pricing-strategies")
def pricing_strategies():
    from .pricing_strategies import STRATEGIES
    return {"version": "measurement.v2",
            "strategies": [{"key": key, **value} for key, value in STRATEGIES.items()],
            "business_rules": business_rules.snapshot()}


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
        except (redis.RedisError, ConnectionError, TimeoutError) as exc:
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
        size = sum(f.byte_size for f in existing)
        pages = sum(f.page_count for f in existing if f.media_type != EXCEL_MEDIA_TYPE)
        for upload in files:
            item = prepare_file(upload.file, upload.filename or "customer-file")
            prepared.append(item)
            size += item["byte_size"]
            if item["media_type"] != EXCEL_MEDIA_TYPE:
                pages += item["page_count"]
            if item["sha256"] in hashes:
                raise FileRejected("该文件已在当前会话中")
            if size > settings().max_job_upload_bytes or pages > settings().max_job_pages:
                raise FileRejected(f"会话文件合计不能超过{settings().max_job_upload_bytes // (1024 * 1024)}MB；"
                                   f"图片/PDF合计不能超过{settings().max_job_pages}页（不含Excel自动分页）")
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
    if file.media_type == "application/pdf":
        from .customer_files import ensure_pdf_preview
        try:
            path = ensure_pdf_preview(file, page)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, "PDF 当前页无法渲染，请检查原文件") from exc
    if file.media_type == EXCEL_MEDIA_TYPE:
        from .excel_manifest import ensure_preview
        try:
            path = ensure_preview(storage_path(file.storage_key), page)
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(422, "Excel 当前预览无法生成，请检查原文件") from exc
    if not path.exists():
        raise HTTPException(404, "预览文件已丢失")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/customer-files/{file_id}/source/{page}")
def customer_source(file_id: int, page: int, db: DB):
    from .customer_excel import MEDIA_TYPE, page_source
    file = db.get(CustomerFile, file_id)
    if not file or not 1 <= page <= file.page_count or file.media_type != MEDIA_TYPE:
        raise HTTPException(404, "Excel来源页不存在")
    source = page_source(storage_path(file.storage_key), page)
    for image in source["images"]:
        image["url"] = f"/api/customer-files/{file_id}/images/{image['storage']}"
        image.pop("storage")
    return source


@router.get("/customer-files/{file_id}/pages/{page}/region")
def preview_region(file_id: int, page: int, db: DB,
                   left: Annotated[float, Query(ge=0, le=1)],
                   top: Annotated[float, Query(ge=0, le=1)],
                   right: Annotated[float, Query(ge=0, le=1)],
                   bottom: Annotated[float, Query(ge=0, le=1)]):
    file = db.get(CustomerFile, file_id)
    if not file or not 1 <= page <= file.page_count:
        raise HTTPException(404, "文件或页码不存在")
    if file.media_type != "application/pdf" and not file.media_type.startswith("image/"):
        raise HTTPException(422, "仅图片及PDF支持区域预览")
    if right <= left or bottom <= top:
        raise HTTPException(422, "区域坐标顺序无效")
    from .customer_files import visual_region_content
    try:
        _, pixels = visual_region_content(file, page, (left, top, right, bottom))
    except (ValueError, RuntimeError, OSError) as exc:
        raise HTTPException(422, "区域无法读取，请核对原件") from exc
    return Response(pixels, media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/customer-files/{file_id}/images/{name}")
def customer_drawing(file_id: int, name: str, db: DB):
    import re
    from .customer_excel import MEDIA_TYPE
    file = db.get(CustomerFile, file_id)
    if not file or file.media_type != MEDIA_TYPE or not re.fullmatch(r"excel-image-\d+\.png", name):
        raise HTTPException(404, "图纸不存在")
    path = storage_path(file.storage_key) / name
    if not path.is_file():
        raise HTTPException(404, "图纸不存在")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/jobs/{job_id}/extract")
def extract_job(job_id: int, data: RunInput, db: DB):
    if settings().agent_background or settings().source_agent_enabled:
        return agent_job(job_id, data.model_copy(update={
            "intent": "resume" if data.intent == "resume" else "extract"}), db, extraction_only=True)
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    if not data.allow_external_processing:
        raise HTTPException(422, "需确认将客户图片和补充需求发送给 DeepSeek")
    if job.requirements and not data.replace_existing:
        raise HTTPException(409, "重新识别将替换需求并清除价格选择，请明确确认")
    if not deepseek.get_key():
        raise HTTPException(503, "尚未配置 DeepSeek API Key，可先手动整理需求")
    files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id)))
    if sum(f.page_count for f in files if f.media_type != EXCEL_MEDIA_TYPE) > settings().max_job_pages:
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
    job.requirements = [RequirementLine(id=uuid.uuid4().hex[:16], business_rules_version=job.business_rules_version,
                                       **line.model_dump()).model_dump(mode="json")
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
def agent_job(job_id: int, data: RunInput, db: DB, extraction_only: bool = False):
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
        except (redis.RedisError, ConnectionError, TimeoutError):
            raise HTTPException(503, "Redis 未连接，未启动报价；请检查服务后重试") from None
    try:
        intent = resolve_agent_intent(bool(job.requirements), data.supplementary_text,
                                      data.replace_existing, data.intent)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    source_audit = job.extraction.get("source_agent") or {}
    incomplete_resume = bool(job.extraction.get("incomplete") or source_audit.get("unreviewed_pages")
        or source_audit.get("inventory", {}).get("issues")) and intent == "resume"
    if incomplete_resume:
        intent = "extract"
    if intent == "extract" and job.requirements and not data.replace_existing and not incomplete_resume:
        raise HTTPException(409, "补充识别会替换需求，请确认")
    run = AgentRun(job_id=job_id, model=settings().deepseek_model)
    from .pricing_contract import WORKSPACE_VERSION
    run.usage = {"external_processing_authorized": True, "auto_resume": True,
                 "workflow_version": WORKSPACE_VERSION}
    if settings().agent_background:
        run.usage = {**run.usage, "durable": True, "phase": "queued", "request": {
            "supplementary": data.supplementary_text,
            "replace": data.replace_existing if intent == "extract" else False,
            "intent": intent}}
        run.message = "后台资料整理已启动，按页保存识别结果。" if extraction_only else "后台报价已启动，逐批保存资料及计费进度。"
    if extraction_only:
        run.usage = {**(run.usage or {}), "extraction_only": True}
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
    if job.extraction.get("incomplete"):
        raise HTTPException(409, "资料尚未全部识别，请先继续整理剩余页面")
    if len(data.lines) > settings().max_requirement_lines:
        raise HTTPException(422, "明细数量超过任务上限")
    if len({line.id for line in data.lines}) != len(data.lines):
        raise HTTPException(422, "需求行编号不能重复")
    valid_files = {f.id: f for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))}
    excel_sources = {}
    for line in data.lines:
        line.business_rules_version = job.business_rules_version
        from .estimate_evidence import validate_estimate_evidence
        try:
            validate_estimate_evidence(line, valid_files)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        for evidence in [*line.evidence, *line.quantity_evidence, *line.dimension_evidence,
                         *(e for measurement in line.source_measurements for e in measurement.evidence),
                         *(e for component in line.components for e in component.evidence),
                         *(e for requirement in line.production_requirements for e in requirement.evidence),
                         *(e for group in line.letter_groups for e in group.evidence)]:
            if evidence.file_id not in valid_files or evidence.page > valid_files[evidence.file_id].page_count:
                raise HTTPException(422, "需求引用了不属于当前任务的来源")
            from .customer_excel import MEDIA_TYPE, page_source, validate_evidence
            source_file = valid_files[evidence.file_id]
            if source_file.media_type == MEDIA_TYPE:
                key = (evidence.file_id, evidence.page)
                if key not in excel_sources:
                    excel_sources[key] = page_source(storage_path(source_file.storage_key), evidence.page)
                try:
                    validate_evidence(evidence, excel_sources[key])
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
        from .quantity_validation import validate_measurement_sources
        try:
            validate_measurement_sources(line, {key: json.dumps(source, ensure_ascii=False)
                                                for key, source in excel_sources.items()})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    before = snapshot(job)
    from .agent_work_state import preserve_inputs
    preserve_inputs(db, job)
    job.requirements = [line.model_dump(mode="json") for line in data.lines]
    job.status = "ready" if data.lines and all(not missing_requirements(line) for line in data.lines) else "needs_review"
    job.revision += 1
    log_change(db, job, "requirements_update", data.reason, before)
    from .pricing_workflow import queue_changed_job
    if settings().agent_background:
        queue_changed_job(db, job)
    db.commit()
    return view_job(db, job, True)


@router.post("/jobs/{job_id}/conversation")
def converse(job_id: int, data: RunInput, db: DB):
    from .collaborative_quotes import collaborate, save_answer
    job = job_for(db, job_id, True)
    if not data.allow_external_processing:
        raise HTTPException(422, "需授权将需求与报价发送给模型")
    if not data.supplementary_text.strip() or not job.requirements:
        raise HTTPException(422, "请先整理需求并输入问题")
    if job.extraction.get("incomplete"):
        raise HTTPException(409, "资料仍在整理，请待识别完成后补充报价条件")
    if job.status == "processing":
        active = db.scalar(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.status == "processing"))
        if not active or not active.usage.get("prepared"):
            raise HTTPException(409, "正在整理资料，请等待进入报价阶段")
        if sum(bool(m.get("pending_reply")) for m in job.messages) >= 10:
            raise HTTPException(409, "待处理补充较多，请等待已发送的消息处理完成")
        messages = [*job.messages, {"role": "user", "text": data.supplementary_text,
                                   "at": now().isoformat() + "Z", "reply_id": uuid.uuid4().hex,
                                   "pending_reply": True}]
        db.execute(update(RequirementJob).where(RequirementJob.id == job.id).values(messages=messages[-40:]))
        db.commit()
        return {"job": view_job(db, job_for(db, job_id), True), "queued": True,
                "changes": [], "operations": [], "continue_pricing": False}
    check_revision(job, data.revision)
    db.commit()
    try:
        answer, usage = collaborate(db, job, data.supplementary_text)
    except (ValueError, deepseek.ModelFailure) as exc:
        raise HTTPException(422, "回复未通过校验或模型暂不可用，需求未修改，请重试") from exc
    db.expire_all()
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    try:
        result = save_answer(db, job, answer, usage, text=data.supplementary_text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    db.expire_all()
    return {"job": view_job(db, job_for(db, job_id), True), **result}


@router.post("/jobs/{job_id}/changes")
def confirm_changes(job_id: int, data: ConfirmChanges, db: DB):
    from .collaborative_quotes import validate_changes
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    try:
        if not data.changes and not data.operations:
            raise ValueError("没有修改内容")
        lines = validate_changes(db, job, data)
    except ValueError as exc:
        raise HTTPException(422, "修改字段、值或项目编号无效") from exc
    before = snapshot(job)
    from .agent_work_state import preserve_inputs
    preserve_inputs(db, job)
    job.requirements = lines
    job.status = "needs_review"
    job.revision += 1
    job.messages = [*job.messages, {"role": "assistant", "text": "局部需求修改已确认保存，已保留其他项目与费用方案。新报价需要重新审核。",
                                   "at": now().isoformat() + "Z"}][-40:]
    log_change(db, job, "requirement_patch", "用户逐项确认局部修改：" + "；".join(
        f"{change.line_id}.{change.field}={change.value}" for change in data.changes)[:1800], before)
    from .pricing_workflow import queue_changed_job
    if settings().agent_background:
        queue_changed_job(db, job)
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
    if job.extraction.get("incomplete"):
        raise HTTPException(409, "资料尚未全部识别，不能把已保存的部分明细作为完整报价")
    if not job.requirements:
        raise HTTPException(422, "请先整理至少一条需求")
    if job.business_rules_version == business_rules.VERSION:
        try:
            data.terms = business_rules.validate_terms(data.terms)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    has_estimates = any(line.get("estimate") for line in job.requirements)
    payload = build_quote(db, job.requirements, automatic=has_estimates, rounding=data.rounding,
                          extraction=job.extraction)
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
        raise HTTPException(422, "请确认费用包含/排除范围及报价条款")
    if quote.payload.get("has_estimates") and not data.confirmed_estimates:
        raise HTTPException(422, "请逐项审核并明确确认Agent补全参数、估算费用和报价范围")
    from .source_reconciliation import source_review
    if (source_review(job.extraction)["required"] or quote.payload.get("source_review", {}).get("required")
            ) and not data.confirmed_source_review:
        raise HTTPException(422, "请核对未读清区域和跨文件归属，并明确确认资料完整性及订购范围")
    if quote.payload.get("generated_by") == "agent" and not data.terms:
        raise HTTPException(422, "Agent 草稿需补充正式费用范围及有效期条款后批准")
    before = snapshot(quote)
    if data.terms:
        try:
            quote.terms = (business_rules.validate_terms(data.terms)
                           if quote.payload.get("business_rules") else data.terms)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    quote.status, quote.approval_note, quote.approved_at = "approved", data.note, now()
    if data.confirmed_source_review:
        quote.payload = {**quote.payload, "source_review_confirmed": True}
    log_change(db, quote, "approve", data.note, before)
    db.commit()
    return view_quote(db, quote, job)


def csv_safe(value):
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


@router.get("/quotes/{quote_id}/export")
def export_quote(quote_id: int, db: DB, audience: Literal["customer", "review"] = "customer"):
    quote = db.get(QuoteDraft, quote_id)
    if not quote or (audience == "customer" and quote.status != "approved"):
        raise HTTPException(409, "仅已批准的报价可以导出")
    job = job_for(db, quote.job_id)
    if quote_stale(db, quote, job):
        raise HTTPException(409, "需求或价格已变化，请重新计算并批准后导出")
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(["报价编号", f"Q{quote.id:06d}", "版本", quote.version])
    writer.writerow(["项目", csv_safe(quote.payload["title"]), "客户", csv_safe(quote.payload["customer"])])
    writer.writerow(["用途", "内部审核版（不可作为正式报价）" if audience == "review" else "客户报价",
                     "规则版本", csv_safe(quote.payload.get("business_rules", {}).get("version", "legacy"))])
    writer.writerow(["产品", "文字内容", "材质", "规格(mm)", "数量", "金额(CNY)", "类型备注", "工艺难度备注", "计量单位"])
    for line in quote.payload["lines"]:
        requirement = line.get("effective_requirement", line["requirement"])
        writer.writerow([csv_safe(line["product"]), csv_safe(requirement["text_content"]),
                         csv_safe(requirement["material"]),
                         f'{requirement["width_mm"] or ""} x {requirement["height_mm"] or ""}',
                         line.get("billing_quantity", requirement["quantity"]), line["amount"], csv_safe(requirement.get("type_note")),
                         csv_safe(requirement.get("difficulty_note") or
                                  (requirement.get("difficulty") if requirement.get("difficulty") != "unknown" else "未标注")),
                         csv_safe(line.get("billing_unit") or requirement.get("quantity_unit") or "件")])
        if audience == "review":
            import json
            writer.writerow(["计价过程", csv_safe(line.get("formula")), "单价依据", csv_safe(line.get("unit_formula"))])
            writer.writerow(["原始需求", csv_safe(json.dumps(line["requirement"], ensure_ascii=False))])
            for evidence in line["requirement"].get("evidence", []):
                writer.writerow(["需求来源", evidence["file_id"], evidence["page"],
                                 csv_safe(evidence.get("sheet")), csv_safe(evidence.get("cell")), csv_safe(evidence["text"])])
            for cost in line.get("estimate_costs", []):
                writer.writerow(["费用依据", csv_safe(cost["label"]), csv_safe(cost.get("kind")),
                                 csv_safe(cost["source"]), cost.get("reference_id"), cost.get("reference_revision"),
                                 csv_safe(cost.get("resolved_formula")), csv_safe(cost["reason"])])
                writer.writerow(["费用校对", csv_safe(cost.get("rate_unit", "unknown")),
                                 cost.get("expected_amount", ""), csv_safe(cost.get("measurement_note"))])
            if line.get("price"):
                writer.writerow(["价格依据", csv_safe(json.dumps(line["price"], ensure_ascii=False))])
            for item in [*line["blockers"], *line.get("warnings", [])]:
                writer.writerow(["待复核", csv_safe(item)])
            for item in line.get("review_signals", []):
                writer.writerow(["计价风险", csv_safe(item["code"]), csv_safe(item["message"])])
            for item in line.get("estimate_review", []):
                writer.writerow(["采用参数", csv_safe(item.get("path", item["field"])),
                                 csv_safe(item.get("mode", "fill")), csv_safe(item.get("original")),
                                 csv_safe(item.get("value")), csv_safe(item.get("reason"))])
    writer.writerow(["合计(CNY)", quote.payload["total"]])
    if quote.payload.get("rounding") not in (None, "none"):
        writer.writerow(["取整前金额", quote.payload["unrounded_total"], "取整策略", quote.payload["rounding"],
                         "取整调整", quote.payload["rounding_adjustment"]])
    writer.writerow(["报价条款", csv_safe(quote.terms)])
    if quote.payload.get("has_estimates") and (audience == "review" or not quote.payload.get("business_rules")):
        writer.writerow(["已审核的补全与估价清单"])
        for item in quote.payload.get("review_items", []):
            writer.writerow([csv_safe(item["product"]), csv_safe(item["label"]),
                             csv_safe(item.get("value", item.get("amount", ""))),
                             csv_safe(item.get("source", "")), csv_safe(item.get("reason", ""))])
    return Response(out.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="quote-{quote.id}-v{quote.version}-{audience}.csv"',
                             "Cache-Control": "no-store"})
