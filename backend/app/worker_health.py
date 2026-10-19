"""Worker readiness is separate from API/database liveness."""
import os
import time
import uuid
from datetime import timezone

import redis
from sqlalchemy import inspect, select, func

from .agent_checkpoints import redis_client
from .config import settings
from .models import AgentRun, PricingWorkItem

WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def drain_key():
    return f"{settings().redis_prefix}:worker:draining"


def draining():
    return bool(redis_client().get(drain_key()))


def heartbeat(*, active_run=None, error="", mode=None):
    client = redis_client()
    key = f"{settings().redis_prefix}:workers:{WORKER_ID}"
    client.hset(key, mapping={"worker_id": WORKER_ID, "heartbeat_at": str(time.time()),
                             "active_run": str(active_run or ""), "error": error[:150],
                             "mode": mode or settings().agent_worker_mode})
    client.expire(key, 45)


def worker_status(db):
    current = time.time()
    workers, available, is_draining = [], False, False
    try:
        client = redis_client()
        available = bool(client.ping())
        is_draining = bool(client.get(drain_key()))
        for key in client.scan_iter(match=f"{settings().redis_prefix}:workers:*", count=100):
            state = client.hgetall(key)
            if state and current - float(state.get("heartbeat_at", 0)) < 40:
                workers.append(state)
    except (redis.RedisError, ValueError, TypeError, ConnectionError, TimeoutError):
        available = False
    schema_ready = inspect(db.get_bind()).has_table(PricingWorkItem.__tablename__)
    runs = list(db.scalars(select(AgentRun).where(AgentRun.status == "processing")))
    stalled = []
    for run in runs:
        last_event = run.usage.get("last_event_at") or run.usage.get("last_scheduled_at")
        last_event = last_event or run.created_at.replace(tzinfo=timezone.utc).timestamp()
        if current - last_event > settings().agent_worker_stall_seconds:
            stalled.append({"job_id": run.job_id, "run_id": run.id,
                            "idle_seconds": int(current - last_event)})
    states = dict(db.execute(select(PricingWorkItem.status, func.count())
                            .group_by(PricingWorkItem.status)).all()) if schema_ready else {}
    ready = (not settings().agent_background or (
        available and schema_ready and bool(workers) and not is_draining))
    return {"ready": ready, "redis": available, "schema_ready": schema_ready,
            "mode": settings().agent_worker_mode, "draining": is_draining,
            "workers": workers, "processing_runs": len(runs), "stalled_runs": stalled,
            "line_states": states}
