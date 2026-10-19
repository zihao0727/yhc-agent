"""Recoverable background runner; only a fully priced draft completes the job."""
import logging
import threading
import time
import uuid

import redis
from sqlalchemy import select

from .agent_checkpoints import redis_client, save_checkpoint
from .config import settings
from .database import SessionLocal
from .models import AgentRun, RequirementJob

logger = logging.getLogger(__name__)
RENEW = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('expire',KEYS[1],30) else return 0 end"
RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"


def run_pending_once(stop):
    from .agent_loop import run_agent
    with SessionLocal() as db:
        runs = list(db.scalars(select(AgentRun).where(AgentRun.status == "processing").order_by(AgentRun.id)))
        runs.sort(key=lambda run: (run.usage.get("last_scheduled_at", 0), run.id))
        for run in runs:
            if stop.is_set():
                return
            if not run.usage.get("durable") or run.usage.get("retry_at", 0) > time.time():
                continue
            job = db.get(RequirementJob, run.job_id)
            if job is None or job.status != "processing":
                continue
            client = redis_client()
            key = f"{settings().redis_prefix}:lease:{run.id}"
            owner = uuid.uuid4().hex
            if not client.set(key, owner, nx=True, ex=30):
                continue
            heartbeat_stop = threading.Event()
            lease_lost = threading.Event()

            def heartbeat():
                while not heartbeat_stop.wait(8):
                    try:
                        if not client.eval(RENEW, 1, key, owner):
                            lease_lost.set()
                            return
                        from .worker_health import heartbeat as worker_heartbeat
                        worker_heartbeat(active_run=run.id)
                    except (redis.RedisError, ConnectionError, TimeoutError):
                        lease_lost.set()
                        return

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            def interrupted():
                if stop.is_set() or lease_lost.is_set():
                    return True
                try:
                    return client.get(key) != owner
                except (redis.RedisError, ConnectionError, TimeoutError):
                    return True
            try:
                from .worker_health import heartbeat as worker_heartbeat
                worker_heartbeat(active_run=run.id)
                from .agent_loop import locked
                job, run = locked(db, job.id, run.id)
                run.usage = {**run.usage, "last_scheduled_at": time.time()}
                db.commit()
                # Redis may have missed a write during shutdown. Restore from
                # committed SQL, never the other way around.
                save_checkpoint(db, job, run)
                request = run.usage.get("request", {})
                run_agent(db, job.id, run.id, request.get("supplementary", ""),
                          request.get("replace", False), request.get("intent", "resume"),
                          interrupted=interrupted)
                db.expire_all()
                from .pricing_workflow import sync_work_items
                current_job, current_run = db.get(RequirementJob, job.id), db.get(AgentRun, run.id)
                if current_run.usage.get("workflow_version"):
                    sync_work_items(db, current_job, current_run)
                    db.commit()
                save_checkpoint(db, db.get(RequirementJob, job.id), db.get(AgentRun, run.id))
            finally:
                heartbeat_stop.set()
                thread.join(timeout=10)
                client.eval(RELEASE, 1, key, owner)
            return


def worker_loop(stop):
    next_dependency_check = 0
    while not stop.is_set():
        try:
            from .worker_health import heartbeat as worker_heartbeat, draining
            worker_heartbeat()
            if draining():
                stop.wait(2)
                continue
            if time.monotonic() >= next_dependency_check:
                from .pricing_workflow import reconcile_waiting_jobs
                with SessionLocal() as db:
                    reconcile_waiting_jobs(db)
                    db.commit()
                next_dependency_check = time.monotonic() + settings().agent_dependency_check_seconds
            run_pending_once(stop)
        except (redis.RedisError, ConnectionError) as exc:
            logger.exception("Redis connection failed; will retry: %s", exc)
        except Exception:
            logger.exception("Agent worker checkpoint or execution failed; committed progress retained")
        stop.wait(2)


def start_worker():
    stop = threading.Event()
    thread = threading.Thread(target=worker_loop, args=(stop,), name="quote-worker", daemon=True)
    thread.start()
    return stop, thread


def main():
    import argparse
    import signal
    parser = argparse.ArgumentParser(description="Run the durable quotation worker independently of the API.")
    parser.add_argument("--once", action="store_true", help="Process at most one saved scheduling slice.")
    args = parser.parse_args()
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
    if args.once:
        run_pending_once(stop)
    else:
        worker_loop(stop)


if __name__ == "__main__":
    main()
