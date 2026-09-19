from datetime import datetime
from decimal import Decimal

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from .models import AuditLog, Category, PriceItem, PriceRevision, Product


def snapshot(obj):
    result = {}
    for attr in inspect(obj).mapper.column_attrs:
        value = getattr(obj, attr.key)
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat() + "Z"
        result[attr.key] = value
    return result


def product_for(db: Session, category: str, name: str):
    cat = db.scalar(select(Category).where(Category.name == category))
    if cat is None:
        cat = Category(name=category)
        db.add(cat)
        db.flush()
    product = db.scalar(select(Product).where(Product.category_id == cat.id, Product.name == name))
    if product is None:
        product = Product(category_id=cat.id, name=name)
        db.add(product)
        db.flush()
    return product


def log_change(db, obj, action, reason, before=None):
    db.flush()
    data = snapshot(obj)
    db.add(AuditLog(entity=obj.__tablename__, entity_id=obj.id, action=action,
                    reason=reason, before=before, after=data))
    if isinstance(obj, PriceItem):
        db.add(PriceRevision(price_item_id=obj.id, revision=obj.revision,
                             snapshot=data, reason=reason))


def activate_draft_prices(db, reason):
    """Enable drafts without rewriting pricing facts or resurrecting disabled rows."""
    items = list(db.scalars(select(PriceItem).where(
        PriceItem.status == "draft").order_by(PriceItem.id).with_for_update()))
    for item in items:
        before = snapshot(item)
        item.status = "active"
        log_change(db, item, "activate", reason, before)
    return len(items)
