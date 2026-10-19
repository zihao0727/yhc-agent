"""Review signals complement arithmetic checks; they are not market-price verification."""
from decimal import Decimal

from .config import settings


def review_signals(lines):
    peers = {}
    for line in lines:
        signals = []
        effective = line.get("effective_requirement", line["requirement"])
        if (effective.get("pricing_family") == "letter" and not effective.get("letter_groups")
                and (effective.get("character_count") or 0) > 1
                and effective.get("measurement_version", 2) < 3):
            signals.append({"code": "legacy_letter_scope", "cost_index": -1,
                            "message": "旧版多字方案未区分整件与单字尺寸，存在重复乘字数风险，须按新契约核对"})
        for index, cost in enumerate(line.get("estimate_costs", [])):
            if cost.get("script_run"):
                signals.append({"code": "custom_measurement", "cost_index": index,
                                "message": f'{cost["label"]}使用临时计算，须核实单位换算、几何假设和规则适用；脚本已随输入重算'})
            if cost["source"] == "agent_estimate" and cost.get("rate_basis_amount") is None:
                signals.append({"code": "unverified_rate", "cost_index": index,
                                "message": f'{cost["label"]}为自主估价，单价尚无结构化数值依据，须人工核价'})
            reference = cost.get("reference")
            if reference and cost["kind"] == "finished" and not reference.get("included_kinds"):
                signals.append({"code": "unknown_coverage", "cost_index": index,
                                "message": f'{cost["label"]}引用的成品价未结构化说明已含工序，须核对重复计费风险'})
            if (len(effective.get("components", [])) > 1 and cost.get("target", "item") == "item"
                    and cost["kind"] == "material" and cost.get("rate_unit") == "m2"):
                signals.append({"code": "assembly_area_used_for_material", "cost_index": index,
                                "message": f'{cost["label"]}按整件面积计材料，须核实实际部件面积及用量'})
            key = (effective.get("pricing_category"), effective.get("material"),
                   effective.get("thickness_mm"), cost["kind"], cost.get("rate_unit"))
            # No cross-category comparison when the category is unresolved.
            if key[0] and cost["source"] == "agent_estimate" and Decimal(cost["rate"]) > 0:
                peers.setdefault(key, []).append((line, index, Decimal(cost["rate"])))
        line["review_signals"] = signals
    threshold = Decimal(str(settings().quote_anomaly_ratio))
    for group in peers.values():
        if len({line["line_id"] for line, _, _ in group}) < 2:
            continue
        low, high = min(rate for _, _, rate in group), max(rate for _, _, rate in group)
        if high / low >= threshold:
            for line, index, _ in group:
                line["review_signals"].append({
                    "code": "peer_rate_variation", "cost_index": index,
                    "message": f"同类别、材质、厚度及费用单位的暂估费率差异达到{high / low:.2f}倍，"
                               "须核对规格、工艺和依据；该提示不代表价格必然错误"})
