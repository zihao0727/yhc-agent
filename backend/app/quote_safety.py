"""Source and applicability checks shared by saved plans and live quotations."""
import re
from collections import defaultdict
from decimal import Decimal

from .pricing_errors import PricingError


def physical_rows(raw):
    rows = set()
    for key in ("evidence", "quantity_evidence", "dimension_evidence"):
        for evidence in raw.get(key, []):
            if evidence.get("target", "item") != "item":
                continue
            match = re.fullmatch(r"[A-Z]+([1-9]\d*)", evidence.get("cell", ""))
            if match and evidence.get("sheet"):
                rows.add((evidence["file_id"], evidence["page"], evidence["sheet"], int(match[1])))
    return rows


def inventory_blockers(requirements):
    rows, blocked = defaultdict(list), defaultdict(list)
    has_schedule = any(raw.get("document_role") == "requirements" for raw in requirements)
    for raw in requirements:
        locations = physical_rows(raw)
        if len(locations) == 1 and raw.get("document_role") != "price_reference":
            rows[next(iter(locations))].append(raw["id"])
        if (has_schedule and raw.get("document_role") == "drawing"
                and not raw.get("confirmed") and raw.get("manual_unit_price") is None):
            blocked[raw["id"]].append("图纸观察尚未关联订购清单，不能作为独立订购项目重复计价")
        if raw.get("document_role") == "price_reference":
            blocked[raw["id"]].append("价格参考资料不能作为订购项目计价")
    for location, identities in rows.items():
        if len(identities) > 1:
            message = f"同一原始清单行（{location[2]} 第{location[3]}行）有重复记录，须归并后计价，数量不能相加"
            for identity in identities:
                blocked[identity].append(message)
    return blocked


def validate_area_basis(line, cost=None, required=(), source_text=""):
    if line.manual_unit_price is not None:
        return
    original = " ".join([source_text, line.material, line.notes,
                         *(e.text for e in line.evidence)])
    if (cost is not None and cost.kind == "material" and cost.target == "component"
            and cost.rate_unit == "m2" and cost.target_index is not None):
        component = line.components[cost.target_index]
        partial = re.search(r"上部|下部|顶部|底部", component.name)
        overall_area = (line.width_mm * line.height_mm / Decimal(1_000_000)
                        if line.width_mm and line.height_mm else None)
        repeats_overall = overall_area is not None and (
            component.area_m2 == overall_area or
            component.width_mm == line.width_mm and component.height_mm == line.height_mm)
        measured_area = any(
            not re.search(r"不能|不得|未给|未标|不代表", evidence.text)
            and re.search(r"净(?:用料)?面积|展开面积|实际用料面积", evidence.text)
            and re.search(r"\d+(?:[.]\d+)?\s*(?:㎡|m[²2]|平方米)", evidence.text, re.I)
            for evidence in component.evidence)
        if partial and repeats_overall and not measured_area:
            raise PricingError("partial_component_uses_overall_area",
                f"{component.name}属于整件的局部，不能直接重复使用整件外轮廓面积；"
                "须核对该部件实际尺寸/展开面积和下料计费依据。",
                [f"components[{cost.target_index}].width_mm",
                 f"components[{cost.target_index}].height_mm",
                 f"components[{cost.target_index}].area_m2"],
                cause="recognition_review", action="inspect_source")
    if (line.rate_evidence_required and cost is not None and cost.kind == "material"
            and cost.rate_unit == "m2" and cost.measurement != "source_quantity"
            and re.search(r"切割造型", original)
            and re.search(r"(?:独立|分离).{0,8}(?:图标|人形)|图标组合外框", original)):
        target = (line.components[cost.target_index]
                  if cost.target == "component" and cost.target_index is not None else line)
        net_area = target.area_m2 is not None and any(
            re.search(r"净(?:用料)?面积|展开面积|实际用料面积", e.text) for e in target.evidence)
        uses_outer_box = (cost.target == "item" or (
            cost.target == "component" and target.width_mm == line.width_mm
            and target.height_mm == line.height_mm))
        if uses_outer_box and not net_area:
            raise PricingError("layout_area_not_material",
                "切割造型的外框尺寸尚不能证明实际用料面积；须核对原图、净用料面积或适用的下料计费依据，"
                "不能直接把整组外框当作完整矩形板材",
                ["area_m2", "components"], cause="recognition_review", action="inspect_source")
    if (line.rate_evidence_required and cost is not None and cost.target == "component"
            and cost.rate_unit == "m2" and cost.target_index is not None):
        component = line.components[cost.target_index]
        text = " ".join(e.text for e in line.evidence)
        net_area = component.area_m2 is not None and any(
            re.search(r"净(?:用料)?面积|展开面积|实际用料面积", e.text) for e in component.evidence)
        if (not net_area and re.search(r"切割立体字|切割.{0,5}文字|整体布置", text)
                and component.width_mm == line.width_mm and component.height_mm == line.height_mm):
            raise PricingError("layout_area_not_material",
                "文字组合外框被重复用作部件用料面积；须核对逐字尺寸或实际用料面积，不能改名为板材规避核图",
                [f"components[{cost.target_index}].area_m2"], cause="recognition_review", action="inspect_source")
    if cost is not None and cost.measurement == "source_quantity":
        return
    if cost is not None and (cost.target != "item" or not (
            cost.rate_unit == "m2" or set(required) & {"area_m2", "billable_area_m2"})):
        return
    notes = " ".join([line.notes, *line.uncertainties, *(e.text for e in line.evidence)])
    if line.area_m2 is not None and any(re.search(r"净(?:用料)?面积|展开面积|实际用料面积", e.text)
                                       for e in line.evidence):
        return
    if (line.rate_evidence_required and cost is not None
            and cost.measurement in ("item_area", "auto", "script")
            and re.search(r"切割立体字|切割.{0,5}文字", notes)):
        raise PricingError("layout_area_not_material",
            "切割文字的整组外框不能作为净材料面积；须采用逐字尺寸、可核验用料面积或有依据的整套成品价",
            ["letter_groups", "area_m2"], cause="recognition_review", action="inspect_source")
    layout = re.search(r"整体.{0,12}(?:布置|指引范围|布置范围)|总布置|包围.{0,8}(?:范围|尺寸)|"
                       r"布置范围|layout\s+(?:extent|dimensions)", notes, re.I)
    if layout:
        raise PricingError("layout_area_not_material",
            "整体布置范围不能作为净材料面积计价；须核对各字、线条或部件工程量，或采用有依据的整套成品价",
            ["components", "letter_groups", "area_m2"], cause="recognition_review", action="inspect_source")


MATERIALS = {
    "stainless": r"(?:304|316|201)?[#＃]?\s*不锈钢|stainless|(?<!\d)(?:201|304|316L?)[#＃](?!\d)",
    "galvanized": r"镀锌(?:钢)?板|galvanized",
    "acrylic": r"亚克力|有机玻璃|acrylic|(?<![a-z])pmma(?![a-z])",
    "pc": r"聚碳酸酯|(?<![a-z])pc(?:板)?(?![a-z])",
    "aluminum": r"铝(?:合金|板)?|alumin(?:um|ium)",
    "brass": r"黄铜(?!色)|(?:h62|h65)(?![a-z])|brass",
    "copper": r"紫铜|红铜|(?<![a-z])copper(?![a-z])",
    "titanium_gold": r"钛金板",
}


def material_family(text):
    matches = [(match.start(), family) for family, pattern in MATERIALS.items()
               if (match := re.search(pattern, text or "", re.I))]
    return min(matches)[1] if matches else None


def material_thickness(text):
    text = text or ""
    # The specification's numbered list can join "2." directly to "5mm".
    if re.search(r"1[.．]\s*规格尺寸", text) and re.search(r"(?m)^\s*3[.．]\s*其[它他]", text):
        text = re.sub(r"(?m)^[ \t]*2[.．][ \t]*(?=\d+(?:\.\d+)?\s*mm)", "", text)
    text = re.sub(r"(?m)^[ \t]*[1-9]\d{0,2}[.．、][ \t]*(?=\d+\.\d+\s*mm)", "", text)
    match = re.search(r"(?:[Tt]\s*|厚(?:度)?\s*[:：]?\s*)(\d+(?:\.\d+)?)\s*mm", text)
    if not match:
        match = re.search(r"(?<![\d.*×])(\d+(?:\.\d+)?)\s*mm\s*(?:透明|实心)?"
                          r"(?:亚克力|不锈钢|黄铜|钛金板|铝板|PC板)", text, re.I)
    return Decimal(match[1]) if match else None


def validate_catalog_target(price, scoped, cost, index, source_text=""):
    if cost.kind not in ("material", "finished", "process"):
        return
    target_material, catalog_material = material_family(scoped.material), material_family(price.material)
    prefix = f"estimate.costs[{index}]"
    target_prefix = f"components[{cost.target_index}]." if cost.target == "component" else ""
    if not target_material and cost.target == "item":
        families = {family for family, pattern in MATERIALS.items()
                    if re.search(pattern, source_text, re.I)}
        if len(families) == 1:
            target_material = next(iter(families))
    if (cost.target == "item" and catalog_material and not target_material
            and cost.kind in ("material", "finished")):
        raise PricingError("missing_target_material",
            f"{cost.label}：原文没有明确该材料，不能将库价材料及其费用当作本项成品；"
            "仅加工的项目应引用实际工艺分项，确需材料时先核对原文",
            [target_prefix + "material", prefix + ".reference_id"],
            cause="recognition_review", action="inspect_source")
    if cost.target == "component" and catalog_material and not scoped.material.strip():
        raise PricingError("missing_target_material",
            f"{cost.label}：计量对象材质尚未明确，不能直接采用库价材质",
            [target_prefix + "material"], cause="recognition_review", action="inspect_source")
    if target_material and catalog_material and target_material != catalog_material:
        raise PricingError("catalog_material_mismatch",
            f"{cost.label}：库价材质“{price.material}”与计量对象“{scoped.material}”不匹配；"
            "类比暂估须单列暂估费率及差异依据，不能直接冒充匹配库价",
            [prefix + ".reference_id", target_prefix + "material"], cause="plan_repair", action="repair_estimate")
    from .catalog_matching import material_grade
    target_grade, catalog_grade = material_grade(scoped.material), material_grade(price.material)
    if target_grade and catalog_grade and target_grade != catalog_grade:
        raise PricingError("catalog_material_mismatch", f"{cost.label}：不锈钢牌号与库价不一致",
                           [prefix + ".reference_id"], cause="plan_repair", action="repair_estimate")
    thickness = scoped.thickness_mm or material_thickness(scoped.material)
    if thickness is None and cost.target == "item":
        thickness = material_thickness(source_text)
    if cost.target == "component" and thickness is None and price.thickness_mm is not None:
        raise PricingError("missing_target_thickness",
            f"{cost.label}：部件板厚尚未明确，不能默认等于库价板厚{price.thickness_mm}mm",
            [target_prefix + "thickness_mm"], cause="recognition_review", action="inspect_source")
    if thickness is not None and price.thickness_mm is not None and thickness != price.thickness_mm:
        raise PricingError("catalog_thickness_mismatch",
            f"{cost.label}：对象板厚{thickness}mm与库价板厚{price.thickness_mm}mm不一致；"
            "厚度差价须另列依据和费用，不能只在说明中声称已调整",
            [prefix + ".reference_id", target_prefix + "thickness_mm"],
            cause="plan_repair", action="repair_estimate")


def validate_source_material(line, sources):
    if line.confirmed or line.manual_unit_price is not None:
        return
    # A short standalone material remark conflicts with the detailed specification.
    descriptions = [e.text for e in sources if len(e.text) > 20
                    and re.search(r"规格|材质|尺寸|材料", e.text)]
    families = {material_family(text) for text in descriptions} - {None}
    remarks = [e for e in sources if re.fullmatch(
        r"(?:备注[:：]?\s*)?(?:钛金板|实心黄铜|黄铜板|亚克力板|PC板|铝板)", e.text.strip(), re.I)]
    if any(material_family(e.text) not in families for e in remarks) and families:
        raise PricingError("conflicting_source_material",
            "原清单说明与材质备注不一致，须确认采用哪种材质后自动计价",
            ["material"], cause="recognition_review", action="inspect_source")


def validate_manufacturing(line, costs, source_text):
    if line.estimate.contract_version < 2:
        return
    from .process_taxonomy import positive_clauses
    source_text = "；".join(positive_clauses(source_text))
    from .catalog_matching import retrieval_targets
    specifications = {(material_family(t["material"]), t["thickness"])
                      for t in retrieval_targets(line.model_dump(mode="json"))
                      if t["id"] == "item" and t["material"]}
    item_materials = [index for index, cost in enumerate(line.estimate.costs)
                      if cost.kind == "material" and cost.target == "item"]
    if len(specifications) > 1 and len(item_materials) > 1:
        raise PricingError("composite_material_scope",
            "不同材料或板厚的费用仍绑定同一整件对象，不能重复套用整件面积；"
            "须分别建立实际部件，并绑定材质、板厚、原图用料尺寸/展开面积及费用。",
            [f"estimate.costs[{index}].target" for index in item_materials],
            action="complete_component_costs")
    framed_panel = (re.search(r"面板|面层|发光板", source_text)
                    and re.search(r"边框|框架|龙骨|吊管|圆管", source_text)
                    and "亚克力" in source_text and "不锈钢" in source_text)
    whole_finished = any(cost["kind"] == "finished" and cost.get("target") == "item"
                         and not (cost.get("reference") or {}).get("part") for cost in costs)
    material_targets = {cost.get("target_id") or cost.get("target_index")
                        for cost in costs if cost.get("target") == "component"
                        and cost["kind"] in ("material", "finished")}
    if (line.rate_evidence_required and framed_panel and not whole_finished
            and (len(line.components) < 2 or len(material_targets) < 2)):
        raise PricingError("missing_component_costs",
            "原文包含亚克力面板及不锈钢边框/支承结构，不能用单一面板的材料和表面处理费冒充整件报价；"
            "须分别建立实际部件并列全材料加工、光源等费用，或引用适用的整套成品价格",
            ["components", "estimate.costs"], action="complete_component_costs")
    led_required = re.search(r"LED.{0,8}(?:模组|灯组|灯带|光源)|(?:内置|内藏|防水)LED",
                             source_text, re.I)
    led_costs = [cost for cost in costs if cost["kind"] != "power"
                 and re.search(r"LED|灯组|灯带|光源", f'{cost["label"]} {cost["reason"]}', re.I)]
    if led_required and not whole_finished and not led_costs:
        raise PricingError("missing_light_source_cost",
            "原图要求LED光源/模组，尚未单独计入光源材料及其安装范围；"
            "电源费用不能冒充包含LED灯组，须补充适用的光源费率、用量及范围依据。",
            ["estimate.costs"], action="complete_component_costs")
    fabrication = re.findall(r"切割|焊接|镶嵌|烤漆|烤金属漆|丝网印刷|丝印|阳极氧化|蚀刻|"
                             r"雕刻|凹刻|防氧化|电镀", source_text)
    processing = [c for c in costs if c["kind"] in ("process", "finished")
                  or "process" in c.get("included_kinds", [])
                  or "process" in (c.get("reference") or {}).get("included_kinds", [])]
    if fabrication and not processing:
        raise PricingError("missing_manufacturing_costs",
            "方案仅覆盖材料，原文加工工序尚未计费：" + "、".join(dict.fromkeys(fabrication)),
            ["estimate.costs"], action="complete_component_costs")
    for name, aliases in {"丝网印刷": ("丝网印刷", "丝印"), "盲文": ("盲文",),
                          "磁铁": ("磁铁",)}.items():
        if not any(alias in source_text for alias in aliases):
            continue
        candidates = costs if name == "磁铁" else processing
        if not any(any(alias in f'{cost["label"]} {cost["reason"]}' for alias in aliases)
                   for cost in candidates):
            raise PricingError("missing_manufacturing_costs",
                f"费用尚未说明原文要求的{name}由哪项承担；材料费标签不能冒充加工费",
                ["estimate.costs"], action="complete_component_costs")
