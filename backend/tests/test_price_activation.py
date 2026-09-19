from sqlalchemy import func, select

from app.models import AuditLog, PriceItem, PriceRevision
from app.quote_engine import calculate_line
from app.requirements_schemas import RequirementLine
from app.services import activate_draft_prices, snapshot


def test_new_price_defaults_active_but_explicit_status_is_respected(client, price_input):
    payload = {k: v for k, v in price_input.items() if k != "status"}
    assert client.post("/api/prices", json=payload).json()["status"] == "active"
    assert client.post("/api/prices", json={**payload, "status": "draft"}).json()["status"] == "draft"
    assert client.post("/api/prices", json={**payload, "status": "inactive"}).json()["status"] == "inactive"


def test_bulk_activation_audited_atomic_idempotent(client, price_input, test_db):
    draft = client.post("/api/prices", json={**price_input, "unit": "unknown",
                                           "price_kind": "starting"}).json()
    inactive = client.post("/api/prices", json={**price_input, "status": "inactive"}).json()
    with test_db.begin() as db:
        before = snapshot(db.get(PriceItem, draft["id"]))
        assert activate_draft_prices(db, "用户要求默认全部启用") == 1
        after = snapshot(db.get(PriceItem, draft["id"]))
        for key in before.keys() - {"status", "revision", "updated_at"}:
            assert before[key] == after[key]
        assert after["revision"] == before["revision"] + 1
        assert after["status"] == "active"
        assert db.get(PriceItem, inactive["id"]).status == "inactive"
        assert activate_draft_prices(db, "重复执行") == 0
        db.flush()
        assert db.scalar(select(func.count()).select_from(PriceRevision).where(
            PriceRevision.price_item_id == draft["id"])) == 2
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "activate"))
        assert audit.before["status"] == "draft" and audit.after["status"] == "active"
    with test_db() as db:
        line = RequirementLine(id="test", product=price_input["product"], quantity=1,
                               confirmed=True, selected_price_id=draft["id"], selected_price_revision=2,
                               language="en", price_review_note="测试计价校验")
        result = calculate_line(db, line)
        assert result["amount"] is None
        assert "起价或参考价不能用于标准计算" in result["blockers"]
        assert "单价或计价单位未确认" in result["blockers"]


def test_active_placeholder_and_unknown_language_still_block_quote(client, price_input, test_db):
    price = client.post("/api/prices", json={**price_input, "status": "active",
        "product": "名称待确认", "language": "unknown", "unit": "piece"}).json()
    with test_db() as db:
        line = RequirementLine(id="test", product="名称待确认", quantity=1, confirmed=True,
                               selected_price_id=price["id"], selected_price_revision=price["revision"],
                               language="unknown", price_review_note="核对原表")
        result = calculate_line(db, line)
        assert result["amount"] is None
        assert "价格产品名称待确认" in result["blockers"]
        assert "价格文字类型未确认" in result["blockers"]
