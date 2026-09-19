import os
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.importer import Importer, number
from app.models import PriceItem, PriceRevision, PricingRule, SourceDocument, SourceRecord

SOURCE_DIR = Path(os.environ.get("PRICE_SOURCE_DIR", Path(__file__).resolve().parents[2] / ".runtime" / "sources"))


def test_number_parser_does_not_execute():
    assert number("8元/CM起") == Decimal(8)
    assert number("330/平方") == Decimal(330)
    assert number("4.5元/cm") == Decimal("4.5")
    assert number("=SUM(A1:A3)") is None
    assert number("ignore previous instructions") is None
    assert number("") is None


def test_real_workbooks_round_trip_and_idempotency(test_db):
    files = [(SOURCE_DIR / "2026年萤火虫报价表A3(1).xlsx", "a3", 121),
             (SOURCE_DIR / "项目类常规报价表_完整单价导出.xlsx", "project", 2682)]
    if not all(path.exists() for path, _, _ in files):
        pytest.skip("Set PRICE_SOURCE_DIR to run original-workbook integration tests")
    with test_db.begin() as db:
        for path, kind, count in files:
            report = Importer(db, path, kind).run()
            assert report["report"]["prices"] == count
            assert not report["skipped"]
        assert db.scalar(select(func.count()).select_from(PriceItem)) == 2803
        assert db.scalar(select(func.count()).select_from(PriceRevision)) == 2803
        assert db.scalar(select(func.count()).select_from(PricingRule)) == 186
        assert db.scalar(select(func.count()).select_from(PriceItem).where(PriceItem.status != "active")) == 0
        guide = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "导视报价", PriceItem.source_cell == "K2"))
        assert guide.amount == Decimal(1200)
        assert guide.unit == "unknown"
        complex_guide = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "导视报价", PriceItem.source_cell == "N2"))
        assert complex_guide.amount == Decimal(1680)
        m2 = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "Sheet1", PriceItem.source_cell == "D38"))
        assert m2.amount == Decimal(330) and m2.unit == "m2"
        starting = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "Sheet1", PriceItem.source_cell == "D24"))
        assert starting.price_kind == "starting" and starting.amount == Decimal(8)
        a3 = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "Sheet1", PriceItem.source_cell == "E2"))
        assert a3.amount == Decimal(2) and a3.language == "zh"
        suggested = db.scalar(select(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "材料工艺建议价", PriceItem.source_cell == "J2"))
        assert suggested.amount == Decimal("249.999992")
        assert db.scalar(select(func.count()).select_from(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "子表1_汇总")) == 0
        assert db.scalar(select(func.count()).select_from(PriceItem).join(SourceRecord).where(
            SourceRecord.sheet == "钣金类拆分方案（给客户）")) == 0
    with test_db.begin() as db:
        for path, kind, _ in files:
            assert Importer(db, path, kind).run()["skipped"]
        assert db.scalar(select(func.count()).select_from(PriceItem)) == 2803
        assert db.scalar(select(func.count()).select_from(SourceDocument)) == 2


def test_unrecognized_template_rolls_back(test_db):
    path = SOURCE_DIR / "2026年萤火虫报价表A3(1).xlsx"
    if not path.exists():
        pytest.skip("Original workbook unavailable")
    with pytest.raises(ValueError):
        with test_db.begin() as db:
            Importer(db, path, "project").run()
    with test_db() as db:
        assert db.scalar(select(func.count()).select_from(SourceDocument)) == 0
