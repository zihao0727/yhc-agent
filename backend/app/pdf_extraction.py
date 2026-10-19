"""Bounded page-region recognition with independently recoverable batches."""
import json
from collections import Counter

from .config import settings
from .customer_files import image_regions, pdf_regions, render_pdf_batch
from .requirements_schemas import ExtractionResult


def page_key(file_id, page, image=False):
    return json.dumps(["image_page" if image else "pdf_page", file_id, page], separators=(",", ":"))


def merge_regions(results):
    lines, questions, coverage, summaries = [], [], [], []
    seen = {}
    for result_index, result in enumerate(results):
        questions.extend(result.questions)
        coverage.extend(result.source_coverage)
        summaries.append(result.summary)
        for line in result.lines:
            # No fuzzy name matching: two identical products can be different rows.
            identity = (line.source_code or line.source_item, line.product) if (
                line.source_code or line.source_item) else None
            match = seen.get(identity) if identity else None
            existing = match[0] if match and match[1] != result_index else None
            if not identity and len(results) > 1:
                line.uncertainties = list(dict.fromkeys([
                    *line.uncertainties, "未提供原始项目序号，须核对相邻分块是否重复，不自动合并同名项目"]))[:20]
            if match and match[1] == result_index:
                line.uncertainties = list(dict.fromkeys([
                    *line.uncertainties, "同一分块出现相同项目序号及名称，须核对项目边界"]))[:20]
                match[0].uncertainties = list(dict.fromkeys([
                    *match[0].uncertainties, "同一分块出现相同项目序号及名称，须核对项目边界"]))[:20]
            if existing is None:
                lines.append(line)
                if identity:
                    seen[identity] = (line, result_index)
                continue
            for field in ("evidence", "dimension_evidence", "quantity_evidence",
                          "text_evidence", "uncertainties", "source_measurements"):
                values = getattr(existing, field)
                for value in getattr(line, field):
                    if value not in values and len(values) < (10 if field == "source_measurements" else 20):
                        values.append(value)
            for field in ("quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                          "billing_length_mm", "area_m2", "character_count"):
                old, new = getattr(existing, field), getattr(line, field)
                conflict = f"同页分块的{field}不一致，须核对原图"
                if conflict in existing.uncertainties:
                    continue
                if old is None:
                    setattr(existing, field, new)
                elif new is not None and old != new:
                    setattr(existing, field, None)
                    existing.uncertainties = [*existing.uncertainties, conflict][:20]
            for field in ("material", "process", "notes", "text_content", "pricing_category",
                          "source_code", "document_role"):
                old, new = getattr(existing, field), getattr(line, field)
                if not old or field == "document_role" and old == "unknown":
                    setattr(existing, field, new)
                elif new and old != new:
                    existing.uncertainties = list(dict.fromkeys([
                        *existing.uncertainties, f"同页分块的{field}描述不同，须核对原图"]))[:20]
            for field in ("components", "letter_groups"):
                old, new = getattr(existing, field), getattr(line, field)
                if not old:
                    setattr(existing, field, new)
                elif new and [v.model_dump() for v in old] != [v.model_dump() for v in new]:
                    existing.uncertainties = list(dict.fromkeys([
                        *existing.uncertainties, "同页分块部件或字组不同，须核对项目边界"]))[:20]
    if len(lines) > settings().max_requirement_lines:
        from .deepseek import ModelFailure
        raise ModelFailure("提取明细超过任务上限，请拆分资料", diagnostics={"retryable": False})
    return ExtractionResult(lines=lines, questions=list(dict.fromkeys(questions))[:30],
                            summary="\n".join(filter(None, summaries))[:4000], source_coverage=coverage)


def extract_pdf_page(file, page, brief, previous, supplementary, checkpoint, plans):
    from . import deepseek
    image = file.media_type.startswith("image/")
    key = page_key(file.id, page, image)
    if checkpoint:
        saved = checkpoint.load(key)
        if saved is not None:
            return saved, 0, {}
        checkpoint.report(stage="rendering", file_id=file.id, page=page, total_pages=file.page_count)
    if not image:
        render_pdf_batch(file, page, checkpoint.check if checkpoint else lambda: None)
    regions = image_regions(file) if image else pdf_regions(file, page)
    results, attempts, usage = [], 0, {}

    def recognize(region, depth=0):
        nonlocal attempts
        if checkpoint:
            checkpoint.report(stage="recognizing", file_id=file.id, page=page,
                              region=list(region), total_regions=len(regions))
        try:
            result, count, tokens = deepseek.extract(
                [file], brief, previous, supplementary, {(file.id, page)},
                _checkpoint=checkpoint, _excel_plans=plans, _pdf_region=region)
            attempts += count
            for field, value in tokens.items():
                usage[field] = usage.get(field, 0) + value
            if settings().visual_region_audit:
                from .visual_audit import audit_region
                result, count, tokens = audit_region(
                    file, page, region, result, brief, previous, supplementary, checkpoint, plans)
                attempts += count
                for field, value in tokens.items():
                    usage[field] = usage.get(field, 0) + value
        except deepseek.ModelFailure as exc:
            attempts += exc.attempts
            for field, value in exc.usage.items():
                usage[field] = usage.get(field, 0) + value
            too_dense = exc.diagnostics.get("reason") == "pdf_region_dense" or any(
                failure.get("finish_reason") == "length" for failure in exc.diagnostics.get("failures", []))
            if not too_dense or depth >= settings().pdf_max_region_depth:
                raise deepseek.ModelFailure(str(exc), attempts, usage, exc.diagnostics) from exc
            left, top, right, bottom = region
            if bottom - top >= right - left:
                middle = (top + bottom) / 2
                children = [(left, top, right, middle), (left, middle, right, bottom)]
            else:
                middle = (left + right) / 2
                children = [(left, top, middle, bottom), (middle, top, right, bottom)]
            # Remember the subdivision before requesting either child.
            split_key = json.dumps(["image_split" if image else "pdf_split", file.id, page, region],
                                   separators=(",", ":"))
            if checkpoint:
                checkpoint.save_split(split_key, children, exc.usage)
            return [r for child in children for r in recognize(child, depth + 1)]
        return [result]

    def recognize_saved(region, depth=0):
        split_key = json.dumps(["image_split" if image else "pdf_split", file.id, page, region],
                               separators=(",", ":"))
        children = checkpoint.load_split(split_key) if checkpoint else None
        if children:
            return [r for child in children for r in recognize_saved(tuple(child), depth + 1)]
        return recognize(tuple(region), depth)

    for index, region in enumerate(regions, 1):
        if checkpoint:
            checkpoint.progress["region_index"] = index
        results.extend(recognize_saved(region))
    result = merge_regions(results)
    if settings().visual_region_audit:
        from .requirements_schemas import SourceCoverage
        from .visual_audit import normalized
        unnamed = Counter(normalized(line.product) for line in result.lines
                          if not line.source_item and not line.source_code and line.product)
        for name, count in unnamed.items():
            if count > 1:
                result.source_coverage.append(SourceCoverage(
                    file_id=file.id, page=page, row=0, kind="view", verification="visual",
                    status="unresolved", label=name[:1500],
                    reason="相邻区域出现同名且无编号的项目，须确认独立订购行或重复视图，不可重复计费"))
                result.questions.append(f"文件{file.id}第{page}页：同名无编号项目{name}须核对重复视图"[:500])
        result.questions = list(dict.fromkeys(result.questions))[:30]
    if checkpoint:
        checkpoint.report(stage="saving", file_id=file.id, page=page)
        checkpoint.save(key, result, {})
    return result, attempts, usage
