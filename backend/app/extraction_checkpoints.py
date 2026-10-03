"""Validated extraction batches in SQL, bound to the exact input and model."""
import hashlib
import json

from .config import settings
from .requirements_schemas import ExtractionResult
from .excel_skills import library_fingerprint

VERSION = 3


def input_signature(files, brief, previous, supplementary):
    payload = {"version": VERSION, "skills": library_fingerprint(), "model": settings().deepseek_model,
               "endpoint": settings().deepseek_base_url,
               "files": [(f.id, f.sha256, f.page_count) for f in files],
               "brief": brief, "previous": previous, "supplementary": supplementary}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class ExtractionCheckpoint:
    def __init__(self, signature, saved, persist, check):
        self.signature = signature
        self.batches = dict(saved.get("batches", {})) if saved.get("signature") == signature else {}
        self.plans = dict(saved.get("plans", {})) if saved.get("signature") == signature else {}
        self.persist = persist
        self.check = check

    def load(self, key):
        self.check()
        raw = self.batches.get(key)
        return ExtractionResult.model_validate(raw) if raw is not None else None

    def save(self, key, result, usage):
        self.check()
        self.batches[key] = result.model_dump(mode="json")
        self.persist({"signature": self.signature, "batches": self.batches, "plans": self.plans}, usage)

    def load_plan(self, file_id):
        self.check()
        return self.plans.get(str(file_id))

    def save_plan(self, file_id, plan, usage):
        self.check()
        self.plans[str(file_id)] = plan
        self.persist({"signature": self.signature, "batches": self.batches, "plans": self.plans}, usage)
