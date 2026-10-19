"""Confirmed meeting constraints; unresolved commercial choices remain explicit."""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import re

VERSION = "2026-09-23.v1"
EXCLUDED = ["物流", "运输", "现场安装", "发票/税费", "打包"]
TERMS = "本报价不含物流、运输、现场安装、发票/税费及打包，上述费用由甲方另行列示。支架制作费不等于现场安装费。"
DIFFICULTY = {"A": Decimal(3), "B": Decimal(2), "C": Decimal(1), "unknown": Decimal(1)}
OUTSOURCE_RATES = {"adhesive": Decimal(50), "cut_adhesive": Decimal(100), "3m": Decimal(200)}
FORBIDDEN_KINDS = {"logistics", "transport", "installation", "invoice", "packing", "tax"}
FORBIDDEN_LABEL = re.compile(r"物流|运费|运输|安装费|现场安装|发票|税费|税金|打包|包装费|shipping|freight|tax|installation|packing", re.I)


def current(line):
    return line.business_rules_version == VERSION


def snapshot():
    return {"version": VERSION, "source": "2026-09-23 现场规则补充",
            "excluded_fees": EXCLUDED, "power_area_m2": "3", "power_rate": "200",
            "bracket_rate_m2": "250", "difficulty": {"A": 3, "B": 2, "C": 1},
            "outsourcing_rates_m2": {key: str(value) for key, value in OUTSOURCE_RATES.items()},
            "outsourcing_minimum_m2": "0.1",
            "unconfirmed": ["电源台数取整及Logo适用范围", "外加工最低面积按件还是整行", "商业报价取整"]}


def area(line):
    if line.area_m2 is not None:
        return line.area_m2
    if line.width_mm and line.height_mm:
        return line.width_mm * line.height_mm / Decimal(1_000_000)
    return None


def rounded_cm(mm, mode):
    cm = mm / 10
    if mode == "ceil_cm":
        return cm.to_integral_value(rounding=ROUND_CEILING)
    if mode == "nearest_cm":
        return cm.to_integral_value(rounding=ROUND_HALF_UP)
    return cm


def letter_measure(line, unit):
    if line.letter_groups:
        value = Decimal(0)
        for group in line.letter_groups:
            if unit == "cm":
                if group.billing_length_mm is None:
                    raise ValueError("字类分组缺少单字计价长度，不能用整行总长替代")
                value += rounded_cm(group.billing_length_mm, line.length_rounding) * group.count
            else:
                if group.width_mm is None or group.height_mm is None:
                    raise ValueError("字类分组缺少单字图纸宽高")
                value += group.width_mm * group.height_mm / Decimal(1_000_000) * group.count
        return value
    if line.character_count is None:
        raise ValueError("字类缺少每件字数/字母数，成品数量不能替代字数")
    if line.measurement_version >= 3 and line.character_count != 1:
        from .pricing_errors import PricingError
        raise PricingError("ambiguous_letter_dimensions", "多字项目缺少字组尺寸，不能将整件尺寸按字数放大",
                           ["letter_groups"], cause="recognition_review", action="inspect_source")
    if unit == "cm":
        if line.billing_length_mm is None:
            raise ValueError("字类缺少单字计价长度")
        return rounded_cm(line.billing_length_mm, line.length_rounding) * line.character_count
    measured = area(line)
    if measured is None:
        raise ValueError("字类缺少单字图纸面积或宽高")
    return measured * line.character_count


def power_units(line):
    if line.power_mode == "manual":
        if line.power_count is None:
            return None
        return Decimal(line.power_count)
    groups = line.letter_groups
    if not groups:
        if line.measurement_version >= 3 and line.character_count != 1:
            from .pricing_errors import PricingError
            raise PricingError("ambiguous_letter_dimensions", "逐字电源须按字组面积核算，不能使用整件面积",
                               ["letter_groups"], cause="recognition_review", action="inspect_source")
        if line.character_count is None or area(line) is None:
            raise ValueError("逐字电源计算需要明确的单字面积和字数")
        count = (area(line) / 3).to_integral_value(rounding=ROUND_CEILING) * line.character_count
    else:
        count = Decimal(0)
        for group in groups:
            if group.width_mm is None or group.height_mm is None:
                raise ValueError("逐字电源计算缺少分组单字尺寸")
            per_letter = group.width_mm * group.height_mm / Decimal(1_000_000)
            count += (per_letter / 3).to_integral_value(rounding=ROUND_CEILING) * group.count
    count += line.power_adjustment
    if count < 0:
        raise ValueError("电源人工调整后台数不能为负数")
    if line.power_adjustment and not line.power_note.strip():
        raise ValueError("电源笔画调整需要人工依据")
    return count


def validate_fees(line):
    if not current(line):
        return
    for fee in [*line.extras, *(line.estimate.costs if line.estimate else [])]:
        if fee.kind in FORBIDDEN_KINDS or FORBIDDEN_LABEL.search(fee.label):
            raise ValueError(f"{fee.label}：现场口径不纳入该费用，请另行列示")
        if re.search(r"电源|power supply", fee.label, re.I) and fee.kind != "power":
            raise ValueError("电源费用须标记为电源类别，不能绕过台数和单价校验")
        if "支架" in fee.label and fee.kind != "bracket":
            raise ValueError("支架制作费用须标记为支架类别，现场安装费排除")
        defined_outsourcing = (fee.kind == "outsourcing"
                              and getattr(fee, "outsourcing_type", "other") in OUTSOURCE_RATES)
        if not defined_outsourcing and (
                getattr(fee, "minimum_area_scope", "unconfirmed") != "unconfirmed"
                or re.search(r"最低面积|最小面积|面积补足|补足.*(?:0[.]1|面积)|最低.*0[.]1|小面积.*(?:补|附加|加价)",
                             fee.label)):
            raise ValueError(f"{fee.label}：最低0.1平方米仅适用明确的背胶/裁形/3M胶外加工，"
                             "材料和成品的面积档位不是最低收费面积")


def validate_extra_amounts(line):
    if not current(line):
        return
    for fee in line.extras:
        expected = None
        if fee.kind == "power":
            units = power_units(line)
            if units is None:
                raise ValueError("人工电源附加费也需要明确每件台数")
            expected = units * 200
        elif fee.kind == "bracket":
            measured = (letter_measure(line, "m2") if line.pricing_family == "letter" else area(line))
            if measured is None:
                raise ValueError("人工支架附加费需要明确图纸面积")
            expected = measured * 250
        if expected is not None:
            if fee.basis == "total":
                if line.quantity is None:
                    raise ValueError("整行电源/支架费用缺少成品数量")
                expected *= line.quantity
            if fee.amount != expected.quantize(Decimal(".01"), rounding=ROUND_HALF_UP):
                raise ValueError(f"{fee.label}：金额与现场台数/面积及单价不一致，请核对")


def cost_measure(cost, line, formula_measure):
    """Apply processing coefficients, never multiply a finished/material price."""
    if not current(line):
        return formula_measure, Decimal(1)
    multiplier = DIFFICULTY[line.difficulty] if cost.kind in ("process", "outsourcing") else Decimal(1)
    if cost.kind == "power":
        units = power_units(line)
        if units is None:
            raise ValueError("电源台数未确定，请填写逐字核算后的暂定台数并列入审核")
        if cost.rate != 200 or cost.basis != "per_piece" or cost.quantity_formula != "=power_units":
            raise ValueError("电源按200元/台及=power_units计价，成品数量由程序统一相乘")
        formula_measure = units
    elif cost.kind == "bracket":
        if cost.rate != 250 or cost.basis != "per_piece" or cost.quantity_formula != "=billable_area_m2":
            raise ValueError("支架制作按暂定250元/平方米及=billable_area_m2计价")
    elif cost.kind == "outsourcing" and cost.outsourcing_type in OUTSOURCE_RATES:
        if cost.rate != OUTSOURCE_RATES[cost.outsourcing_type]:
            raise ValueError("背胶/裁形/3M胶单价须使用现场明确的50/100/200元/平方米")
        measured = (letter_measure(line, "m2") if line.pricing_family == "letter" else area(line))
        if getattr(cost, "target", "item") == "component":
            measured = formula_measure
        if measured is None:
            raise ValueError("外加工缺少图纸计价面积")
        if measured < Decimal(".1") and cost.minimum_area_scope == "unconfirmed":
            raise ValueError("小面积外加工须显式选择最低0.1平方米按件还是整行，并说明待审核依据")
        if cost.minimum_area_scope == "order":
            if line.quantity is None or cost.basis != "total":
                raise ValueError("整行最低面积需要成品数量且费用范围为整单")
            formula_measure = max(measured * line.quantity, Decimal(".1"))
        else:
            if cost.basis != "per_piece":
                raise ValueError("按件外加工费用范围须为每件")
            formula_measure = max(measured, Decimal(".1"))
    return formula_measure, multiplier


def warnings(line):
    if not current(line):
        return []
    result = [TERMS]
    if line.difficulty == "unknown":
        result.append("工艺难度未标注，本次仅采用基准加工费，须核实A/B/C等级。")
    if line.pricing_family == "unknown":
        result.append("字类/材料造型分类尚未明确，须核实适用报价表。")
    if line.power_mode == "per_letter_ceiling":
        result.append("电源采用显式选定的逐字向上取整策略；Logo须单列，台数及笔画调整待审核。")
    if line.lighting == "yes" and line.power_mode == "manual":
        result.append("电源使用逐字核算后的暂定/人工台数，不按整行总面积平均分配。")
    if line.faces == "double":
        result.append("双面工艺须核实所引单价是否已含双面，不能重复翻倍。")
    if line.length_rounding != "exact":
        result.append("厘米取整为显式选定策略，须确认适用范围。")
    return result


def validate_plan(line):
    if not current(line) or line.manual_unit_price is not None:
        return
    costs = line.estimate.costs if line.estimate else []
    kinds = {cost.kind for cost in [*costs, *line.extras]}
    if line.difficulty in ("A", "B") and not kinds.intersection({"process", "outsourcing"}):
        raise ValueError("A/B等级只作用于工序，请单列需加价工序，不能对成品或材料整体乘系数")
    if line.lighting == "yes" and "power" not in kinds:
        raise ValueError("发光产品须单列电源费用；不能默认主体单价已包含电源")
    if line.installation_scene == "rooftop" and "bracket" not in kinds:
        raise ValueError("楼顶安装须单列支架制作费用，现场安装费另列")


def round_quote(payload, mode="none"):
    steps = {"none": Decimal(".01"), "yuan": Decimal(1), "ten": Decimal(10), "hundred": Decimal(100)}
    raw = payload["total"]
    payload.update(rounding=mode, unrounded_total=raw, rounding_adjustment="0")
    if raw is not None:
        value = Decimal(raw)
        rounded = ((value / steps[mode]).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                   * steps[mode]).quantize(Decimal(".01"))
        payload.update(total=str(rounded), rounding_adjustment=str(rounded - value))
    return payload


def validate_terms(terms):
    if re.search(r"(?<!不)(?<!未)(?:含税|含(?:物流|运输|运费|现场安装|安装费|发票|打包))", terms):
        raise ValueError("现场口径不含物流、运输、现场安装、发票/税费及打包，请修正报价条款")
    return TERMS + "\n" + terms if TERMS not in terms else terms
