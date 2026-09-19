"""Separate customer labels, product categories, and source-scoped pricing rules."""
import re

from sqlalchemy import and_, or_, select

from .models import PricingRule, SourceRecord


def rule_scope(db, price):
    # An unassigned rule is not global. Keep unresolved notes from the price's own sheet.
    unresolved_source = False
    if price.source_record_id:
        record = db.get(SourceRecord, price.source_record_id)
        if record:
            ids = select(SourceRecord.id).where(SourceRecord.document_id == record.document_id,
                                                 SourceRecord.sheet == record.sheet)
            unresolved_source = and_(PricingRule.product_id.is_(None),
                                     PricingRule.source_record_id.in_(ids))
    return and_(PricingRule.status != "inactive", PricingRule.rule_type != "case",
                or_(PricingRule.product_id == price.product_id, unresolved_source))

def category_matches(line, product_name):
    if line.product == product_name:
        return True
    if line.pricing_category != product_name:
        return False
    if line.confirmed:
        return bool(line.category_basis)
    # The category must be grounded in a quoted specification, not just a model's label.
    basis = line.category_basis
    sources = [line.material, line.process, *(e.text for e in line.evidence), *line.text_evidence]
    names = [product_name, *re.split(r"[（()）]", product_name)]
    return bool(basis and any(basis in source for source in sources)
                and any(name and name in basis for name in names))
