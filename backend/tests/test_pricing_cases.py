from decimal import Decimal
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import PriceItem, PricingRule, SourceDocument
from app.pricing_cases import arithmetic_formula, evaluate_case, search_cases, CASE_KIND
from import_manual_cases import import_cases


@pytest.mark.parametrize("formula,expected", [
    ("=193*2.5*16+100*2", "7920"),
    ("=1.04*0.65*400+104*3*8+300-6.4", "3060"),
    ("=1.8*0.6*3200-6", "3450"),
    ("=(2+3)*4/2", "10"),
])
def test_historical_combination_formulas_use_decimal(formula, expected):
    assert arithmetic_formula(formula) == Decimal(expected)


@pytest.mark.parametrize("formula", [
    "=__import__('os').system('echo forbidden')", "=2**8", "=1/0", "=sum([1,2])",
    "=True+1", "='file.xlsx'!A1", "=1e999", "=I30+J30",
])
def test_formula_does_not_execute_code_or_treat_missing_cells_as_zero(formula):
    with pytest.raises(ValueError):
        arithmetic_formula(formula, {"I30": None, "J30": None})


def test_named_parameters_are_supported_without_arbitrary_functions():
    assert arithmetic_formula("=length_cm*rate+fee-adjustment",
                              {"length_cm": 193, "rate": 40, "fee": 200, "adjustment": 0}) == 7920


def test_real_manual_cases_import_reconcile_and_remain_reference_only(test_db):
    path = Path("C:/Users/PC/Documents/xwechat_files/wxid_j99f829slcdb12_f119/msg/attach/"
                "751479c7d336dd69f0df08a25b34c81e/2026-09/Rec/c239573a0ac8a015/F/4/报价清单-万胜张工.xlsx")
    if not path.exists():
        pytest.skip("Original manual quote not available")
    with test_db.begin() as db:
        report = import_cases(db, path)
        assert report["cases"] == 28 and report["unpriced"] == 1
        assert Decimal(report["reconciled_untaxed_subtotal"]) == 92154
        assert import_cases(db, path) == report
        assert db.scalar(select(func.count()).select_from(SourceDocument)) == 1
        assert db.scalar(select(func.count()).select_from(PriceItem)) == 0
        rules = list(db.scalars(select(PricingRule)))
        results = [evaluate_case(db, r) for r in rules]
        assert all(r.status == "draft" for r in rules)
        assert all(r["reference_only"] for r in results)
        tm = next(r for r in results if r["source_item"] == "9")
        assert tm["historical_unit_price"] is None
        assert "缺少公式参数" in str(tm["issues"])
        assert search_cases(db, "史努比")["cases"][0]["historical_unit_price"] == "3060.00"
        assert search_cases(db, "", 20)["total"] == 28
        assert len(search_cases(db, "", 20)["cases"]) == 8


def test_case_api_is_authenticated_and_rejects_malformed_case(client, test_db):
    with test_db.begin() as db:
        malformed = PricingRule(name="broken", rule_type="case", content=json.dumps({"kind": CASE_KIND}))
        db.add(malformed)
        db.flush()
        case_id = malformed.id
    assert client.get("/api/pricing-cases").json()["total"] == 0
    assert client.get(f"/api/pricing-cases/{case_id}").status_code == 404
    client.headers.pop("X-Admin-Token")
    assert client.get("/api/pricing-cases").status_code == 401
