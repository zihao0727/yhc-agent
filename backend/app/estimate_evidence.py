"""Check estimate evidence against immutable uploads, not model-generated narratives."""
from .customer_files import pdf_page_text, storage_path
from .customer_excel import MEDIA_TYPE, page_source, validate_evidence
from .pricing_strategies import target_object


def validate_estimate_evidence(line, files):
    if not line.estimate:
        return
    cached = {}
    for assumption in line.estimate.assumptions:
        for evidence in assumption.evidence:
            source_file = files.get(evidence.file_id)
            if not source_file or evidence.page > source_file.page_count:
                raise ValueError("补全/纠错引用了不属于当前任务的来源")
            if source_file.media_type == MEDIA_TYPE:
                key = (evidence.file_id, evidence.page)
                if key not in cached:
                    cached[key] = page_source(storage_path(source_file.storage_key), evidence.page)
                validate_evidence(evidence, cached[key])
                if assumption.mode == "correct":
                    if evidence.source == "image" and not any(
                            image["cell"] == evidence.cell for image in cached[key]["images"]):
                        raise ValueError("纠错图片证据未对应实际内嵌图纸，不能把文本单元格伪称为图片来源")
                    from openpyxl.utils.cell import coordinate_to_tuple
                    obj = target_object(line, assumption.target, assumption.target_index)
                    anchors = [*line.evidence, *getattr(obj, "evidence", []), *[
                        e for e in line.dimension_evidence if e.target == assumption.target
                        and e.target_index == assumption.target_index]]
                    rows = {coordinate_to_tuple(e.cell)[0] for e in anchors
                            if e.file_id == evidence.file_id and e.page == evidence.page
                            and e.sheet == evidence.sheet and e.cell}
                    if not rows or coordinate_to_tuple(evidence.cell)[0] not in rows:
                        raise ValueError("纠错引用了其他项目的Excel行，请核对当前对象的来源单元格")
                if assumption.mode == "correct" and evidence.source == "text":
                    cells = [cell for row in [
                        *cached[key]["rows"], *cached[key].get("header_rows", [])]
                        for cell in row["cells"] if cell["cell"] == evidence.cell]
                    if not any(contains_quote(cell["value"], evidence.text) for cell in cells):
                        raise ValueError("纠错引用的文字未出现在对应Excel单元格，不能伪造原文数值")
            elif (source_file.media_type == "application/pdf" and assumption.mode == "correct"
                  and evidence.source == "text"):
                key = (evidence.file_id, evidence.page)
                if key not in cached:
                    cached[key] = pdf_page_text(source_file, evidence.page)
                if not contains_quote(cached[key], evidence.text):
                    raise ValueError("纠错引用的文字未出现在对应PDF文本层，请核对图纸证据")


def contains_quote(source, quote):
    quoted = "".join(quote.split())
    return bool(quoted) and quoted in "".join(str(source).split())
