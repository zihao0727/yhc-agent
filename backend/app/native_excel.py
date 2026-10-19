"""Read explicit workbook facts once; defer semantic interpretation to pricing."""
import copy
import hashlib
import json
import re
import unicodedata
import uuid
from decimal import Decimal, InvalidOperation

from openpyxl.utils import coordinate_to_tuple

from .config import settings
from .customer_excel import MEDIA_TYPE, financial_summary, table_columns, validate_excel_rows
from .customer_files import storage_path
from .requirements_schemas import DimensionEvidence, Evidence, ExtractedLine, ExtractionResult
from .source_coverage import pending_order_charge, row_kind

VERSION = 6
SEMANTIC_NOTE = "工艺及尺寸原文已保留，计价时按对象解析；有歧义再核对来源"
IMAGE_NOTE = "本项目含图纸，须核对图示后采用相关尺寸或部件"


def execution_only(text):
    from .agent_requests import is_resume_command
    text = re.sub(r"[\s。.!！?？,，;；]+", "", text)
    return not text or is_resume_command(text) or bool(re.fullmatch(
        r"(?:请|帮我|给我|为我|完整|生成|整理|一份|这份|这个|一下|报价单|报价|报个价|计价|计算|明细|吧|谢谢|按规则|自动)+",
        text))


def native_page(file_id, page, source):
    """Return None when row identities or the purpose require interpretation."""
    columns, header = table_columns(source)
    if not {"product", "quantity"} <= columns.keys():
        return None
    headings = source["sheet"] + "\n" + "\n".join(
        c["value"] for r in [*source.get("header_rows", []), *source["rows"]]
        if r["row"] <= header for c in r["cells"])
    if re.search(r"价目表|价格库|供应商报价|材料价格表|参考价格|库存表|库存清单", headings):
        return None
    header_cells = [c for r in source.get("header_rows", []) if r["row"] == header for c in r["cells"]]
    # Duplicate semantic headers cannot be resolved by silently taking the last column.
    names = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", c["value"])).lower() for c in header_cells]
    nonempty_names = [name for name in names if name]
    if len(nonempty_names) != len(set(nonempty_names)):
        return None
    for field in ("product", "quantity"):
        remaining = [cell for cell in header_cells
                     if coordinate_to_tuple(cell["cell"])[1] != columns[field]]
        alternatives, _ = table_columns({"header_rows": [{"row": header, "cells": remaining}]})
        if field in alternatives:
            return None
    direct, dimension_columns = {}, {}
    for cell, name in zip(header_cells, names):
        column = coordinate_to_tuple(cell["cell"])[1]
        if name in {"材质", "材料", "工艺", "制作工艺"}:
            field = "material" if name in {"材质", "材料"} else "process"
            if field in direct:
                return None
            direct[field] = column
        dimension = re.fullmatch(r"(宽度|高度|厚度|深度)\((mm|cm|m)\)", name)
        if dimension:
            field = {"宽度": "width_mm", "高度": "height_mm", "厚度": "thickness_mm", "深度": "depth_mm"}[dimension[1]]
            dimension_columns[field] = (column, dimension[2])
    lines, product_rows, fee_questions = [], set(), []
    for row in source["rows"]:
        if row["row"] > header and pending_order_charge(row, columns):
            raw = "；".join(f'{cell["cell"]}: {cell["value"]}' for cell in row["cells"]
                           if cell["value"].strip())
            fee_questions.append(
                f'文件{file_id} {source["sheet"]} 第{page}页第{row["row"]}行费用待确认（{raw}）；'
                "须在整单报价中确认范围和金额，不得默认免费或已包含")
        if row["row"] <= header or row_kind(row, header, columns):
            continue
        cells = {coordinate_to_tuple(c["cell"])[1]: c for c in row["cells"]}
        name_cell = cells.get(columns["product"])
        if not name_cell or not name_cell["value"].strip():
            # Continuation rows with content may belong to the preceding product.
            meaningful = [c for c in cells.values() if re.search(r"[\w\u4e00-\u9fff]", c["value"])]
            if meaningful:
                return None
            continue
        if name_cell.get("formula"):
            return None
        name = name_cell["value"].strip()
        if financial_summary(name):
            continue
        if re.match(r"^(?:备注|说明|注意)(?:[:：\s]|$)", name):
            return None
        product_rows.add(row["row"])
        evidence = [Evidence(file_id=file_id, page=page, sheet=source["sheet"],
                             cell=name_cell["cell"], text=name[:1500])]
        other_cells = [c for c in cells.values() if c is not name_cell and c["value"].strip()]
        evidence.extend(Evidence(file_id=file_id, page=page, sheet=source["sheet"],
                                 cell=c["cell"], text=c["value"][:1500]) for c in other_cells[:15])
        raw = "\n".join(f'{c["cell"]}: {c["value"]}' for c in row["cells"]
                        if c["value"].strip() or c.get("formula"))
        line = ExtractedLine(
            product=name[:150], source_item=cells.get(columns.get("item"), {}).get("value", "")[:100],
            notes=raw[:4000], evidence=evidence, measurement_version=3, document_role="requirements")
        code_cell = cells.get(columns.get("code"))
        if code_cell and not code_cell.get("formula"):
            line.source_code = code_cell["value"][:100]
            if not any(e.cell == code_cell["cell"] for e in line.evidence):
                line.evidence.append(Evidence(file_id=file_id, page=page, sheet=source["sheet"],
                                              cell=code_cell["cell"], text=code_cell["value"][:1500]))
        for field, column in direct.items():
            cell = cells.get(column)
            if cell and not cell.get("formula"):
                setattr(line, field, cell["value"][:100 if field == "material" else 255])
        for field, (column, unit) in dimension_columns.items():
            cell = cells.get(column)
            if not cell or cell.get("formula") or not cell["value"].strip():
                continue
            try:
                value = Decimal(cell["value"])
                value_mm = value * {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000)}[unit]
                claim = DimensionEvidence(file_id=file_id, page=page, sheet=source["sheet"],
                    cell=cell["cell"], text=cell["value"][:1500], source="text",
                    field=field, value=value, unit=unit,
                    role="plate_thickness" if field == "thickness_mm" else
                         "shell_depth" if field == "depth_mm" else "overall")
                # Validate bounds after unit conversion as well as before.
                parsed = ExtractedLine.model_validate({field: value_mm})
                setattr(line, field, getattr(parsed, field))
                line.dimension_evidence.append(claim)
            except (ValueError, InvalidOperation):
                line.uncertainties.append(f"{cell['cell']}尺寸需核实，未自动采用")
        structured_columns = set(columns.values()) | set(direct.values()) | {
            value[0] for value in dimension_columns.values()}
        descriptive = [c for col, c in cells.items() if col not in structured_columns
                       and re.search(r"[\u4e00-\u9fff]", c["value"])]
        if descriptive:
            line.uncertainties.append(SEMANTIC_NOTE)
        if len(raw) > 4000:
            line.uncertainties.append("原行描述较长，完整资料须通过read_requirement核对")
        images = [image for image in source.get("images", []) if image["row"] == row["row"]]
        if images:
            line.uncertainties.append(IMAGE_NOTE)
            line.evidence.extend(Evidence(file_id=file_id, page=page, sheet=source["sheet"],
                cell=image["cell"], text="原表内嵌图纸待核对", source="image") for image in images[:3])
        lines.append(line)
    if not lines or any(image["row"] not in product_rows for image in source.get("images", [])):
        return None
    result = validate_excel_rows(ExtractionResult(lines=lines), {(file_id, page): source})
    result.questions = list(dict.fromkeys([
        *result.questions, *(question[:500] for question in fee_questions),
        *(str(warning)[:500] for warning in source.get("warnings", []))
    ]))[:30]
    result.summary = "程序读取表格事实；复杂描述与图纸在计价时按需解析"
    return result


def cache_path(file, manifest):
    digest = getattr(file, "sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    # Parser changes invalidate only derived facts, never original uploads.
    fingerprint = hashlib.sha256(json.dumps(
        [VERSION, manifest.get("parser_version"), manifest["pages"]],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    root = settings().storage_dir.resolve() / "_native_excel"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}-{fingerprint}.json"


def file_pages(file):
    from .excel_manifest import load_manifest
    manifest = load_manifest(storage_path(file.storage_key), upgrade=True)
    if manifest.get("upgrade_status") == "requires_reextraction":
        return None, {}
    if re.search(r"价目表|价格库|供应商报价|材料价格表|参考价格|库存表|库存清单",
                 getattr(file, "filename", "")):
        return manifest, {}
    path = cache_path(file, manifest)
    if path and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            def rebind(value):
                if isinstance(value, list):
                    return [rebind(item) for item in value]
                if isinstance(value, dict):
                    return {k: file.id if k == "file_id" else rebind(v) for k, v in value.items()}
                return value
            results = {int(p): ExtractionResult.model_validate(rebind(raw)) if raw is not None else None
                       for p, raw in cached["pages"].items()}
            if set(results) == {page["page"] for page in manifest["pages"]}:
                return manifest, {p: (result, True) for p, result in results.items()}
        except (ValueError, KeyError, TypeError):
            pass
    results = {page["page"]: native_page(file.id, page["page"], page) for page in manifest["pages"]}
    if path:
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"pages": {
            p: result.model_dump(mode="json") if result is not None else None for p, result in results.items()
        }}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    return manifest, {p: (result, False) for p, result in results.items()}


def reuse_batches(result, checkpoint, file_id, page):
    """Retain successful semantic work when continuing a partially extracted page."""
    if not checkpoint:
        return result
    def identity(line):
        return tuple(sorted({(e.file_id, e.page, coordinate_to_tuple(e.cell)[0])
                             for e in line.evidence if e.cell}))
    replacements = {}
    keys = (checkpoint.store.keys | checkpoint.store.source_keys
            if checkpoint.store is not None else checkpoint.batches.keys())
    batches = []
    for key in keys:
        try:
            parsed = json.loads(key)
            if len(parsed) != 3 or parsed[0] != [[file_id, page]]:
                continue
            saved = checkpoint.load(key)
            if saved is None:
                continue
            batches.append((len(parsed[2] or []), key, saved))
        except (ValueError, TypeError, IndexError):
            continue
    # A smaller retried batch supersedes an overlapping parent batch as a unit.
    for _, _, saved in sorted(batches, key=lambda entry: (-entry[0], entry[1])):
        grouped = {}
        for line in saved.lines:
            try:
                grouped.setdefault(identity(line), []).append(line)
            except ValueError:
                continue
        replacements.update(grouped)
    if not replacements:
        return result
    merged = []
    for line in result.lines:
        candidates = replacements.get(identity(line), [])
        # Parent batches may overlap with split children; keep each successful fact once.
        seen = set()
        for saved in candidates or [line]:
            key = saved.model_dump_json()
            if key not in seen:
                merged.append(saved)
                seen.add(key)
    return result.model_copy(update={"lines": merged})


def extract_native(files, brief, previous, supplementary, checkpoint):
    """Only initial extraction uses this route; targeted source inspection stays semantic."""
    from . import deepseek
    if previous or not execution_only(brief) or not execution_only(supplementary):
        return None
    prepared = {}
    for file in files:
        if checkpoint:
            checkpoint.check()
        if file.media_type == MEDIA_TYPE:
            manifest, pages = file_pages(file)
            if manifest is not None:
                plan = checkpoint.load_plan(file.id) if checkpoint else None
                if plan:
                    semantic_sheets = {sheet["sheet"] for sheet in plan.get("sheets", [])
                                       if sheet.get("purpose") == "price_reference"
                                       or sheet.get("layout") not in (None, "row_table")}
                    for source in manifest["pages"]:
                        if source["sheet"] in semantic_sheets:
                            pages[source["page"]] = (None, False)
                prepared[file.id] = (manifest, pages)
    if not any(result is not None for _, pages in prepared.values() for result, _ in pages.values()):
        return None
    lines, questions, coverage, usage, attempts = [], [], [], {}, 0
    for file in files:
        for page in range(1, file.page_count + 1):
            key = json.dumps([[(file.id, page)], None], separators=(",", ":"))
            saved = checkpoint.load(key) if checkpoint else None
            manifest, pages = prepared.get(file.id, (None, {}))
            base, hit = pages.get(page, (None, False))
            if saved is not None:
                result = saved
            elif base is not None:
                result = reuse_batches(copy.deepcopy(base), checkpoint, file.id, page)
                source = next(p for p in manifest["pages"] if p["page"] == page)
                result = validate_excel_rows(result, {(file.id, page): source})
                if checkpoint:
                    checkpoint.report(stage="native_cells", file_id=file.id, page=page,
                        sheet=source["sheet"], row_start=source["row_start"], row_end=source["row_end"],
                        native_cache_hit=hit, extraction_route="native_first")
                    checkpoint.save(key, result, {})
            else:
                # Only ambiguous pages reach the existing semantic/image pipeline.
                try:
                    result, count, tokens = deepseek.extract(
                        [file], brief, previous, supplementary, _page_scope={(file.id, page)},
                        _checkpoint=checkpoint)
                except deepseek.ModelFailure as exc:
                    for field, value in exc.usage.items():
                        usage[field] = usage.get(field, 0) + value
                    raise deepseek.ModelFailure(str(exc), attempts + exc.attempts, usage, exc.diagnostics) from exc
                attempts += count
                for field, value in tokens.items():
                    usage[field] = usage.get(field, 0) + value
            lines.extend(result.lines)
            questions.extend(result.questions)
            coverage.extend(result.source_coverage)
    if len(lines) > settings().max_requirement_lines:
        raise deepseek.ModelFailure("提取明细超过任务上限", diagnostics={"retryable": False})
    seen = set()
    for line in lines:
        key = (line.evidence[0].file_id, line.evidence[0].sheet, line.source_item) if line.evidence and line.source_item else None
        if key and key in seen:
            line.uncertainties = list(dict.fromkeys([*line.uncertainties, "跨页出现相同原始序号，需要核对是否重复"]))[:20]
        if key:
            seen.add(key)
    return ExtractionResult(lines=lines, questions=list(dict.fromkeys(questions))[:30],
                            source_coverage=coverage,
                            summary="表格基础事实由程序读取；复杂说明及图纸按计价需要核对"), attempts, usage
