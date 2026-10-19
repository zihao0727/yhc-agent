"""Reconcile attachments before pricing; filenames and repeated views never add quantity."""
import re
from collections import Counter

from .requirements_schemas import SourceLink
from .visual_audit import normalized, quoted_code


def file_ids(line):
    return {e.file_id for e in line.evidence}


def explicit_code(line):
    code = normalized(line.source_code)
    if not code or len(code) < 2 or not (re.search(r"[a-z]", code) or re.fullmatch(r"\d{6,}", code)):
        return ""
    # Image quotes remain observations; the code must at least be explicitly quoted.
    return code if any(quoted_code(code, e.text) for e in line.evidence) else ""


def combine(base, drawing):
    for evidence in drawing.evidence:
        if evidence not in base.evidence and len(base.evidence) < 20:
            base.evidence.append(evidence)
    for field, limit in (("dimension_evidence", 20), ("quantity_evidence", 20),
                         ("source_measurements", 10), ("text_evidence", 20)):
        values = getattr(base, field)
        for value in getattr(drawing, field):
            if value not in values and len(values) < limit:
                values.append(value)
    conflicts = []
    for field in ("quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                  "billing_length_mm", "area_m2", "character_count"):
        old, new = getattr(base, field), getattr(drawing, field)
        if any("跨文件清单与图纸存在冲突：" in warning and field in warning for warning in base.uncertainties):
            conflicts.append(field)
            setattr(base, field, None)
            continue
        if new is None:
            continue
        if old is None:
            if field == "quantity" and not any(
                e.validation_status == "verified" for e in drawing.quantity_evidence):
                continue
            if field in ("width_mm", "height_mm", "thickness_mm", "depth_mm") and not any(
                e.field == field and e.target == "item" and e.value is not None
                for e in drawing.dimension_evidence):
                continue
            setattr(base, field, new)
        elif old != new:
            conflicts.append(field)
            setattr(base, field, None)
    for field in ("material", "process", "text_content"):
        old, new = getattr(base, field), getattr(drawing, field)
        if not old:
            setattr(base, field, new)
        elif new and normalized(old) != normalized(new):
            conflicts.append(field)
    for field in ("components", "letter_groups"):
        old, new = getattr(base, field), getattr(drawing, field)
        if not old:
            setattr(base, field, new)
        elif new and old != new:
            conflicts.append(field)
    base.notes = "\n".join(dict.fromkeys(filter(None, [base.notes, drawing.notes])))[:4000]
    warning = "跨文件清单与图纸存在冲突：" + "、".join(conflicts) + "；须确认后计价"
    base.uncertainties = list(dict.fromkeys([
        *([warning] if conflicts else []), *base.uncertainties, *drawing.uncertainties]))[:20]
    return conflicts


def reconcile_sources(result, files):
    if not files:
        return result
    result = result.model_copy(deep=True)
    hashes = {file.id: getattr(file, "sha256", "") for file in files}
    lines, seen = [], {}
    for line in result.lines:
        ids = file_ids(line)
        if line.document_role == "price_reference" and ids:
            result.source_links.append(SourceLink(file_ids=sorted(ids), product=line.product,
                status="reference", reason="单价参考资料保留原件和来源，不作为客户订购数量"))
            for entry in result.source_coverage:
                if entry.file_id in ids and (entry.document_role == "price_reference" or (
                    entry.source_item and entry.source_item == line.source_item
                    and any(e.page == entry.page for e in line.evidence))):
                    entry.status, entry.reason = "non_product", "单价参考来源，不作为订购项目"
            continue
        digest = hashes.get(next(iter(ids)), "") if len(ids) == 1 else ""
        identity = (digest, line.source_item, line.product,
                    tuple((e.page, e.sheet, e.cell, e.text) for e in line.evidence))
        if digest and identity in seen:
            first = seen[identity]
            # Exact duplicate attachment content is not a second order quantity.
            if file_ids(first) != ids:
                combine(first, line)
                result.source_links.append(SourceLink(file_ids=sorted(file_ids(first)),
                    product=first.product, status="merged",
                    reason="重复上传同一原文件，合并来源，数量不相加"))
                continue
        seen[identity] = line
        lines.append(line)
    result.lines = lines
    # Explicitly coded drawing views can span pages of the same attachment.
    drawings, page_duplicates, grouped = {}, Counter(), []
    for line in lines:
        for fid, page in {(e.file_id, e.page) for e in line.evidence}:
            if line.document_role == "drawing" and explicit_code(line):
                page_duplicates[fid, page, explicit_code(line)] += 1
    for line in lines:
        ids = file_ids(line)
        code = explicit_code(line)
        key = (next(iter(ids)), code, normalized(line.product)) if len(ids) == 1 else None
        pages = {(e.file_id, e.page) for e in line.evidence}
        prior = drawings.get(key) if line.document_role == "drawing" and code else None
        if (prior is not None and not pages.intersection((e.file_id, e.page) for e in prior.evidence)
                and all(page_duplicates[fid, page, code] == 1 for fid, page in pages)
                and len(prior.evidence) + len(line.evidence) <= 20):
            conflicts = combine(prior, line)
            result.source_links.append(SourceLink(file_ids=sorted(ids), product=line.product,
                source_code=line.source_code, status="unresolved" if conflicts else "merged",
                reason="同一附件跨页图号唯一对应，合并重复视图，数量不相加" +
                       ("；存在冲突须核图" if conflicts else "")))
            continue
        if line.document_role == "drawing" and code:
            drawings[key] = line
        grouped.append(line)
    lines = grouped
    codes = Counter((fid, explicit_code(line)) for line in lines
                    for fid in file_ids(line) if explicit_code(line))
    removed = set()
    for index, drawing in enumerate(lines):
        if drawing.document_role != "drawing":
            continue
        candidates = [base for base in lines if base.document_role == "requirements"
                      and not (file_ids(base) & file_ids(drawing))
                      and ((explicit_code(drawing) and explicit_code(base) == explicit_code(drawing))
                           or (drawing.source_item and base.source_item == drawing.source_item
                               and normalized(base.product) == normalized(drawing.product)))]
        if not candidates:
            continue
        code = explicit_code(drawing)
        base = candidates[0]
        unique = code and len(candidates) == 1 and all(
            codes[fid, code] == 1 for fid in file_ids(base) | file_ids(drawing))
        unique = unique and all(len(getattr(base, field)) + len(getattr(drawing, field)) <= limit
                               for field, limit in (("evidence", 20), ("dimension_evidence", 20),
                                                    ("quantity_evidence", 20), ("source_measurements", 10)))
        if unique and normalized(base.product) == normalized(drawing.product):
            conflicts = combine(base, drawing)
            removed.add(index)
            result.source_links.append(SourceLink(
                file_ids=sorted(file_ids(base)), product=base.product, source_code=drawing.source_code,
                status="unresolved" if conflicts else "merged",
                reason="明确项目代码唯一对应，合并清单与图纸证据，数量不相加" +
                       ("；存在冲突须核图确认" if conflicts else "")))
        else:
            reason = "跨文件项目可能对应但缺少唯一代码或名称不一致，须确认归属，不能重复计费"
            drawing.quantity = None
            drawing.uncertainties = [reason, *drawing.uncertainties][:20]
            result.source_links.append(SourceLink(file_ids=sorted(file_ids(base) | file_ids(drawing)),
                product=drawing.product, source_code=drawing.source_code, status="unresolved", reason=reason))
            result.questions.append(f"{drawing.product}：{reason}")
    result.lines = [line for index, line in enumerate(lines) if index not in removed]
    result.questions = list(dict.fromkeys(result.questions))[:30]
    return result


def source_review(extraction):
    unresolved = [entry for entry in extraction.get("source_coverage", [])
                  if entry.get("status") == "unresolved" and entry.get("verification") in ("visual", "ai", "native")]
    links = [entry for entry in extraction.get("source_links", []) if entry.get("status") == "unresolved"]
    region_keys = {(e["file_id"], e["page"], tuple(e.get("region") or [])) for e in unresolved}
    audit = extraction.get("source_agent") or {}
    inventory_issues = audit.get("inventory", {}).get("issues", [])
    return {"required": bool(unresolved or links or extraction.get("incomplete") or audit.get("unread_pages")
                            or audit.get("unreviewed_pages") or inventory_issues),
            "unresolved_regions": len(region_keys), "unresolved_links": len(links),
            "regions": unresolved[:50], "links": links[:50],
            "mode": audit.get("mode"), "unread_pages": len(audit.get("unread_pages", [])),
            "unreviewed_pages": len(audit.get("unreviewed_pages", [])),
            "inventory_issues": inventory_issues}
