"""Redis progress mirrors committed SQL facts; SQL repairs interrupted dual writes."""
import json
import time
from decimal import Decimal

import redis

from .config import settings
from .quote_engine import calculate_line
from .requirements_schemas import RequirementLine

PUBLISH = """
local old = redis.call('get', KEYS[1])
if old then
  local raw = redis.call('hget', old, 'metadata')
  if raw then
    local current = cjson.decode(raw)
    if tonumber(current.revision) > tonumber(ARGV[1])
       or tonumber(current.run_id) > tonumber(ARGV[2]) then return 0 end
  end
end
redis.call('set', KEYS[1], KEYS[2])
return 1
"""


def redis_client():
    return redis.Redis.from_url(settings().redis_url, decode_responses=True,
                               socket_connect_timeout=3, socket_timeout=5)


def checkpoint_key(job_id):
    return f"{settings().redis_prefix}:job:{job_id}"


def save_checkpoint(db, job, run):
    lines = [calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
             for raw in job.requirements]
    amounts = [Decimal(line["amount"]) for line in lines if line["amount"] is not None]
    metadata = {
        "job_id": job.id, "run_id": run.id, "revision": job.revision,
        "status": run.status, "priced_count": len(amounts), "line_count": len(lines),
        "known_subtotal": str(sum(amounts, Decimal(0))),
        "complete": bool(lines) and all(line["amount"] is not None and not line["blockers"] for line in lines),
        "line_ids": [line["line_id"] for line in lines], "saved_at": time.time(),
    }
    values = {"metadata": json.dumps(metadata, ensure_ascii=False)}
    values.update({f"line:{line['line_id']}": json.dumps(line, ensure_ascii=False) for line in lines})
    # Version-specific snapshots cannot overwrite a newer human edit.
    key = f"{checkpoint_key(job.id)}:revision:{job.revision}"
    with redis_client().pipeline(transaction=True) as pipeline:
        pipeline.hset(key, mapping=values)
        pipeline.eval(PUBLISH, 2, checkpoint_key(job.id), key, job.revision, run.id)
        pipeline.execute()
    return metadata


def read_checkpoint(job_id):
    client = redis_client()
    key = client.get(checkpoint_key(job_id))
    raw = client.hget(key, "metadata") if key else None
    return json.loads(raw) if raw else None
