from app.models import PricingRule, PriceItem, SourceDocument, SourceRecord
from app.pricing_scope import category_matches
from app.quote_engine import automatic_blockers, pricing_warnings
from app.requirements_schemas import RequirementLine
from test_agent_loop import seed, prepared, provider, call, selection, start
from test_requirements import save_lines


def test_customer_label_and_unassigned_rules_do_not_block_quote(client, price_input, monkeypatch, test_db):
    price = seed(client, price_input)
    job = prepared(client)
    line = job["requirements"][0]
    line.update(product="梦幻商店", pricing_category="测试产品", category_basis="测试产品")
    line["evidence"][0]["text"] += " 测试产品"
    job = save_lines(client, job, [line])
    with test_db.begin() as db:
        db.add(PricingRule(name="其他资料", content="别的产品每件加5元", product_id=None))
    provider(monkeypatch, [call("search_prices", query="测试"), selection(price), call("finish_quote")])
    result = start(client, job).json()
    assert result["quotes"][0]["payload"]["total"] == "21.60"
    assert result["quotes"][0]["payload"]["lines"][0]["product"] == "梦幻商店"


def test_category_label_without_source_does_not_authorize_price():
    line = RequirementLine(id="line1", product="客户项目", pricing_category="平面铝字",
                           category_basis="猜测为平面铝字", material="铝")
    assert not category_matches(line, "平面铝字")
    line.category_basis = "平面铝字"
    line.text_evidence = ["制作平面铝字"]
    assert category_matches(line, "平面铝字")
    assert not category_matches(line, "浮雕铝牌")


def test_only_own_sheet_rules_warn_without_blocking_estimate(client, price_input, test_db):
    price = seed(client, price_input)
    job = prepared(client)
    line = RequirementLine.model_validate(job["requirements"][0])
    with test_db.begin() as db:
        doc = SourceDocument(filename="source.xlsx", sha256="a" * 64)
        db.add(doc)
        db.flush()
        own = SourceRecord(document_id=doc.id, sheet="价格", row_number=1, cells={})
        other = SourceRecord(document_id=doc.id, sheet="别的案例", row_number=2, cells={})
        db.add_all([own, other])
        db.flush()
        p = db.get(PriceItem, price["id"])
        p.source_record_id = own.id
        db.add(PricingRule(name="其他页", content="附加费", source_record_id=other.id))
        db.add(PricingRule(name="原案例", content="历史", source_record_id=own.id, rule_type="case"))
        db.flush()
        assert not automatic_blockers(db, line, p)
        assert not pricing_warnings(db, line, p)
        db.add(PricingRule(name="同页待归属费用", content="附加费", source_record_id=own.id))
        db.flush()
        assert not automatic_blockers(db, line, p)
        assert "同页待归属费用" in str(pricing_warnings(db, line, p))
