"""Restore an explicit native Excel schedule without copying external quote prices."""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (AgentRun, CustomerFile, ExtractionBatch, ExtractionRun,
                        QuoteDraft, RequirementJob)
from app.native_excel import file_pages
from app.quote_presentation import sql_progress
from app.quote_safety import physical_rows
from app.estimate_pricing import FIELD_LABELS
from app.requirements_schemas import ExtractedLine, RequirementLine
from app.services import log_change, snapshot


def distinct(values):
    unique = {}
    for value in values:
        unique[json.dumps(value, ensure_ascii=False, sort_keys=True)] = value
    return list(unique.values())


def restore_rows(job, file_id, native_lines):
    original, restored, mapping, missing = job.requirements, [], {}, []
    defaults = ExtractedLine().model_dump(mode="json")
    for native in native_lines:
        facts = native.model_dump(mode="json")
        locations = physical_rows(facts)
        if len(locations) != 1:
            raise ValueError("Native product must refer to one original row")
        location = next(iter(locations))
        candidates = [raw for raw in original if (
            location in physical_rows(raw) or (
                raw.get("document_role") == "requirements" and native.source_code
                and raw.get("source_code") == native.source_code
                and any(e["file_id"] == file_id for key in ("evidence", "quantity_evidence", "dimension_evidence")
                        for e in raw.get(key, []))))]
        protected = [raw for raw in candidates if raw.get("confirmed")
                     or raw.get("manual_unit_price") is not None or raw.get("selected_price_id")]
        if len(protected) > 1:
            raise ValueError(f"{native.source_item}: multiple human-reviewed rows require manual reconciliation")
        planned = [raw for raw in candidates if raw.get("estimate")]
        keeper = (protected or planned or candidates or [None])[0]
        identity = keeper["id"] if keeper else hashlib.sha256(
            f"native-repair:{job.id}:{location}".encode()).hexdigest()[:16]
        raw = RequirementLine(id=identity, business_rules_version=job.business_rules_version,
                              **facts).model_dump(mode="json")
        if protected:
            # Human choices and their facts remain intact; immutable row evidence is still restored.
            raw = copy.deepcopy(protected[0])
        else:
            for field in defaults:
                if field in ("product", "source_item", "source_code", "document_role", "quantity",
                             "quantity_evidence", "source_measurements", "evidence", "notes",
                             "uncertainties", "dimension_evidence"):
                    continue
                values = distinct([candidate[field] for candidate in candidates
                                   if candidate.get(field) != defaults[field] and candidate.get(field) is not None])
                if len(values) == 1:
                    raw[field] = values[0]
                elif len(values) > 1:
                    label = FIELD_LABELS.get(field, {"components": "部件", "letter_groups": "字组"}.get(field, field))
                    raw["uncertainties"].append(f"同一原始行的{label}识别结果冲突，已暂挂，须核图确认")
            if len(planned) == 1:
                for field in ("estimate", "difficulty", "difficulty_note", "length_rounding",
                              "power_mode", "power_count", "power_adjustment", "power_note", "extras"):
                    raw[field] = copy.deepcopy(planned[0][field])
            elif len(planned) > 1:
                raw["uncertainties"].append("同一原始行存在不同暂估方案，已归档待重新核价，未任意采用其中一份")
        evidence = distinct([*facts["evidence"], *[
            e for candidate in candidates for e in candidate.get("evidence", [])
            if e["file_id"] != file_id]])
        raw["evidence"] = evidence[:20]
        raw["uncertainties"] = list(dict.fromkeys([*raw["uncertainties"], *[
            note for candidate in candidates for note in candidate.get("uncertainties", [])]]))[:20]
        if not protected:
            dimensions = distinct([e for candidate in candidates for e in candidate.get("dimension_evidence", [])
                                   if e.get("target", "item") == "item"
                                   or e.get("target_id") in {obj["object_id"] for kind in ("components", "letter_groups")
                                                           for obj in raw[kind]}])
            raw["dimension_evidence"] = dimensions[:20]
        restored.append(RequirementLine.model_validate(raw).model_dump(mode="json"))
        mapping[identity] = [candidate["id"] for candidate in candidates]
        if not candidates:
            missing.append(native.source_item)
    associated = {identity for identities in mapping.values() for identity in identities}
    if any((raw.get("confirmed") or raw.get("manual_unit_price") is not None or raw.get("selected_price_id"))
           and raw["id"] not in associated for raw in original):
        raise ValueError("Unassociated human-reviewed rows must be preserved and reviewed separately")
    return restored, mapping, missing


def repair(job_id, file_id, apply=False):
    with SessionLocal() as db:
        job = db.scalar(select(RequirementJob).where(RequirementJob.id == job_id).with_for_update())
        if not job or job.status == "processing":
            raise ValueError("Job is missing or still running")
        file = db.get(CustomerFile, file_id)
        if not file or file.job_id != job.id:
            raise ValueError("Source file must belong to this job")
        manifest, pages = file_pages(file)
        if not manifest or any(result is None for result, _ in pages.values()):
            raise ValueError("Not every Excel page has an unambiguous native schedule")
        native = [line for result, _ in pages.values() for line in result.lines]
        if not native or any(len(physical_rows(line.model_dump(mode="json"))) != 1 for line in native):
            raise ValueError("Native rows lack unique immutable identities")
        rows, mapping, missing = restore_rows(job, file.id, native)
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Ambiguous source rows would reuse an identity")
        old_progress = sql_progress(db, job)
        report = {"job_id": job.id, "old_revision": job.revision, "old_count": len(job.requirements),
                  "new_count": len(rows), "recovered_items": missing, "mapping": mapping,
                  "source_file_id": file.id, "status": "applied" if apply else "preview"}
        if not apply:
            job.requirements = rows
            progress = sql_progress(db, job)
            print(json.dumps({**report, "priced_count": progress["priced_count"],
                              "known_subtotal": progress["known_subtotal"]}, ensure_ascii=False))
            db.rollback()
            return
        before = copy.deepcopy(snapshot(job))
        runs = list(db.scalars(select(AgentRun).where(AgentRun.job_id == job.id).order_by(AgentRun.id)))
        if any(run.status == "processing" for run in runs):
            raise ValueError("An agent run is still processing")
        batches = list(db.scalars(select(ExtractionBatch).where(
            ExtractionBatch.run_id.in_([run.id for run in runs]))))
        backup = Path(__file__).resolve().parents[2] / ".runtime" / "quote-repairs" / (
            f"job-{job.id}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}.json")
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({
            "job": before, "agent_runs": [snapshot(run) for run in runs],
            "extraction_batches": [snapshot(batch) for batch in batches],
            "extraction_runs": [snapshot(run) for run in db.scalars(select(ExtractionRun).where(
                ExtractionRun.job_id == job.id))],
            "quotes": [snapshot(quote) for quote in db.scalars(select(QuoteDraft).where(
                QuoteDraft.job_id == job.id))],
            "files": [snapshot(f) for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))]
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if json.loads(backup.read_text(encoding="utf-8"))["job"] != before:
            raise ValueError("Backup verification failed")
        report["backup"] = str(backup)
        report["previous_progress"] = {key: old_progress[key] for key in (
            "line_count", "priced_count", "known_subtotal")}
        job.requirements = rows
        audit = copy.deepcopy(job.extraction.get("source_agent") or {})
        identities = ["repair_" + row["id"] for row in rows]
        protected = {identity: row for identity, row in zip(identities, rows)
                     if row.get("estimate") or row.get("confirmed") or row.get("manual_unit_price") is not None
                     or row.get("selected_price_id")}
        audit.update(observation_ids=identities, protected_lines=protected,
                     inventory={"expected_rows": len(rows), "issues": []},
                     reference_observations={raw["id"]: raw for raw in before["requirements"]
                                             if raw.get("document_role") == "drawing"})
        job.extraction = {**job.extraction, "lines": [
            {key: value for key, value in row.items() if key in ExtractedLine.model_fields} for row in rows],
            "source_agent": audit, "inventory_repair": report}
        job.status = "needs_review"
        note = (f"已按原始清单恢复{len(rows)}个产品行，补回{len(missing)}个缺项；"
                "重复观察与图纸观察已归档保留，不相加数量。错误暂估方案由新校验拦截，剩余项待核图核价。")
        from app.models import now
        job.messages = [*job.messages, {"role": "assistant", "text": note,
                                       "at": now().isoformat() + "Z"}][-40:]
        if runs:
            run = runs[-1]
            from app.extraction_checkpoints import input_signature
            files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id)))
            usage = copy.deepcopy(run.usage)
            frozen = copy.deepcopy(usage.get("extraction_input", {}))
            frozen["previous"] = rows
            signature = input_signature(files, frozen.get("brief", job.brief), rows,
                                        frozen.get("supplementary", ""))
            checkpoint = copy.deepcopy(usage.get("extraction_checkpoint", {}))
            state = copy.deepcopy(checkpoint.get("source_agent", {}))
            if state:
                state.update(observations=dict(zip(identities, job.extraction["lines"])), protected=protected,
                             archived={**state.get("archived", {}), **state.get("observations", {})},
                             phase="complete")
                state["decisions"] = [*state.get("decisions", []), {
                    "action": "repair_inventory", "reason": note, "before_backup": str(backup), "mapping": mapping}]
                checkpoint.update(source_agent=state, signature=signature)
                checkpoint["progress"] = {**checkpoint.get("progress", {}), "saved_lines": len(rows),
                                          "observation_ids": identities}
            usage.update(extraction_input=frozen, extraction_signature=signature,
                         extraction_checkpoint=checkpoint, prepared=True, work_state={},
                         active_line_id="", active_product="", inventory_repair=report)
            run.usage = usage
            job.extraction["source_agent"]["input_signature"] = signature
            for batch in batches:
                if batch.run_id == run.id and batch.batch_key == '["source_agent"]':
                    batch.result = copy.deepcopy(job.extraction)
        log_change(db, job, "repair_inventory", note, before)
        db.commit()
        progress = sql_progress(db, job)
        if runs:
            from app.agent_checkpoints import save_checkpoint
            save_checkpoint(db, job, runs[-1])
        print(json.dumps({**report, "revision": job.revision, "priced_count": progress["priced_count"],
                          "known_subtotal": progress["known_subtotal"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    parser.add_argument("--source-file", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    repair(arguments.job, arguments.source_file, arguments.apply)
