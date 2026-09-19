from decimal import Decimal, ROUND_HALF_UP
import re

from sqlalchemy import or_, select

from .models import PriceItem, PricingRule, Product, SourceDocument, SourceRecord
from .requirements_schemas import RequirementLine
from .services import snapshot
from .pricing_scope import category_matches, rule_scope


def candidate_prices(db, line, query=""):
    term = (query or line.pricing_category or line.product).strip()
    if not term:
        return []
    stmt = select(PriceItem).join(Product).where(
        PriceItem.status != "inactive",
        or_(Product.name.contains(term, autoescape=True), PriceItem.material.contains(term, autoescape=True)),
    ).order_by(PriceItem.id).limit(200)
    candidates = []
    for price in db.scalars(stmt):
        product = db.get(Product, price.product_id)
        mismatches = []
        if line.material and price.material and line.material != price.material:
            mismatches.append("材质不同")
        if line.thickness_mm and price.thickness_mm and line.thickness_mm != price.thickness_mm:
            mismatches.append("厚度不同")
        if line.language != "unknown" and price.language not in ("all", line.language):
            mismatches.append("文字类型不同")
        value = snapshot(price)
        value.update(product=product.name, mismatches=mismatches, source=price_source(db, price))
        candidates.append(value)
    candidates.sort(key=lambda x: (len(x["mismatches"]), x["status"] != "active", x["id"]))
    return candidates[:40]


def price_source(db, price):
    if not price.source_record_id:
        return None
    record = db.get(SourceRecord, price.source_record_id)
    doc = db.get(SourceDocument, record.document_id)
    return {"filename": doc.filename, "sheet": record.sheet, "cell": price.source_cell,
            "record_id": record.id}


def missing_requirements(line: RequirementLine):
    missing = []
    if not line.product:
        missing.append("产品名称")
    if line.quantity is None:
        missing.append("单件数量")
    if not line.confirmed:
        missing.append("需求人工确认")
    return missing


def automatic_blockers(db, line, price):
    blockers = []
    if not line.confirmed and not (line.evidence or line.text_evidence):
        blockers.append("需求缺少来源，请补充资料")
    if not category_matches(line, db.get(Product, price.product_id).name):
        blockers.append("报价类别尚未匹配或缺少做法依据；客户项目名称无需与价格名称相同")
    if line.material != price.material:
        blockers.append("材质未明确匹配")
    if line.thickness_mm != price.thickness_mm:
        blockers.append("厚度未明确匹配")
    if line.process != price.process:
        blockers.append("工艺未明确匹配")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*cm", price.spec, re.I)
    measure = line.billing_length_mm or (max(line.width_mm, line.height_mm) if line.width_mm and line.height_mm else None)
    if match and measure is not None and not Decimal(match[1]) <= measure / 10 <= Decimal(match[2]):
        blockers.append("实际尺寸不在价格规格档位内，请选择适用档位")
    return blockers


def pricing_warnings(db, line, price=None):
    warnings = list(line.uncertainties)
    if price:
        if price.spec or price.quality or price.notes or price.review_reason == "待确认":
            warnings.append("按候选价格先估价，规格/品质/备注条件待复核：" +
                            "；".join(v for v in (price.spec, price.quality, price.notes) if v)[:1000])
        rules = list(db.scalars(select(PricingRule).where(rule_scope(db, price)).order_by(PricingRule.id).limit(6)))
        if rules:
            warnings.append("相关规则尚未计入本次估价，待人工补充：" + "；".join(r.name for r in rules))
    return list(dict.fromkeys(warnings))


def unit_price_usable(result):
    return result.get("unit_price") is not None and not any(
        blocker != "单件数量" for blocker in result["blockers"])


def calculate_line(db, line: RequirementLine, automatic=False):
    if line.estimate is not None:
        from .estimate_pricing import calculate_estimate
        return calculate_estimate(db, line, calculate_line)
    blockers = missing_requirements(line)
    if automatic and "需求人工确认" in blockers:
        blockers.remove("需求人工确认")
    price = db.get(PriceItem, line.selected_price_id) if line.selected_price_id else None
    result = {"line_id": line.id, "product": line.product, "requirement": line.model_dump(mode="json"),
              "blockers": blockers, "warnings": pricing_warnings(db, line, price),
              "amount": None, "unit_price": None, "unit_formula": "",
              "fixed_charges": str(sum((c.amount for c in line.extras if c.basis == "total"), Decimal(0))),
              "price": None, "formula": ""}
    if line.manual_unit_price is not None:
        if len(line.manual_price_note.strip()) < 2:
            blockers.append("人工补价需填写依据及包含范围")
        if any(b != "单件数量" for b in blockers):
            return result
        unit = line.manual_unit_price + sum(
            (c.amount for c in line.extras if c.basis == "per_piece"), Decimal(0))
        if unit > Decimal("999999999999.99"):
            blockers.append("金额超出允许范围")
            return result
        result.update(unit_price=str(unit.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                      unit_formula=f"{line.manual_unit_price} + {unit - line.manual_unit_price} = {unit}",
                      manual_price=True,
                      warnings=[*line.uncertainties, "人工补价：" + line.manual_price_note])
        if line.quantity is None:
            return result
        extra = sum((charge.amount * (line.quantity if charge.basis == "per_piece" else 1)
                     for charge in line.extras), Decimal(0))
        total = (line.manual_unit_price * line.quantity + extra).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if total > Decimal("999999999999.99"):
            blockers.append("金额超出允许范围")
            return result
        result.update(amount=str(total), manual_price=True,
                      warnings=[*line.uncertainties, "人工补价：" + line.manual_price_note],
                      formula=f"{line.manual_unit_price} × {line.quantity} + {extra} = {total}")
        return result
    if not price:
        blockers.append("尚未选择价格")
        return result
    if automatic:
        blockers.extend(automatic_blockers(db, line, price))
    result["price"] = {**snapshot(price), "product": db.get(Product, price.product_id).name,
                       "source": price_source(db, price)}
    result["rule_revisions"] = [[r.id, r.revision] for r in db.scalars(
        select(PricingRule).where(rule_scope(db, price)).order_by(PricingRule.id))]
    if price.status != "active":
        blockers.append("所选价格尚未启用")
    if "待确认" in db.get(Product, price.product_id).name:
        blockers.append("价格产品名称待确认")
    if price.language == "unknown":
        blockers.append("价格文字类型未确认")
    if price.revision != line.selected_price_revision:
        blockers.append("价格版本已变化，需重新选择并确认")
    if price.price_kind not in ("standard", "suggested"):
        blockers.append("起价或参考价不能用于标准计算")
    if price.amount is None or price.unit == "unknown":
        blockers.append("单价或计价单位未确认")
    if len(line.price_review_note.strip()) < 2:
        blockers.append("尚未确认规格档位、材质、工艺及附加规则的适用性")
    if line.material and price.material and line.material != price.material:
        blockers.append("需求材质与价格材质不一致")
    if line.thickness_mm and price.thickness_mm and line.thickness_mm != price.thickness_mm:
        blockers.append("需求厚度与价格厚度不一致")
    if price.language not in ("all", line.language):
        blockers.append("文字类型不匹配或未确认")
    measure = Decimal(1)
    if price.unit in ("cm", "m"):
        if line.billing_length_mm is None:
            blockers.append("缺少每件计价长度（mm）")
        else:
            measure = line.billing_length_mm / (10 if price.unit == "cm" else 1000)
    elif price.unit == "m2":
        if not line.width_mm or not line.height_mm:
            blockers.append("缺少单件计价宽、高（mm）")
        else:
            measure = line.width_mm * line.height_mm / Decimal(1_000_000)
    elif price.unit not in ("piece", "set"):
        blockers.append("不支持的计价单位")
    if any(b != "单件数量" for b in blockers):
        return result
    per_piece_extra = sum((c.amount for c in line.extras if c.basis == "per_piece"), Decimal(0))
    unit = price.amount * measure + per_piece_extra
    if unit > Decimal("999999999999.99"):
        blockers.append("金额超出允许范围")
        return result
    result.update(unit_price=str(unit.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                  unit_formula=f"{price.amount} × {measure} + {per_piece_extra} = {unit}")
    if line.quantity is None:
        return result
    quantity = Decimal(line.quantity)
    base = price.amount * measure * quantity
    extra = sum((charge.amount * (quantity if charge.basis == "per_piece" else 1)
                 for charge in line.extras), Decimal(0))
    total = (base + extra).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if total > Decimal("999999999999.99"):
        blockers.append("金额超出允许范围")
        return result
    result.update(amount=str(total), base_amount=str(base.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                  extra_amount=str(extra), billing_measure=str(measure),
                  formula=f"{price.amount} × {measure} × {line.quantity} + {extra} = {total}")
    return result


def build_quote(db, requirements, automatic=False):
    from .component_pricing import analyze_components
    lines = [calculate_line(db, RequirementLine.model_validate(line), automatic) for line in requirements]
    for line in lines:
        if automatic and unit_price_usable(line) and not line.get("manual_price") and not line.get("estimated"):
            requirement = RequirementLine.model_validate(line["requirement"])
            price = db.get(PriceItem, requirement.selected_price_id)
            eligible = 0
            for other in db.scalars(select(PriceItem).where(
                    PriceItem.product_id == price.product_id, PriceItem.status == "active")):
                candidate = requirement.model_copy(update={
                    "selected_price_id": other.id, "selected_price_revision": other.revision})
                eligible += unit_price_usable(calculate_line(db, candidate, automatic=True))
                if eligible > 1:
                    line["blockers"].append("存在多个适用价格，需要人工确认")
                    line["amount"] = line["unit_price"] = None
                    break
        line["component_analysis"] = analyze_components(db, RequirementLine.model_validate(line["requirement"]))
        line["estimate_conditions"] = list(line["warnings"])
        if line.get("estimated"):
            line["estimate_conditions"].append(line["estimate_scope"])
        if line["unit_price"] is not None:
            line["estimate_conditions"].append("成品单价包含每件附加费；整单附加费另列，不分摊至单价。")
            if line["requirement"]["quantity"] is None and not line.get("estimated"):
                line["estimate_conditions"].append("数量未确认，仅报成品单价，未计入成品小计。")
        line["estimate_conditions"].append("税费、运费、安装费仅按明确报价依据计入，未明确部分待核价。")
    subtotal = sum((Decimal(line["amount"]) for line in lines if line["amount"] is not None), Decimal(0))
    complete = bool(lines) and all(not line["blockers"] for line in lines)
    review_items = []
    for line in lines:
        if not line.get("estimated"):
            continue
        for item in line.get("estimate_review", []):
            review_items.append({"line_id": line["line_id"], "product": line["product"], **item})
        for item in line.get("estimate_costs", []):
            review_items.append({"line_id": line["line_id"], "product": line["product"], "kind": "cost", **item})
        review_items.append({"line_id": line["line_id"], "product": line["product"], "kind": "scope",
                             "label": "报价范围", "value": line["estimate_scope"], "reason": "须审核包含及未包含费用"})
    return {"lines": lines, "known_subtotal": str(subtotal), "total": str(subtotal) if complete else None,
            "currency": "CNY", "complete": complete,
            "priced_count": sum(line["amount"] is not None for line in lines),
            "unit_priced_count": sum(line["unit_price"] is not None for line in lines),
            "pending_count": sum(line["amount"] is None for line in lines),
            "confirmation_items": confirmation_items(lines),
            "review_items": review_items, "has_estimates": any(line.get("estimated") for line in lines),
            "requires_review": bool(review_items) or any(line["warnings"] for line in lines)}


def confirmation_items(lines):
    grouped = {}
    for line in lines:
        for blocker in line["blockers"]:
            owner = "报价管理员" if any(word in blocker for word in (
                "价格", "单价", "规则", "人工", "计价单位")) else "需求方"
            key = (owner, blocker)
            grouped.setdefault(key, []).append({"line_id": line["line_id"], "product": line["product"]})
    return [{"owner": owner, "question": question, "lines": affected}
            for (owner, question), affected in grouped.items()]


def quote_stale(db, quote, job):
    if quote.job_revision != job.revision:
        return True
    for line in quote.payload.get("lines", []):
        if line.get("estimated"):
            requirement = RequirementLine.model_validate(line["requirement"])
            current = calculate_line(db, requirement, automatic=True)
            if current["blockers"] or current["amount"] != line["amount"]:
                return True
            # Validate recorded standard-price and rule dependencies as well as estimate cost references.
            if current.get("price") != line.get("price") or current.get("rule_revisions") != line.get("rule_revisions"):
                return True
            continue
        recorded = line.get("price")
        if recorded:
            price = db.get(PriceItem, recorded["id"])
            if not price or price.revision != recorded["revision"] or price.status != "active":
                return True
            if quote.payload.get("generated_by") == "agent":
                requirement = RequirementLine.model_validate(line["requirement"])
                if automatic_blockers(db, requirement, price):
                    return True
                current_rules = [[r.id, r.revision] for r in db.scalars(
                    select(PricingRule).where(rule_scope(db, price)).order_by(PricingRule.id))]
                if current_rules != line.get("rule_revisions", []):
                    return True
                count = 0
                for other in db.scalars(select(PriceItem).where(
                        PriceItem.product_id == price.product_id, PriceItem.status == "active")):
                    candidate = requirement.model_copy(update={
                        "selected_price_id": other.id, "selected_price_revision": other.revision})
                    if unit_price_usable(calculate_line(db, candidate, automatic=True)):
                        count += 1
                if count != 1:
                    return True
    return False
