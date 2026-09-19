"""Material-rate references are never a finished assembly quotation."""
import re
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select

from .models import PriceItem, Product
from .quote_engine import price_source


MATERIALS = {
    "304不锈钢": "304#", "304#不锈钢": "304#", "304": "304#", "304#": "304#",
    "201不锈钢": "201#", "201#不锈钢": "201#", "201": "201#", "201#": "201#",
    "亚克力": "亚克力", "透明亚克力": "亚克力", "镀锌板": "镀锌板",
    "H62黄铜": "H62黄铜", "6061铝": "6061铝",
}
PROCESSES = {"UV印刷": "UV打印", "UV喷印": "UV打印"}


def material_family(value):
    compact = value.replace(" ", "")
    if compact in MATERIALS:
        return MATERIALS[compact]
    steel = re.search(r"(?<!\d)(304|201)#?(?:原色)?不锈钢", compact)
    if steel:
        return steel[1] + "#"
    if "亚克力" in compact:
        return "亚克力"
    return value


def surface_processes(values):
    """Only identify named surface processes; keep all other work unpriced."""
    result = []
    for value in values:
        if value == "无":
            result.append(value)
        elif value in PROCESSES:
            result.append(PROCESSES[value])
        else:
            result.extend(re.findall(r"烤漆|UV打印|UV印刷|UV喷印|蚀刻填漆/丝印|电镀/喷镀/水转印", value))
    return [PROCESSES.get(value, value) for value in result]


def analyze_components(db, line):
    results = []
    for component in line.components:
        material = material_family(component.material)
        missing = []
        if not component.evidence:
            missing.append("部件缺少原文来源")
        if not material:
            missing.append("部件材质/牌号")
        if component.thickness_mm is None:
            missing.append("部件板厚")
        if not component.processes:
            missing.append("部件表面工艺")
        if component.geometry != "flat_rectangle":
            missing.append("异形/壳体的实际计价面积或展开尺寸")
        elif component.width_mm is None or component.height_mm is None:
            missing.append("该部件实际宽高")
        if component.quantity_per_item is None:
            missing.append("每件产品的部件用量")
        missing.extend(component.uncertainties)
        if material != component.material:
            missing.append("按材料类别检索，原文颜色、等级及其他限定需核实")
        processes = surface_processes(component.processes)
        if component.processes and component.processes != processes:
            missing.append("原文加工工序、色号及未匹配的表面处理尚未计价")
        candidates = []
        if material and component.thickness_mm is not None:
            stmt = select(PriceItem).join(Product).where(
                Product.name == "材料+表面处理建议价", PriceItem.status == "active",
                PriceItem.material == material, PriceItem.thickness_mm == component.thickness_mm,
                PriceItem.unit == "m2", PriceItem.amount.is_not(None),
            ).order_by(PriceItem.id)
            for price in db.scalars(stmt):
                stored = [price.attributes.get(k, "") for k in ("process_1", "process_2")]
                stored = [p for p in stored if p and p != "无"]
                requested = [p for p in processes if p != "无"]
                if processes and stored != requested:
                    continue
                candidates.append({
                    "id": price.id, "revision": price.revision, "amount": str(price.amount),
                    "unit": price.unit, "process": price.process, "notes": price.notes,
                    "source": price_source(db, price),
                })
        if not candidates:
            missing.append("库内无匹配的材料/板厚/工艺建议单价")
        elif len(candidates) > 1:
            missing.append("存在多个建议单价，需确认工艺次序及适用条件")
        cost_per_item = None
        cost_formula = ""
        # A material reference needs its own geometry and usage, never the assembly's bounds.
        if (len(candidates) == 1 and component.evidence
                and component.geometry == "flat_rectangle"
                and component.width_mm is not None and component.height_mm is not None
                and component.quantity_per_item is not None and processes
                and component.processes == processes and not component.uncertainties):
            area = component.width_mm * component.height_mm / Decimal(1_000_000)
            rate = Decimal(candidates[0]["amount"])
            cost = rate * area * component.quantity_per_item
            if Decimal(0) <= cost <= Decimal("999999999999.99"):
                cost_per_item = str(cost.quantize(Decimal(".01"), rounding=ROUND_HALF_UP))
                cost_formula = f"{rate} × {area} × {component.quantity_per_item} = {cost_per_item}"
        conditions = ["仅此部件材料及匹配表面处理的成本参考；不含损耗、其他加工、组装、利润及未明确商业费用。"]
        if material != component.material:
            conditions.append(f"按材料类别 {material} 检索，原文颜色、等级等限定仍需复核。")
        results.append({
            "name": component.name, "material": material,
            "thickness_mm": str(component.thickness_mm) if component.thickness_mm is not None else None,
            "original_processes": component.processes,
            "references": candidates[:6], "candidate_count": len(candidates),
            "missing": list(dict.fromkeys(missing)),
            "cost_per_item": cost_per_item, "cost_formula": cost_formula,
            "estimate_conditions": conditions,
        })
    return {
        "components": results,
        "notice": "材料成本参考不是成品报价，未计入成品合计；每件成本不代表已确认成品数量。"
                  "需确认计价面积、用量、加工/组装及其他商业费用后才能计算成品金额。",
        "missing": [] if results else ["尚未形成有来源的材料部件清单，不能用单条材料价替代组合成品价"],
    }
