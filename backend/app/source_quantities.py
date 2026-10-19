"""Use ordered engineering quantities directly, without inventing a piece count."""
from .pricing_errors import PricingError


def source_measurement(line, index=None):
    if index is None and len(line.source_measurements) == 1:
        index = 0
    if index is None or index >= len(line.source_measurements):
        raise PricingError("missing_source_quantity", "须选择本项目已保存的原始工程量，多个工程量不能自动相加",
                           ["source_measurements"], cause="recognition_review", action="inspect_source")
    return line.source_measurements[index]


def verify_source_measurement(db, line, measurement):
    from openpyxl.utils.cell import coordinate_to_tuple
    from .models import CustomerFile
    from .customer_excel import MEDIA_TYPE, page_source, table_measurement, validate_evidence
    from .customer_files import storage_path, pdf_page_text
    from .quantity_validation import contains_quote
    anchors = {(e.file_id, e.page, e.sheet, coordinate_to_tuple(e.cell)[0] if e.cell else None)
               for e in line.evidence}
    pages = {}
    proven = False
    for evidence in measurement.evidence:
        location = (evidence.file_id, evidence.page, evidence.sheet,
                    coordinate_to_tuple(evidence.cell)[0] if evidence.cell else None)
        if location not in anchors:
            raise ValueError("工程量来源不属于当前项目行，不能借用其他项目")
        file = db.get(CustomerFile, evidence.file_id)
        if not file:
            raise ValueError("工程量来源文件不存在，须重新核对原件")
        key = (file.id, evidence.page)
        if evidence.page > file.page_count:
            raise ValueError("工程量来源页码不存在")
        if evidence.source == "image":
            if not line.confirmed:
                raise ValueError("图片中的原始工程量须先人工确认，不能自动当作已核验数量")
            if file.media_type == MEDIA_TYPE:
                if key not in pages:
                    pages[key] = page_source(storage_path(file.storage_key), evidence.page)
                if not any(image["cell"] == evidence.cell for image in pages[key]["images"]):
                    raise ValueError("工程量图片来源未对应实际图纸")
            elif file.media_type != "application/pdf" and not file.media_type.startswith("image/"):
                raise ValueError("工程量来源不是可确认的图纸")
            proven = True
            continue
        if file.media_type == MEDIA_TYPE:
            if key not in pages:
                pages[key] = page_source(storage_path(file.storage_key), evidence.page)
            page = pages[key]
            validate_evidence(evidence, page)
            expected = table_measurement(page, location[3], file.id, evidence.page) if location[3] else None
            if (expected is None or expected.value != measurement.value or expected.unit != measurement.unit
                    or evidence.cell not in {e.cell for e in expected.evidence}):
                raise ValueError("工程量数值或单位与原表数量、单位栏不一致，不能改写工程量凑价")
            proven = True
        elif file.media_type == "application/pdf" and evidence.source == "text":
            if key not in pages:
                pages[key] = pdf_page_text(file, evidence.page)
            text = pages[key]
            if not contains_quote(text, evidence.text):
                raise ValueError("工程量引用未出现在原始PDF中")
            # PDF engineering quantities need an explicit number and matching unit.
            import re
            import unicodedata
            from decimal import Decimal
            from .measurement_units import ENGINEERING_FACTORS
            quoted = unicodedata.normalize("NFKC", evidence.text)
            matches = re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*([A-Za-z0-9^\u4e00-\u9fff]+)", quoted)
            proven = proven or any(unit in ENGINEERING_FACTORS
                                   and ENGINEERING_FACTORS[unit][0] == measurement.unit
                                   and Decimal(number) * ENGINEERING_FACTORS[unit][1] == measurement.value
                                   for number, unit in matches)
    if not proven:
        raise ValueError("原始工程量尚不能由数量及单位证据核实；图片读数须先人工确认或补充可核验依据")
