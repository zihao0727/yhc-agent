from contextlib import contextmanager

from sqlalchemy import event

from app.models import PriceItem
from app.quote_engine import build_quote, candidate_prices, quote_stale
from app.requirements_schemas import RequirementLine
from test_agent_loop import seed
from test_requirements import line_input


@contextmanager
def count_queries(db):
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def summary(queries):
    return "\n".join(" ".join(q.split()).split(" FROM ", 1)[-1][:120] for q in queries)


def priced_line(price):
    return line_input(selected_price_id=price["id"], selected_price_revision=price["revision"],
                      price_review_note="Agent 自动核对")


def seed_product(client, price_input, alternatives):
    price = seed(client, price_input)
    for index in range(alternatives):
        seed(client, price_input, process=f"工艺{index}")
    return price


def test_ambiguity_check_queries_do_not_scale_with_alternatives(client, price_input, test_db):
    price = seed_product(client, price_input, 12)
    with test_db() as db:
        with count_queries(db) as queries:
            payload = build_quote(db, [priced_line(price)], automatic=True)
    line = payload["lines"][0]
    assert line["amount"] == "21.60" and not line["blockers"]
    assert len(queries) <= 10, summary(queries)


def test_ambiguity_still_detected(client, price_input, test_db):
    price = seed_product(client, price_input, 3)
    seed(client, price_input, amount="320")
    with test_db() as db:
        payload = build_quote(db, [priced_line(price)], automatic=True)
    line = payload["lines"][0]
    assert "存在多个适用价格，需要人工确认" in line["blockers"]
    assert line["amount"] is None and line["unit_price"] is None


def test_candidate_prices_batches_lookups(client, price_input, test_db):
    for index in range(15):
        seed(client, price_input, process=f"工艺{index}")
    with test_db() as db:
        line = RequirementLine.model_validate(line_input())
        with count_queries(db) as queries:
            result = candidate_prices(db, line)
    assert len(result) == 15
    assert all(item["product"] == "测试产品" for item in result)
    assert len(queries) <= 3, summary(queries)


def test_quote_stale_agent_quote(client, price_input, test_db):
    price = seed_product(client, price_input, 12)
    with test_db() as db:
        payload = build_quote(db, [priced_line(price)], automatic=True)
        payload["generated_by"] = "agent"

        class Quote:
            job_revision = 1

        class Job:
            revision = 1
        quote = Quote()
        quote.payload = payload
        with count_queries(db) as queries:
            assert not quote_stale(db, quote, Job())
        assert len(queries) <= 6, summary(queries)
        db.get(PriceItem, price["id"]).revision += 1
        assert quote_stale(db, quote, Job())
