import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from .config import settings
from .database import get_db
from .models import AuditLog, Category, PriceItem, PriceRevision, PricingRule, Product, SourceDocument, SourceRecord
from .schemas import DeleteInput, PriceInput, RuleInput, Status
from .services import log_change, product_for, snapshot
from .upload_limits import RequestSizeLimit

@asynccontextmanager
async def lifespan(app):
    worker = None
    if settings().agent_background:
        from .agent_worker import start_worker
        worker = start_worker()
    yield
    if worker:
        worker[0].set()
        worker[1].join(timeout=3)


app = FastAPI(title="萤火虫报价库", version="0.2.0", docs_url="/api/docs",
              openapi_url="/api/openapi.json", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings().allowed_origins,
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "X-Admin-Token"])
app.add_middleware(RequestSizeLimit)
DB = Annotated[Session, Depends(get_db)]


def authorize(x_admin_token: Annotated[str | None, Header()] = None):
    token = settings().admin_token
    if not token:
        raise HTTPException(503, "尚未配置 ADMIN_TOKEN")
    if not x_admin_token or not secrets.compare_digest(token.encode(), x_admin_token.encode()):
        raise HTTPException(401, "管理口令无效")


AUTH = [Depends(authorize)]


@app.exception_handler(OperationalError)
def database_unavailable(request, exc):
    return JSONResponse(status_code=503, content={"detail": "数据库未连接，请检查后端连接配置及数据库迁移状态"})


@app.exception_handler(StaleDataError)
def stale_change(request, exc):
    return JSONResponse(status_code=409, content={"detail": "记录已被其他操作修改，请刷新后重试"})


@app.exception_handler(IntegrityError)
def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={"detail": "数据冲突，请刷新后检查重复记录或关联关系"})


@app.get("/api/health")
def health(db: DB):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "postgresql"}
    except OperationalError:
        return JSONResponse(status_code=503, content={"status": "database_unavailable", "database": "postgresql"})


@app.get("/api/session", dependencies=AUTH)
def session():
    return {"user": "local-admin"}


@app.get("/api/stats", dependencies=AUTH)
def stats(db: DB):
    counts = dict(db.execute(select(PriceItem.status, func.count()).group_by(PriceItem.status)).all())
    return {"prices": sum(counts.values()), "draft": counts.get("draft", 0),
            "active": counts.get("active", 0), "inactive": counts.get("inactive", 0),
            "products": db.scalar(select(func.count()).select_from(Product)),
            "rules": db.scalar(select(func.count()).select_from(PricingRule)),
            "documents": db.scalar(select(func.count()).select_from(SourceDocument))}


@app.get("/api/options", dependencies=AUTH)
def options(db: DB):
    return {
        "categories": [snapshot(c) for c in db.scalars(select(Category).order_by(Category.id))],
        "products": [snapshot(p) for p in db.scalars(select(Product).order_by(Product.id))],
    }


def source_info(db, record_id, cell=""):
    if record_id is None:
        return None
    record = db.get(SourceRecord, record_id)
    doc = db.get(SourceDocument, record.document_id)
    return {"filename": doc.filename, "sheet": record.sheet, "row": record.row_number,
            "cell": cell, "record_id": record.id}


def price_view(db, item, detail=False):
    data = snapshot(item)
    product = db.get(Product, item.product_id)
    category = db.get(Category, product.category_id)
    data.update(product=product.name, category=category.name, category_id=category.id,
                source=source_info(db, item.source_record_id, item.source_cell))
    if not detail:
        data.pop("attributes")
    return data


@app.get("/api/prices", dependencies=AUTH)
def prices(db: DB, q: str = Query("", max_length=200), category_id: int | None = None,
           product_id: int | None = None, status: Status | None = None,
           page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    stmt = select(PriceItem).join(Product)
    if q.strip():
        term = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(or_(*[c.ilike(term, escape="\\") for c in
                               [Product.name, PriceItem.material, PriceItem.spec, PriceItem.process]]))
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if product_id is not None:
        stmt = stmt.where(PriceItem.product_id == product_id)
    if status:
        stmt = stmt.where(PriceItem.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(PriceItem.id).offset((page - 1) * page_size).limit(page_size))
    return {"items": [price_view(db, item) for item in rows], "total": total}


@app.post("/api/prices", dependencies=AUTH, status_code=201)
def create_price(data: PriceInput, db: DB):
    product = product_for(db, data.category, data.product)
    item = PriceItem(product_id=product.id, **data.model_dump(exclude={"category", "product", "revision"}))
    db.add(item)
    log_change(db, item, "create", data.review_reason)
    db.commit()
    return price_view(db, item, True)


@app.get("/api/prices/{item_id}", dependencies=AUTH)
def get_price(item_id: int, db: DB):
    item = db.get(PriceItem, item_id)
    if not item:
        raise HTTPException(404, "价格记录不存在")
    return price_view(db, item, True)


@app.put("/api/prices/{item_id}", dependencies=AUTH)
def update_price(item_id: int, data: PriceInput, db: DB):
    item = db.get(PriceItem, item_id)
    if not item:
        raise HTTPException(404, "价格记录不存在")
    if data.revision != item.revision:
        raise HTTPException(409, "版本已变化，请刷新后重试")
    before = snapshot(item)
    item.product_id = product_for(db, data.category, data.product).id
    for key, value in data.model_dump(exclude={"category", "product", "revision"}).items():
        setattr(item, key, value)
    # Explicit increment also records a review which did not change the amount.
    item.revision += 1
    log_change(db, item, "update", data.review_reason, before)
    db.commit()
    return price_view(db, item, True)


@app.delete("/api/prices/{item_id}", dependencies=AUTH)
def deactivate_price(item_id: int, data: DeleteInput, db: DB):
    item = db.get(PriceItem, item_id)
    if not item:
        raise HTTPException(404, "价格记录不存在")
    if data.revision != item.revision:
        raise HTTPException(409, "版本已变化，请刷新后重试")
    before = snapshot(item)
    item.status = "inactive"
    item.review_reason = data.reason
    item.revision += 1
    log_change(db, item, "deactivate", data.reason, before)
    db.commit()
    return price_view(db, item)


@app.get("/api/prices/{item_id}/history", dependencies=AUTH)
def price_history(item_id: int, db: DB):
    return [snapshot(r) for r in db.scalars(select(PriceRevision).where(
        PriceRevision.price_item_id == item_id).order_by(PriceRevision.revision.desc()))]


@app.get("/api/rules", dependencies=AUTH)
def rules(db: DB, q: str = Query("", max_length=200), status: Status | None = None,
          page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    stmt = select(PricingRule)
    if q:
        stmt = stmt.where(or_(PricingRule.name.contains(q, autoescape=True),
                             PricingRule.content.contains(q, autoescape=True)))
    if status:
        stmt = stmt.where(PricingRule.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = []
    for r in db.scalars(stmt.order_by(PricingRule.id).offset((page - 1) * page_size).limit(page_size)):
        item = snapshot(r)
        item["source"] = source_info(db, r.source_record_id, r.source_cell)
        item["product"] = db.get(Product, r.product_id).name if r.product_id else "公共 / 待归属"
        items.append(item)
    return {"items": items, "total": total}


@app.get("/api/pricing-cases", dependencies=AUTH)
def pricing_cases(db: DB, q: str = Query("", max_length=150), offset: int = Query(0, ge=0, le=10000)):
    from .pricing_cases import search_cases
    return search_cases(db, q, offset)


@app.get("/api/pricing-cases/{case_id}", dependencies=AUTH)
def pricing_case(case_id: int, db: DB):
    from .pricing_cases import evaluate_case
    item = db.get(PricingRule, case_id)
    if not item:
        raise HTTPException(404, "案例不存在")
    result = evaluate_case(db, item)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


def apply_rule(db, data, item=None):
    if data.product_id and not db.get(Product, data.product_id):
        raise HTTPException(422, "产品不存在")
    if data.status == "active":
        raise HTTPException(422, "当前仅整理规则原文，尚未接入计价引擎，规则不能启用自动执行")
    before = snapshot(item) if item else None
    values = data.model_dump(exclude={"reason", "revision"})
    if item is None:
        item = PricingRule(**values)
        db.add(item)
    else:
        if item.revision != data.revision:
            raise HTTPException(409, "版本已变化，请刷新后重试")
        for key, value in values.items():
            setattr(item, key, value)
        item.revision += 1
    log_change(db, item, "update" if before else "create", data.reason, before)
    db.commit()
    return snapshot(item)


@app.post("/api/rules", dependencies=AUTH, status_code=201)
def create_rule(data: RuleInput, db: DB):
    return apply_rule(db, data)


@app.put("/api/rules/{item_id}", dependencies=AUTH)
def update_rule(item_id: int, data: RuleInput, db: DB):
    item = db.get(PricingRule, item_id)
    if not item:
        raise HTTPException(404, "规则不存在")
    return apply_rule(db, data, item)


@app.delete("/api/rules/{item_id}", dependencies=AUTH)
def deactivate_rule(item_id: int, data: DeleteInput, db: DB):
    item = db.get(PricingRule, item_id)
    if not item:
        raise HTTPException(404, "规则不存在")
    if item.revision != data.revision:
        raise HTTPException(409, "版本已变化，请刷新后重试")
    before = snapshot(item)
    item.status = "inactive"
    item.revision += 1
    log_change(db, item, "deactivate", data.reason, before)
    db.commit()
    return snapshot(item)


@app.get("/api/documents", dependencies=AUTH)
def documents(db: DB):
    return [snapshot(d) for d in db.scalars(select(SourceDocument).order_by(SourceDocument.id))]


@app.get("/api/sources/{record_id}", dependencies=AUTH)
def source_record(record_id: int, db: DB):
    record = db.get(SourceRecord, record_id)
    if not record:
        raise HTTPException(404, "来源记录不存在")
    return {**snapshot(record), "filename": db.get(SourceDocument, record.document_id).filename}


@app.get("/api/audit", dependencies=AUTH)
def audit(db: DB, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    return {"items": [snapshot(a) for a in db.scalars(stmt)],
            "total": db.scalar(select(func.count()).select_from(AuditLog))}


from .requirements_api import router as requirements_router

app.include_router(requirements_router, dependencies=AUTH)
