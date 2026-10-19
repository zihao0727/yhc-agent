"""One authoritative validator for recognition and agent-directed repairs."""
import json
import re
from decimal import Decimal

from pydantic import ValidationError

from .config import settings
from .extraction_contract import normalize_extraction_response, validate_repair_inventory
from .requirements_schemas import ExtractionResult, SourceCoverage


class ExtractionValidationFailure(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("需求资料校验未通过")


def failure_description(diagnostic):
    labels = {"width_mm": "宽度", "height_mm": "高度", "thickness_mm": "板材厚度",
              "depth_mm": "深度", "billing_length_mm": "计价长度", "quantity": "数量"}
    parts = []
    if diagnostic.get("source_item"):
        parts.append(f'第{str(diagnostic["source_item"])[:80]}项')
    if diagnostic.get("product"):
        parts.append(str(diagnostic["product"])[:100])
    if diagnostic.get("sheet"):
        parts.append(f'工作表“{str(diagnostic["sheet"])[:100]}”')
    if diagnostic.get("cell"):
        parts.append(str(diagnostic["cell"])[:40])
    elif diagnostic.get("page"):
        parts.append(f'第{diagnostic["page"]}页')
    if diagnostic.get("field") in labels:
        field = labels[diagnostic["field"]]
        if diagnostic.get("value") is not None:
            field += f'（提取值 {str(diagnostic["value"])[:40]}{diagnostic.get("unit", "")}）'
        parts.append(field)
    reason = diagnostic.get("reason", "")
    if reason:
        parts.append(str(reason)[:200])
    return "，".join(parts)


def validate_extraction(raw, files, source_texts, brief, supplementary, *,
                        inventory=None, page_scope=None, excel_rows=None,
                        pdf_region=None, image_slice=None):
    from . import deepseek
    from .customer_excel import MEDIA_TYPE, normalize_evidence, validate_evidence, validate_excel_rows
    from .quantity_validation import validate_measurement_sources, validate_quantity_evidence
    from .pdf_requirements import validate_rows

    stage, line_index, evidence = "schema", None, None
    try:
        normalize_extraction_response(raw)
        deepseek.normalize_dimension_targets(raw)
        deepseek.normalize_dimension_values(raw)
        deepseek.normalize_quantity_values(raw)
        result = ExtractionResult.model_validate(raw)
        stage = "repair_coverage"
        validate_repair_inventory(inventory, raw)
        result.source_coverage = []
        allowed = {f.id: f.page_count for f in files}
        excel_ids = {f.id for f in files if f.media_type == MEDIA_TYPE}
        for line_index, line in enumerate(result.lines):
            stage, evidence = "file_source", None
            for (file_id, page), text in source_texts.items():
                if file_id in excel_ids:
                    normalize_evidence(line, json.loads(text), file_id, page)
            items = [*line.evidence, *line.quantity_evidence, *line.dimension_evidence,
                     *(e for item in line.source_measurements for e in item.evidence),
                     *(e for item in line.components for e in item.evidence),
                     *(e for item in line.production_requirements for e in item.evidence),
                     *(e for item in line.letter_groups for e in item.evidence)]
            for evidence in items:
                if evidence.file_id not in allowed or evidence.page > allowed[evidence.file_id]:
                    raise ValueError("模型返回无效来源")
                if page_scope is not None and (evidence.file_id, evidence.page) not in page_scope:
                    raise ValueError("模型返回未提供的页面")
                if evidence.file_id in excel_ids:
                    validate_evidence(evidence, json.loads(source_texts[evidence.file_id, evidence.page]))
            if excel_rows is not None:
                from openpyxl.utils.cell import coordinate_to_tuple
                stage = "row_validation"
                if not any(e.cell and coordinate_to_tuple(e.cell)[0] in excel_rows for e in line.evidence):
                    raise ValueError("模型项目未引用本批Excel正文行，不能用表头或其他行补造项目")
            stage = "measurement_source"
            if line.source_code:
                from .visual_audit import quoted_code
                if not any(quoted_code(line.source_code, e.text) for e in line.evidence):
                    raise ValueError("项目代码须引用本项原始代码文字")
                for e in line.evidence:
                    text = source_texts.get((e.file_id, e.page), "")
                    if e.source == "text" and text and quoted_code(line.source_code, e.text):
                        if e.file_id in excel_ids:
                            source = json.loads(text)
                            cells = [c for row in source["rows"] for c in row["cells"] if c["cell"] == e.cell]
                            text = "\n".join(c["value"] for c in cells)
                        if not quoted_code(line.source_code, text):
                            raise ValueError("项目代码未出现在引用原文")
            validate_measurement_sources(line, source_texts)
            stage = "quantity_evidence"
            validate_quantity_evidence(line, source_texts, recover_semantic=True)
            stage = "dimension_evidence"
            supported = deepseek.validate_dimension_evidence(line, source_texts)
            stage = "text_evidence"
            for text in line.text_evidence:
                if text not in brief and text not in supplementary:
                    raise ValueError("模型返回无效文字来源")
            native = "\n".join(source_texts.get((e.file_id, e.page), "") for e in line.evidence)
            if len(native.strip()) > 100:
                values = {Decimal(n) for n in re.findall(
                    r"\d+(?:\.\d+)?", native + "\n" + brief + "\n" + supplementary)}
                for field in ("width_mm", "height_mm", "thickness_mm", "depth_mm"):
                    value = getattr(line, field)
                    if value is not None and value not in values and field not in supported:
                        line.uncertainties = [*line.uncertainties,
                            f"{field}={value}未在PDF文本层找到，请核对图示或单位换算"][:20]
                        setattr(line, field, None)
        stage, line_index = "row_validation", None
        result = validate_rows(result, {key: text for key, text in source_texts.items()
                                       if key[0] not in excel_ids})
        excel_sources = {key: json.loads(text) for key, text in source_texts.items() if key[0] in excel_ids}
        if image_slice is not None:
            excel_sources = {key: deepseek.batch_source(source) for key, source in excel_sources.items()}
        result = validate_excel_rows(result, excel_sources)
        if len(result.lines) > settings().max_requirement_lines:
            stage = "requirement_limit"
            raise ValueError("提取明细超过任务上限，请拆分任务")
        for line in result.lines:
            line.measurement_version = 3
            if pdf_region is not None:
                for evidence in [*line.evidence, *line.dimension_evidence, *line.quantity_evidence,
                                 *(e for item in line.components for e in item.evidence),
                                 *(e for item in line.production_requirements for e in item.evidence),
                                 *(e for item in line.letter_groups for e in item.evidence),
                                 *(e for item in line.source_measurements for e in item.evidence)]:
                    evidence.region = pdf_region
        for file_id, page in source_texts:
            if (file_id, page) not in excel_sources:
                covered = any(any(e.file_id == file_id and e.page == page for e in line.evidence)
                              for line in result.lines)
                result.source_coverage.append(SourceCoverage(
                    file_id=file_id, page=page, row=0, status="extracted" if covered else "unresolved",
                    region=pdf_region, kind="region" if pdf_region else "page",
                    reason="区域提取完成，尚须独立核查漏项" if pdf_region else
                           "页面级覆盖；版面区域及图示完整性仍须审核"))
        return result
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError) as exc:
        detail = {"stage": stage, "error_type": type(exc).__name__}
        if line_index is not None:
            detail.update(line_index=line_index, path=f"lines.{line_index}",
                          product=line.product, source_item=line.source_item)
        if isinstance(exc, ValidationError):
            detail["fields"] = [{"path": ".".join(str(p)[:100] for p in item["loc"]),
                                 "type": item["type"], "message": item["msg"][:300]}
                                for item in exc.errors(include_input=False, include_url=False,
                                                       include_context=False)[:10]]
        else:
            detail["reason"] = str(exc)[:200]
            if hasattr(exc, "diagnostics"):
                detail.update(exc.diagnostics)
                if "evidence_index" in exc.diagnostics and stage in ("quantity_evidence", "dimension_evidence"):
                    detail["path"] = f"lines.{line_index}.{stage}.{exc.diagnostics['evidence_index']}"
        if stage == "file_source":
            from .customer_excel import source_cells
            if evidence is not None:
                detail.update(file_id=evidence.file_id, page=evidence.page,
                              sheet=evidence.sheet, cell=evidence.cell)
            detail["allowed_excel_sources"] = [
                {"file_id": file_id, "page": page, "sheet": json.loads(text)["sheet"],
                 "cells": sorted(source_cells(json.loads(text)))}
                for (file_id, page), text in source_texts.items() if file_id in excel_ids]
        raise ExtractionValidationFailure(detail) from exc
