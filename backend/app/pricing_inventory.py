"""Keep ordered schedule rows separate from unresolved source observations."""
from collections import defaultdict
import re

from sqlalchemy import select

from .customer_excel import MEDIA_TYPE
from .models import CustomerFile
from .native_excel import file_pages
from .quote_safety import physical_rows
from .services import log_change


def protected(raw):
    return bool(raw.get("confirmed") or raw.get("estimate") or raw.get("selected_price_id")
                or raw.get("manual_unit_price") is not None or raw.get("extras"))


def prepare_pricing_inventory(db, job):
    if (job.extraction.get("source_agent") or {}).get("mode") != "ai":
        return None
    schedules = [raw for raw in job.requirements if raw.get("document_role") == "requirements"]
    if not schedules:
        return None
    schedule_files = {e["file_id"] for raw in schedules for e in raw.get("evidence", [])}
    native_rows, identities, schedule_pages = {}, defaultdict(set), set()
    for file in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id)):
        if file.id not in schedule_files or file.media_type != MEDIA_TYPE:
            continue
        _, pages = file_pages(file)
        for page, (result, _) in pages.items():
            if result is None:
                continue
            schedule_pages.add((file.id, page))
            for line in result.lines:
                locations = physical_rows(line.model_dump(mode="json"))
                if len(locations) != 1:
                    continue
                location = next(iter(locations))
                native_rows[location] = line
                identities[file.id, line.source_item, line.source_code, line.product].add(location)
    grouped, other, pending, excluded = defaultdict(list), [], [], []
    for raw in job.requirements:
        if raw.get("document_role") == "drawing" and not protected(raw):
            pending.append(raw)
            continue
        if raw.get("document_role") != "requirements":
            other.append(raw)
            continue
        locations = physical_rows(raw) & native_rows.keys()
        if not locations:
            for file_id in {e["file_id"] for e in raw.get("evidence", [])}:
                locations |= identities.get(
                    (file_id, raw.get("source_item", ""), raw.get("source_code", ""), raw["product"]), set())
        if len(locations) == 1:
            grouped[next(iter(locations))].append(raw)
        elif (not protected(raw) and not locations and not raw.get("quantity")
              and not raw.get("source_measurements") and not raw.get("material")
              and not raw.get("width_mm") and not raw.get("height_mm")
              and re.search(r"待逐行|逐行.*汇总|行汇总观察|续读|页面汇总|阅读占位",
                            raw.get("product", "") + " " + raw.get("source_item", ""))
              and raw.get("evidence") and all(
                  (e["file_id"], e["page"]) in schedule_pages for e in raw["evidence"])):
            excluded.append({"requirement": raw, "reason": "阅读占位或页面汇总观察，没有独立订购行依据"})
        else:
            other.append(raw)
    keep_ids = {raw["id"] for raw in other}
    for location, candidates in grouped.items():
        reviewed = [raw for raw in candidates if protected(raw)]
        if len(reviewed) > 1:
            keep_ids.update(raw["id"] for raw in candidates)
            continue
        keeper = (reviewed or sorted(candidates, key=lambda raw: (
            location not in physical_rows(raw), -len(raw.get("evidence", [])))))[0]
        keep_ids.add(keeper["id"])
        for raw in candidates:
            if raw["id"] != keeper["id"]:
                excluded.append({"requirement": raw, "kept_line_id": keeper["id"],
                                 "reason": "同一原始清单行的重复观察，数量不相加"})
    if not pending and not excluded:
        return None
    audit = job.extraction.get("source_agent") or {}
    references = {**audit.get("reference_observations", {}),
                  **{raw["id"]: raw for raw in pending}}
    previous = job.extraction.get("pricing_inventory") or {}
    archived = {entry["requirement"]["id"]: entry
                for entry in [*previous.get("excluded_observations", []), *excluded]}
    job.requirements = [raw for raw in job.requirements if raw["id"] in keep_ids]
    inventory = {
        "schedule_line_count": len(job.requirements),
        "pending_observation_count": len(references),
        "excluded_observations": list(archived.values()),
        "pending_sources": [{"line_id": raw["id"], "product": raw["product"],
                            "source_code": raw.get("source_code", ""),
                            "sources": list({(e["file_id"], e["page"]): {
                                "file_id": e["file_id"], "page": e["page"]}
                                for e in raw.get("evidence", [])}.values())}
                           for raw in references.values()],
    }
    job.extraction = {**job.extraction, "source_agent": {
        **audit, "reference_observations": references}, "pricing_inventory": inventory}
    log_change(db, job, "prepare_pricing_inventory",
               f"按订购行计价，保留{len(references)}条待归属图纸观察，归档{len(archived)}条重复或阅读占位记录")
    return inventory
