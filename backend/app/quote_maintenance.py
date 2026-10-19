"""Audited maintenance uses public calculation paths and never patches customer facts."""
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field
from sqlalchemy import select

from .models import AgentRun, PricingWorkItem
from .requirements_schemas import StrictModel, RequirementLine
from .requirements_api import DB, job_for, check_revision, view_job
from . import agent_work_state as work

router = APIRouter(prefix="/api")


class MaintenanceInput(StrictModel):
    revision: int = Field(ge=1)
    action: Literal["revalidate", "refresh_candidates", "retry_failed", "disable_auto_resume"]
    line_ids: list[str] = Field(default_factory=list, max_length=10000)
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/jobs/{job_id}/maintenance")
def maintain(job_id: int, data: MaintenanceInput, db: DB):
    from .quote_engine import calculate_line
    from .pricing_workflow import sync_work_items, queue_changed_job
    from .services import log_change
    job = job_for(db, job_id, True)
    check_revision(job, data.revision)
    raws = {raw["id"]: raw for raw in job.requirements}
    ids = list(dict.fromkeys(data.line_ids or raws))
    if not set(ids) <= raws.keys():
        raise HTTPException(422, "维护操作引用了不存在的项目")
    run = db.scalar(select(AgentRun).where(AgentRun.job_id == job_id).order_by(AgentRun.id.desc()).limit(1))
    report = {"action": data.action, "line_ids": ids, "reopened": []}
    if data.action == "disable_auto_resume":
        if run:
            run.usage = {**run.usage, "auto_resume": False}
    elif data.action == "refresh_candidates":
        from .catalog_search import search_catalog
        report["searches"] = [{
            "line_id": identity, "search": result.audit(),
            "candidates": [{"id": hit.price.id, "revision": hit.price.revision, "product": hit.name}
                           for hit in result.hits[:40]],
        } for identity in ids for result in [search_catalog(db, raw=raws[identity])]]
    elif data.action == "retry_failed":
        if not run or not run.usage.get("external_processing_authorized"):
            raise HTTPException(409, "请先通过继续报价授权后台处理此任务")
        if run.status == "cancelled":
            raise HTTPException(409, "任务已由管理员停止，请通过继续报价明确重新启动")
        if not run.usage.get("auto_resume"):
            run.usage = {**run.usage, "auto_resume": True}
        items = list(db.scalars(select(PricingWorkItem).where(PricingWorkItem.job_id == job_id)))
        if not items:
            sync_work_items(db, job, run)
        retryable = [identity for identity in ids if
                     calculate_line(db, RequirementLine.model_validate(raws[identity]), automatic=True)["amount"] is None
                     and work.line_state(job, run, identity).get("cause") in (
                         "plan_repair", "execution_error", "recognition_review", "service_unavailable")]
        report["reopened"] = queue_changed_job(db, job, force_line_ids=retryable, reason=data.reason)
        if not report["reopened"]:
            report["notice"] = "没有新的可恢复项目；相同输入和依赖已尝试过的故障不会重复付费重跑"
    else:
        if run:
            sync_work_items(db, job, run)
    log_change(db, job, "quote_maintenance", data.reason)
    db.commit()
    return {"job": view_job(db, job, True), "maintenance": report}


class WorkerControl(StrictModel):
    draining: bool
    reason: str = Field(min_length=2, max_length=1000)


@router.post("/operations/worker")
def control_worker(data: WorkerControl, db: DB):
    from .agent_checkpoints import redis_client
    from .worker_health import drain_key, worker_status
    from .models import AuditLog
    import redis
    try:
        client = redis_client()
        if data.draining:
            client.set(drain_key(), "1")
        else:
            client.delete(drain_key())
    except (redis.RedisError, ConnectionError, TimeoutError):
        raise HTTPException(503, "Redis不可用，未改变后台接单状态") from None
    db.add(AuditLog(entity="quote_worker", entity_id=0, action="drain" if data.draining else "resume",
                    reason=data.reason, after={"draining": data.draining}))
    db.commit()
    return worker_status(db)
