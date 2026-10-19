"""Exclude only source-verified, untouched Excel headings from quotation work."""
from sqlalchemy import select
from openpyxl.utils import coordinate_to_tuple

from .customer_excel import MEDIA_TYPE, page_source, table_columns
from .customer_files import storage_path
from .models import CustomerFile
from .source_coverage import row_kind


def partition_requirements(db, job):
    if (job.extraction.get("source_agent") or {}).get("mode") == "ai":
        return list(job.requirements), []
    files = {f.id: f for f in db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id))}
    kept, excluded, cache = [], [], {}
    for raw in job.requirements:
        evidence = raw.get("evidence") or []
        protected = (raw.get("confirmed") or raw.get("estimate") or raw.get("selected_price_id")
                     or raw.get("manual_unit_price") is not None or raw.get("extras") or raw.get("text_evidence"))
        locations = {(e["file_id"], e["page"], e.get("sheet", ""), coordinate_to_tuple(e["cell"])[0])
                     for e in evidence if e.get("cell")}
        matched = False
        if not protected and len(locations) == 1 and all(e.get("cell") for e in evidence):
            file_id, page, sheet, row_number = next(iter(locations))
            file = files.get(file_id)
            if file and file.media_type == MEDIA_TYPE and 1 <= page <= file.page_count:
                key = (file_id, page)
                try:
                    if key not in cache:
                        source = page_source(storage_path(file.storage_key), page)
                        cache[key] = (source, *table_columns(source))
                    source, columns, header = cache[key]
                    row = next((r for r in source["rows"] if r["row"] == row_number), None)
                    matched = bool(row and sheet == source["sheet"]
                                   and row_kind(row, header, columns) in ("header", "non_product"))
                except (OSError, ValueError, KeyError, IndexError):
                    matched = False
        if matched:
            excluded.append({"requirement": raw, "file_id": file_id, "page": page,
                             "sheet": sheet, "row": row_number,
                             "reason": "原始单元格核实为标题、说明或汇总行，不作为报价产品"})
        else:
            kept.append(raw)
    return kept, excluded


def exclude_non_products(db, job):
    from .services import log_change, snapshot
    kept, excluded = partition_requirements(db, job)
    if not excluded:
        return []
    before = snapshot(job)
    job.requirements = kept
    job.extraction = {**job.extraction, "excluded_non_products": [
        *job.extraction.get("excluded_non_products", []), *excluded]}
    log_change(db, job, "exclude_summary_rows",
               "按原始Excel单元格排除非产品行；完整原需求保存在extraction.excluded_non_products", before)
    return excluded
