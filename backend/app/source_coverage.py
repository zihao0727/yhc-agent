"""Deterministic row accounting; model output cannot declare its own completeness."""
import re

from openpyxl.utils import coordinate_to_tuple


def pending_order_charge(row, columns):
    """Recognize an explicit pending fee footer, never a product or quantity."""
    if not columns or not {"product", "quantity"} <= columns.keys():
        return False
    cells = [c for c in row["cells"] if c.get("value", "").strip() or c.get("formula")]
    if len(cells) != 2 or any(c.get("formula") for c in cells):
        return False
    cells.sort(key=lambda c: coordinate_to_tuple(c["cell"])[1])
    label, value = cells
    return (coordinate_to_tuple(label["cell"])[1] < columns["product"]
            and coordinate_to_tuple(value["cell"])[1] > columns["quantity"]
            and label["value"].strip() in {"安装", "安装费", "发票", "税费", "运费", "运输费", "包装费"}
            and value["value"].strip() in {"待定", "待确认", "另议", "未定"})


def row_kind(row, header, columns=None):
    if header and row["row"] <= header:
        return "header"
    if pending_order_charge(row, columns):
        return "non_product"
    cells = [c for c in row["cells"] if c.get("value", "").strip()]
    if len(cells) == 1:
        value = cells[0]["value"].strip()
        if re.match(r"^(?:备注|说明|注意事项|品牌要求|[^：:\n]{1,30}品牌要求)\s*[:：]", value):
            return "non_product"
        if re.fullmatch(r"(?:本页|本章|本节)?(?:合计|小计|总计|总金额)(?:[:：\s]*[\d,.元]*)?", value):
            return "non_product"
    if columns:
        by_column = {coordinate_to_tuple(c["cell"])[1]: c for c in cells}
        name = by_column.get(columns.get("product"), {}).get("value", "").strip()
        # Require an exact section label and no specification/unit/quantity data.
        if name in {"户外", "室内", "车库", "基础", "公区", "室外", "地上", "地下"}:
            allowed = {columns.get("product"), columns.get("item")}
            if all(column in allowed for column in by_column):
                return "non_product"
        if any(re.fullmatch(r"(?:本页|本章|本节)?(?:合计|小计|总计|总金额)", c["value"].strip())
               for c in cells):
            # Summary rows may carry amount cells, never a separate product description.
            if not name or re.fullmatch(r"(?:本页|本章|本节)?(?:合计|小计|总计|总金额)", name):
                return "non_product"
    return None


def account_rows(result, sources):
    from .customer_excel import table_columns, financial_summary
    from .requirements_schemas import SourceCoverage
    coverage = {(c.file_id, c.page, c.sheet, c.row): c for c in result.source_coverage}
    for (file_id, page), source in sources.items():
        columns, header = table_columns(source)
        for row in source["rows"]:
            key = (file_id, page, source["sheet"], row["row"])
            kind = row_kind(row, header, columns)
            product_cell = next((c for c in row["cells"] if coordinate_to_tuple(c["cell"])[1]
                                 == columns.get("product")), None)
            if product_cell and financial_summary(product_cell["value"]):
                kind = "non_product"
            if source.get("analysis_plan", {}).get("purpose") == "price_reference":
                kind = "non_product"
            def belongs(e):
                return (e.file_id == file_id and e.page == page and e.sheet == source["sheet"]
                        and e.cell and coordinate_to_tuple(e.cell)[0] == row["row"])
            associated = [line for line in result.lines if any(belongs(e) for e in line.evidence)]
            if kind in ("non_product", "header"):
                result.lines = [line for line in result.lines if not (
                    any(line is item for item in associated) and line.evidence
                    and all(belongs(e) for e in line.evidence) and not line.text_evidence)]
            else:
                kind = "extracted" if associated else "unresolved"
            reason = ("原始标题/说明/汇总行，保留来源覆盖，不作为产品计价" if kind == "non_product" else "")
            missing = [a for a in source.get("resource_audit", [])
                       if a.get("row") == row["row"] and a["status"] != "resolved"]
            if missing:
                reason = "图纸资源未读取，依赖图纸的文字、尺寸须核实"
                for line in associated:
                    line.uncertainties = list(dict.fromkeys([reason, *line.uncertainties]))[:20]
            coverage[key] = SourceCoverage(file_id=file_id, page=page, sheet=source["sheet"],
                                           row=row["row"], status=kind, reason=reason)
    result.source_coverage = list(coverage.values())
    return result
