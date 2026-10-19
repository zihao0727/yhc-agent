"""Validated extraction batches in SQL, bound to the exact input and model."""
import hashlib
import json

from .config import settings
from .requirements_schemas import ExtractionResult
from .excel_skills import library_fingerprint

VERSION = 7


class ExtractionYield(Exception):
    """A committed batch is a safe boundary for another worker turn."""


class SQLBatchStore:
    def __init__(self, db, run_id, source_run_id=None):
        from sqlalchemy import select
        from .models import ExtractionBatch
        self.db, self.run_id, self.source_run_id = db, run_id, source_run_id
        self.keys = set(db.scalars(select(ExtractionBatch.batch_key).where(
            ExtractionBatch.run_id == run_id)))
        self.source_keys = set(db.scalars(select(ExtractionBatch.batch_key).where(
            ExtractionBatch.run_id == source_run_id))) if source_run_id else set()

    def load(self, key):
        from sqlalchemy import select
        from .models import ExtractionBatch
        owner = self.run_id if key in self.keys else self.source_run_id
        if owner is None or key not in self.keys | self.source_keys:
            return None
        batch = self.db.scalar(select(ExtractionBatch).where(
            ExtractionBatch.run_id == owner, ExtractionBatch.batch_key == key))
        if owner != self.run_id:
            self.save(key, batch.result, {})
        return batch.result

    def save(self, key, result, usage):
        from .models import ExtractionBatch
        if key not in self.keys:
            self.db.add(ExtractionBatch(run_id=self.run_id, batch_key=key, result=result, usage=usage))
            self.keys.add(key)

    def __len__(self):
        return len(self.keys | self.source_keys)


def input_signature(files, brief, previous, supplementary, *, include_execution_commands=False):
    if not include_execution_commands:
        from .agent_requests import is_resume_command
        brief = "\n".join(line for line in brief.splitlines() if not is_resume_command(line)).strip()
        supplementary = "" if is_resume_command(supplementary) else supplementary
    payload = {"version": VERSION, "skills": library_fingerprint(), "model": settings().deepseek_model,
               "endpoint": settings().deepseek_base_url,
               "pdf_layout": [settings().pdf_region_rows, settings().pdf_region_columns,
                              settings().pdf_max_region_depth],
               "visual_audit": [settings().visual_region_audit, settings().image_tile_pixels],
               "source_agent": settings().source_agent_enabled,
               "files": [(f.id, f.sha256, f.page_count) for f in files],
               "brief": brief, "previous": previous, "supplementary": supplementary}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ExtractionCheckpoint:
    def __init__(self, signature, saved, persist, check, store=None, recovery_enabled=False):
        self.signature = signature
        self.batches = dict(saved.get("batches", {})) if saved.get("signature") == signature else {}
        self.plans = dict(saved.get("plans", {})) if saved.get("signature") == signature else {}
        self.splits = dict(saved.get("splits", {})) if saved.get("signature") == signature else {}
        self.persist = persist
        self.check = check
        self.store = store
        self.progress = dict(saved.get("progress", {})) if saved.get("signature") == signature else {}
        self.last_saved = None
        self.recovery_enabled = recovery_enabled
        self.recoveries = dict(saved.get("recoveries", {})) if saved.get("signature") == signature else {}
        self.recovery_event = None
        self.source_agent = dict(saved.get("source_agent", {})) if saved.get("signature") == signature else {}

    def load(self, key):
        self.check()
        raw = self.store.load(key) if self.store is not None else self.batches.get(key)
        return ExtractionResult.model_validate(raw) if raw is not None else None

    def save(self, key, result, usage):
        self.check()
        raw = result.model_dump(mode="json")
        self.recoveries.pop(key, None)
        if self.store is not None:
            self.store.save(key, raw, usage)
        else:
            self.batches[key] = raw
        self.last_saved = (key, result)
        self.persist(self.snapshot(), usage)
        self.last_saved = None

    def report(self, **progress):
        self.check()
        self.progress.update(progress)
        self.persist(self.snapshot(), {})

    def load_split(self, key):
        self.check()
        return self.splits.get(key)

    def save_split(self, key, rows, usage):
        self.check()
        self.splits[key] = rows
        self.persist(self.snapshot(), usage)

    def snapshot(self):
        return {"signature": self.signature, "batches": self.batches,
                "plans": self.plans, "splits": self.splits, "progress": self.progress,
                "batch_count": len(self.store) if self.store is not None else len(self.batches),
                "recoveries": self.recoveries, "source_agent": self.source_agent}

    def save_source_agent(self, state, usage, tool, detail, result=None):
        self.check()
        self.source_agent = state
        self.recovery_event = {"tool": "source_agent." + tool, "result": detail}
        self.last_saved = ('["source_agent"]', result) if result is not None else None
        try:
            self.persist(self.snapshot(), usage)
        finally:
            self.last_saved = None
            self.recovery_event = None

    def save_recovery(self, key, state, tool, result, usage=None):
        self.check()
        if state is None:
            self.recoveries.pop(key, None)
        else:
            self.recoveries[key] = state
        self.recovery_event = {"tool": tool, "result": result}
        try:
            self.persist(self.snapshot(), dict(usage or {}))
        finally:
            self.recovery_event = None

    def load_plan(self, file_id):
        self.check()
        return self.plans.get(str(file_id))

    def save_plan(self, file_id, plan, usage):
        self.check()
        self.plans[str(file_id)] = plan
        self.persist(self.snapshot(), usage)
