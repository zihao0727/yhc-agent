"""Conservative source-row coverage and numeric checks for text-backed PDF tables."""
import re
from decimal import Decimal

from .requirements_schemas import ExtractedLine, Evidence

ROW = re.compile(r"(?<![\d.])(\d{1,3})[ \t]+(?=[\u4e00-\u9fffA-Za-z])")
DIMENSION = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[*xX×]\s*(\d+(?:\.\d+)?)"
                       r"\s*(?:mm)?(?:\s*[*xX×]\s*(\d+(?:\.\d+)?))?", re.I)
STEEL = re.compile(r"(\d+(?:\.\d+)?)\s*mm\s*(?:TH[HK]+\.?\s*)?(?:厚\s*)?(?:304#\s*)?不锈钢", re.I)


def outer_dimensions(raw):
    """Exclude profile sections such as 20*20*2mm tube from overall dimensions."""
    values = set()
    for match in DIMENSION.finditer(raw):
        prefix = raw[:match.start()].split("\n")[-1].strip()
        suffix = raw[match.end():].split("\n")[0].strip()
        if re.search(r"尺寸\s*[:：]?\s*$", prefix) or (not prefix and suffix in ("", "mm", "MM")):
            values.add((Decimal(match[1]), Decimal(match[2])))
    return values


def source_rows(text):
    # Only recognize a numbered table when several plausible rows are present.
    matches = [m for m in ROW.finditer(text) if int(m[1]) <= 100
               and not re.match(r"(?:mm|cm|m2|THK)\b", text[m.end():], re.I)]
    if len(matches) < 2:
        return {}
    result = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        raw = text[m.start():end].strip()
        name = text[m.end():end].splitlines()[0].strip()
        result[m[1]] = {"name": name, "text": raw}
    return result


def validate_rows(result, texts):
    for (file_id, page), text in texts.items():
        rows = source_rows(text)
        if not rows:
            continue
        page_lines = [l for l in result.lines if any(e.file_id == file_id and e.page == page for e in l.evidence)]
        seen = {l.source_item for l in page_lines}
        for line in page_lines:
            row = rows.get(line.source_item)
            if not row:
                line.uncertainties = [*line.uncertainties, "未匹配到PDF原始行序号，需核实来源"][:20]
                continue
            raw = row["text"]
            dimensions = outer_dimensions(raw)
            if len(dimensions) > 1:
                line.width_mm = line.height_mm = None
                line.uncertainties = [*line.uncertainties, "同一行尺寸栏与项目特征存在不同尺寸，须确认后计价"][:20]
            elif dimensions:
                width, height = next(iter(dimensions))
                if ((line.width_mm is None or line.width_mm == width)
                        and (line.height_mm is None or line.height_mm == height)):
                    line.width_mm, line.height_mm = width, height
                else:
                    line.width_mm = line.height_mm = None
                    line.uncertainties = [*line.uncertainties, "提取尺寸与本行原文不一致，须核实"][:20]
            steel = {Decimal(m[1]) for m in STEEL.finditer(raw)}
            if steel and line.thickness_mm is not None and line.thickness_mm not in steel:
                line.thickness_mm = None
                line.uncertainties = [*line.uncertainties, "板材厚度与壳深/其他部件厚度可能混淆，请核实"][:20]
        for item, row in rows.items():
            if item in seen:
                continue
            result.lines.append(ExtractedLine(
                source_item=item, product=row["name"][:150], notes=row["text"][:4000],
                evidence=[Evidence(file_id=file_id, page=page, text=row["text"][:1500])],
                uncertainties=["该行由PDF文本层补回，模型未完整提取，请核对规格及数量"]))
    if len(result.lines) > 100:
        raise ValueError("Too many source rows")
    return result
