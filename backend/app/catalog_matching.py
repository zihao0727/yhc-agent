"""Rank catalog evidence by physical specification before descriptive wording."""
import re
import unicodedata
from decimal import Decimal

from .quote_safety import material_family, material_thickness


def normalize(text):
    return re.sub(r"[\s#＃]", "", unicodedata.normalize("NFKC", text or "")).casefold()


def material_grade(text):
    text = unicodedata.normalize("NFKC", text or "").strip().casefold()
    bare = re.fullmatch(r"(201|304|316l?|h\d{2}|6061|5052)\s*#?", text)
    if bare:
        return bare[1]
    patterns = (
        r"(?<!\d)(201|304|316l?)\s*#?\s*(?:不锈钢|stainless)",
        r"(?:不锈钢|stainless|sus)\s*(201|304|316l?)(?!\d)",
        r"(?<!\d)(201|304|316l?)\s*#(?=\s*\d+(?:\.\d+)?(?:mm)?\s*$)",
        r"(?<![a-z0-9])(h\d{2})\s*(?:黄铜|brass)",
        r"(?<!\d)(6061|5052)\s*#?\s*(?:铝|alumin)",
        r"(?:铝(?:合金)?|alumin(?:ium|um))\s*(6061|5052)(?!\d)",
    )
    return next((match[1] for pattern in patterns if (match := re.search(pattern, text))), None)


def tokens(text):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    result = set(re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", text))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(word[index:index + 2] for index in range(len(word) - 1))
    return result


def lexical_score(query, name, text):
    name, text = normalize(name), normalize(text)
    terms = tokens(query) - {"产品", "材料", "工艺", "报价", "需要", "制作", "这个", "没有"}
    return sum(min(len(token), 5) * (12 if token in name else 1)
               for token in terms if token in text)


def process_score(query, processes):
    from .process_taxonomy import process_codes
    return 60 * len(set(process_codes(query)) & set(process_codes(processes)))


def specification_match(material, thickness, price):
    family, candidate_family = material_family(material), material_family(price.material)
    grade, candidate_grade = material_grade(material), material_grade(price.material)
    if family and candidate_family and family != candidate_family:
        return -1
    if grade and candidate_grade and grade != candidate_grade:
        return -1
    if thickness is not None and price.thickness_mm is not None and Decimal(
            str(thickness)) != price.thickness_mm:
        return -1
    return (100 if family and family == candidate_family else 0) + (
        160 if grade and grade == candidate_grade else 0) + (
        80 if thickness is not None and price.thickness_mm == Decimal(str(thickness)) else 0)


def retrieval_targets(raw, source_text=""):
    # Each physical object supplies its own material and thickness.
    targets = [{"id": "item", "material": raw.get("material", ""),
                "thickness": raw.get("thickness_mm"),
                "text": " ".join([*(raw.get(key, "") or "" for key in
                                    ("product", "material", "process", "notes")), source_text])}]
    targets.extend({"id": obj.get("object_id", f"component_{index}"),
                    "material": obj.get("material", ""), "thickness": obj.get("thickness_mm"),
                    "text": " ".join([obj.get("name", ""), obj.get("material", ""),
                                      *obj.get("processes", [])])}
                   for index, obj in enumerate(raw.get("components", [])))
    expanded = []
    for target in targets:
        # These spans are retrieval hints only, never inferred billable components.
        text = unicodedata.normalize("NFKC", target["material"] or target["text"])
        spans = list(re.finditer(
            r"(?<![\d.x×*])(?:T\s*)?"
            r"(?P<sizes>\d+(?:\.\d+)?\s*mm(?:\s*/\s*\d+(?:\.\d+)?\s*mm)*)\s*"
            r"(?P<material>(?:(?:201|304|316L?)\s*#?\s*)?"
            r"(?:不锈钢|铝板|(?:透光|乳白|透明|/|\s){0,12}亚克力板?))", text, re.I))
        hints = [{**target, "material": span["material"], "thickness": Decimal(size)}
                 for span in spans if not re.search(r"[x×*]\s*$", text[:span.start()], re.I)
                 for size in re.findall(r"(\d+(?:\.\d+)?)\s*mm", span["sizes"], re.I)]
        if len(hints) > 1:
            expanded.extend(hints)
        else:
            expanded.append(target)
    for index, target in enumerate(expanded):
        target["retrieval_key"] = index
        if not target["material"]:
            from .quote_safety import MATERIALS
            families = {family for family, pattern in MATERIALS.items()
                        if re.search(pattern, target["text"], re.I)}
            if len(families) <= 1:
                target["material"] = target["text"]
        if target["thickness"] is None:
            target["thickness"] = material_thickness(target["material"])
            if target["thickness"] is None:
                # A multi-material description cannot supply one object's thickness.
                from .quote_safety import MATERIALS
                families = {family for family, pattern in MATERIALS.items()
                            if re.search(pattern, target["text"], re.I)}
                if len(families) <= 1:
                    target["thickness"] = material_thickness(target["text"])
    return expanded
