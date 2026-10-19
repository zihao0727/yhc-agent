"""One deterministic retrieval contract for the catalog, agent and pricing packets."""
from dataclasses import dataclass, field
from decimal import Decimal
import re

from sqlalchemy import select

from .catalog_matching import (
    lexical_score, normalize, process_score, retrieval_targets, specification_match, material_grade,
)
from .models import PriceItem, Product, PricingRule
from .process_taxonomy import process_codes

SEARCH_VERSION = "catalog.v2"


def catalog_profile(price):
    attributes = price.attributes or {}
    group = attributes.get("cost_group") or (
        "material_process" if "material_price" in attributes else
        "assembly_reference" if price.unit == "unknown" or price.price_kind == "reference" else "finished")
    processes = " + ".join([price.process, *[str(attributes.get(key) or "")
                                            for key in ("process_1", "process_2")]])
    return {
        "version": SEARCH_VERSION, "cost_group": group, "unit": price.unit,
        "material": price.material, "grade": material_grade(price.material),
        "thickness_mm": str(price.thickness_mm) if price.thickness_mm is not None else None,
        "process_codes": sorted(set(attributes.get("process_codes") or process_codes(processes))),
        "included_process_codes": attributes.get("included_process_codes") or [],
        "scope": attributes.get("pricing_scope", ""),
        "reference_only": group == "assembly_reference" or price.price_kind in ("reference", "starting"),
        "source_record_id": price.source_record_id, "source_cell": price.source_cell,
        "revision": price.revision,
    }


@dataclass
class CatalogHit:
    price: PriceItem
    name: str
    score: int
    exact: bool = False
    specification: int = 0
    process: int = 0
    matched_targets: list = field(default_factory=list)
    retrieval_keys: list = field(default_factory=list)


@dataclass
class CatalogResult:
    hits: list
    scanned: int
    excluded: dict

    def audit(self):
        return {"version": SEARCH_VERSION, "complete": True, "scanned": self.scanned,
                "matched": len(self.hits), "excluded": self.excluded,
                "outcome": "candidates_found" if self.hits else
                    "specification_mismatch" if self.excluded.get("specification") else "no_matching_record",
                "notice": "检索完成不等于适用性确认；候选须通过规格、计量与费用覆盖校验。"}


def search_catalog(db, query="", material="", status="active", *, raw=None,
                   source_text="", category_id=None, product_id=None):
    stmt = select(PriceItem, Product.name).join(Product)
    if status == "all":
        stmt = stmt.where(PriceItem.status != "inactive")
    elif status:
        stmt = stmt.where(PriceItem.status == status)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if product_id is not None:
        stmt = stmt.where(PriceItem.product_id == product_id)
    thicknesses = set(re.findall(r"(?<![\d.*×x])(\d+(?:\.\d+)?)\s*mm(?![a-z])", query, re.I))
    thickness = (Decimal(next(iter(thicknesses))) if len(thicknesses) == 1
                 and not re.search(r"\d\s*[*×x]\s*\d", query, re.I) else None)
    targets = retrieval_targets(raw, source_text) if raw is not None else []
    words = " ".join([query, source_text, *(raw.get(key, "") or "" for key in
                      ("product", "material", "process", "notes"))]) if raw is not None else query
    hits, excluded, scanned = [], {"specification": 0, "text": 0}, 0
    for price, name in db.execute(stmt.order_by(PriceItem.id)):
        scanned += 1
        attributes = price.attributes or {}
        processes = " + ".join([price.process, *[str(attributes.get(key) or "")
                                                for key in ("process_1", "process_2")]])
        text = " ".join((name, price.material, price.spec, processes, price.notes))
        match = specification_match(material, thickness, price) if material or thickness is not None else 0
        if match < 0 or material and not match and normalize(material) not in normalize(price.material):
            excluded["specification"] += 1
            continue
        exact = bool(words.strip() and normalize(words) in normalize(text))
        process = process_score(words, processes) if words.strip() else 0
        score = lexical_score(words, name, text) + process if words.strip() else 0
        matched_targets, keys = [], []
        if raw is not None and catalog_profile(price)["cost_group"] == "material_process":
            matches = [(specification_match(target["material"], target["thickness"], price), target)
                       for target in targets]
            matches = [(value, target) for value, target in matches if value >= 0]
            if not matches:
                excluded["specification"] += 1
                continue
            score = max(value + lexical_score(target["text"], name, text)
                        for value, target in matches) + process
            match = max(value for value, _ in matches)
            matched_targets = list(dict.fromkeys(target["id"] for value, target in matches if value > 0))
            keys = [target["retrieval_key"] for value, target in matches if value > 0]
        if exact:
            score = max(1, score)
        if words.strip() and score <= 0:
            excluded["text"] += 1
            continue
        hits.append(CatalogHit(price, name, score, exact, match, process, matched_targets, keys))
    hits.sort(key=lambda hit: (-hit.exact, -hit.specification, -hit.process, -hit.score, hit.price.id))
    return CatalogResult(hits, scanned, excluded)


def search_rules(db, query="", offset=0, limit=20, active_only=False, status=None):
    stmt = select(PricingRule)
    if status:
        stmt = stmt.where(PricingRule.status == status)
    elif active_only:
        stmt = stmt.where(PricingRule.status == "active")
    else:
        stmt = stmt.where(PricingRule.status != "inactive")
    ranked = []
    for rule in db.scalars(stmt):
        text = rule.name + " " + rule.content
        exact = bool(query.strip() and normalize(query) in normalize(text))
        score = lexical_score(query, rule.name, text) if query.strip() else 0
        if query.strip() and not exact and score <= 0:
            continue
        ranked.append((exact, score, rule))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2].id))
    return len(ranked), [rule for _, _, rule in ranked[offset:offset + limit]]


def select_candidate_hits(result, limit=40):
    """Reserve one matching row per material/thickness before filling by shared rank."""
    selected, covered = [], set()
    for hit in result.hits:
        keys = set(hit.retrieval_keys)
        if keys - covered:
            selected.append(hit)
            covered.update(keys)
            if len(selected) >= limit:
                return selected
    identities = {hit.price.id for hit in selected}
    for hit in result.hits:
        if hit.price.id not in identities:
            selected.append(hit)
            if len(selected) >= limit:
                break
    return selected


def search_rows(db, query="", material="", status="active", offset=0):
    result = search_catalog(db, query, material, status)
    return len(result.hits), [(hit.price, hit.name) for hit in result.hits[offset:offset + 20]]


def packet_candidates(db, raw, sources=None):
    source_text = " ".join(e.text for e in (sources or {}).values() if e.source == "text")
    result = search_catalog(db, raw=raw, source_text=source_text)
    entries = []
    for hit in result.hits:
        price, name = hit.price, hit.name
        profile = catalog_profile(price)
        if price.amount is None and not any(
                (price.attributes or {}).get(part) is not None
                for part in ("material_price", "process_1_price", "process_2_price")):
            continue
        entries.append((hit.score, profile["cost_group"], {
            "id": price.id, "revision": price.revision, "product": name,
            "rate": str(price.amount) if price.amount is not None else None, "unit": price.unit,
            "material": price.material, "thickness_mm": profile["thickness_mm"], "spec": price.spec,
            "process": price.process, "language": price.language, "kind": price.price_kind,
            "notes": price.notes[:1200], "source_cell": price.source_cell,
            "source_record_id": price.source_record_id, "cost_group": profile["cost_group"],
            "matched_targets": hit.matched_targets, "_retrieval_keys": hit.retrieval_keys,
            "catalog_profile": profile,
            "cost_columns": {key: value for key, value in (price.attributes or {}).items()
                             if key in ("material_price", "process_1", "process_1_price",
                                        "process_2", "process_2_price")},
        }))
    entries.sort(key=lambda entry: (-entry[0], entry[2]["id"]))
    selected, counts, variants = [], {}, {}
    limits = {"finished": 6, "material_process": 8, "assembly_reference": 2}
    for target in retrieval_targets(raw, source_text):
        entry = next((entry for score, group, entry in entries if score > 0
                      and group == "material_process" and target["retrieval_key"] in entry["_retrieval_keys"]), None)
        if entry is not None and entry not in selected:
            selected.append(entry)
            counts["material_process"] = counts.get("material_process", 0) + 1
            identity = (entry["product"], entry["material"], entry["thickness_mm"])
            variants[identity] = variants.get(identity, 0) + 1
    limits["material_process"] = max(limits["material_process"], counts.get("material_process", 0))
    for score, group, entry in entries:
        identity = (entry["product"], entry["material"], entry["thickness_mm"])
        if entry in selected or score <= 0 or counts.get(group, 0) >= limits[group] or variants.get(identity, 0) >= 3:
            continue
        selected.append(entry)
        counts[group] = counts.get(group, 0) + 1
        variants[identity] = variants.get(identity, 0) + 1
    from .catalog_parts import PART_COLUMNS, part_rate
    for entry in selected:
        entry.pop("_retrieval_keys")
        price = db.get(PriceItem, entry["id"])
        entry["source_cost_columns"] = dict(entry["cost_columns"])
        for part in PART_COLUMNS.keys() & entry["cost_columns"].keys():
            try:
                entry["cost_columns"][part] = str(part_rate(price, part))
            except ValueError:
                entry["cost_columns"][part] = None
    return selected
