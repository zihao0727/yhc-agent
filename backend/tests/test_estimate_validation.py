from copy import deepcopy
from decimal import Decimal

import pytest

from app import business_rules
from app.models import QuoteDraft, RequirementJob
from app.quote_engine import build_quote, quote_stale
from app.requirements_schemas import RequirementLine
from test_agent_loop import call, provider, seed, start
from test_requirements import create_job, save_lines


def estimate_cost(**changes):
    return {"label": "紫铜字材料", "rate": "200", "rate_unit": "piece",
            "expected_amount": "200", "quantity_formula": "=1", "kind": "material",
            "source": "agent_estimate", "reason": "单字材料约200元，暂定待审核", **changes}


def letter(costs=None, **changes):
    return {"id": "unitguard", "product": "电梯厅西立面效果", "quantity": 1,
            "material": "紫铜", "thickness_mm": "5", "height_mm": "250",
            "billing_length_mm": "250", "character_count": 1, "pricing_family": "letter",
            "business_rules_version": business_rules.VERSION,
            "estimate": {"scope": "仅制作费用，待审核", "costs": costs or [estimate_cost()]},
            **changes}


def calculate(test_db, raw):
    with test_db() as db:
        return build_quote(db, [raw], automatic=True)


def test_53795_regression_cannot_be_saved_as_complete_quote(test_db):
    raw = letter([
        estimate_cost(rate="2000", rate_unit="cm", quantity_formula="=letter_cm"),
        estimate_cost(label="雕刻造型", kind="process", rate="120", rate_unit="cm",
                      quantity_formula="=letter_cm", expected_amount="3000"),
        estimate_cost(label="拉丝", kind="process", rate="30", rate_unit="cm",
                      quantity_formula="=letter_cm", expected_amount="750"),
        estimate_cost(label="固定件", rate="45", expected_amount="45"),
    ])
    quote = calculate(test_db, raw)
    assert quote["total"] is None
    assert quote["lines"][0]["amount"] is None
    assert "校对金额200" in quote["lines"][0]["blockers"][0]
    assert "程序金额50000.00" in quote["lines"][0]["blockers"][0]
    assert quote["known_subtotal"] == "0"


@pytest.mark.parametrize("unit,formula", [
    ("piece", "=letter_cm"), ("set", "=letter_cm"), ("fixed", "=letter_cm"),
    ("cm", "=1"), ("m2", "=letter_cm"), ("m", "=billing_length_mm"),
    ("cm", "=billing_length_mm"), ("ratio", "=letter_cm"),
])
def test_unit_and_formula_must_agree(test_db, unit, formula):
    quote = calculate(test_db, letter([estimate_cost(rate_unit=unit, quantity_formula=formula)]))
    assert quote["total"] is None
    assert "单价单位" in quote["lines"][0]["blockers"][0]


@pytest.mark.parametrize("field,value,issue", [
    ("rate_unit", "unknown", "缺少单价单位"),
    ("expected_amount", None, "缺少费用校对金额"),
])
def test_current_estimates_require_units_and_control_totals(test_db, field, value, issue):
    quote = calculate(test_db, letter([estimate_cost(**{field: value})]))
    assert quote["total"] is None
    assert issue in quote["lines"][0]["blockers"][0]


def test_legacy_plan_keeps_its_original_calculation(test_db):
    cost = estimate_cost()
    cost.pop("rate_unit")
    cost.pop("expected_amount")
    quote = calculate(test_db, letter([cost], business_rules_version="legacy"))
    assert quote["total"] == "200.00"


def test_piece_amount_and_centimeter_rate_are_distinct(test_db):
    assert calculate(test_db, letter())["total"] == "200.00"
    cost = estimate_cost(rate="14", rate_unit="cm", expected_amount="350",
                         quantity_formula="=letter_cm")
    raw = letter([cost], quantity=2)
    quote = calculate(test_db, raw)
    assert quote["total"] == "700.00"
    assert quote["lines"][0]["estimate_costs"][0]["rate_unit"] == "cm"
    assert quote["lines"][0]["estimate_costs"][0]["expected_amount"] == "350"
    raw["billing_length_mm"] = "300"
    assert calculate(test_db, raw)["total"] is None
    raw["estimate"]["costs"][0]["expected_amount"] = "420"
    assert calculate(test_db, raw)["total"] == "840.00"


def test_processing_control_total_includes_difficulty(test_db):
    cost = estimate_cost(kind="process", expected_amount="600")
    assert calculate(test_db, letter([cost], difficulty="A"))["total"] == "600.00"
    cost["expected_amount"] = "200"
    assert calculate(test_db, letter([cost], difficulty="A"))["total"] is None


@pytest.mark.parametrize("formula", ["=billable_area_m2*2", "=2*area_m2", "=area_m2/(1/2)"])
def test_catalog_suggested_price_cannot_be_multiplied_again(
        client, price_input, test_db, formula):
    price = seed(client, price_input, amount="1499.999400", price_kind="suggested",
                 notes="（材料+工艺）*2=建议单价")
    cost = estimate_cost(source="catalog", rate=price["amount"], rate_unit="m2",
                         reference_id=price["id"], reference_revision=price["revision"],
                         expected_amount=None, quantity_formula=formula)
    quote = calculate(test_db, letter([cost], pricing_family="material",
                                      width_mm="689", height_mm="1012"))
    assert quote["total"] is None
    assert "额外倍数" in quote["lines"][0]["blockers"][0]
    cost["quantity_formula"] = "=area_m2"
    correct = calculate(test_db, letter([cost], pricing_family="material",
                                       width_mm="689", height_mm="1012"))
    assert correct["total"] == "1045.90"


def test_authoritative_catalog_unit_cannot_be_relabeled(client, price_input, test_db):
    price = seed(client, price_input, unit="cm", amount="8.5")
    cost = estimate_cost(source="catalog", rate="8.5", rate_unit="m2",
                         reference_id=price["id"], reference_revision=price["revision"],
                         quantity_formula="=letter_cm", expected_amount=None)
    assert calculate(test_db, letter([cost]))["total"] is None
    cost["rate_unit"] = "unknown"
    quote = calculate(test_db, letter([cost]))
    assert quote["total"] == "212.50"
    assert quote["lines"][0]["estimate_costs"][0]["rate_unit"] == "cm"


@pytest.mark.parametrize("formula", [
    "=area_m2*.25", "=0.25*area_m2", "=area_m2/4",
    "=width_mm*height_mm/4000000",
])
def test_actual_component_share_requires_measurement_basis(test_db, formula):
    cost = estimate_cost(rate="100", rate_unit="m2", quantity_formula=formula,
                         expected_amount="25")
    raw = letter([cost], pricing_family="material", width_mm=1000, height_mm=1000)
    assert calculate(test_db, raw)["total"] is None
    cost["measurement_note"] = "图纸该亚克力部件占成品面积25%，待审核"
    assert calculate(test_db, raw)["total"] == "25.00"


@pytest.mark.parametrize("cost", [
    estimate_cost(rate="5315", rate_unit="m2", quantity_formula="=0.1",
                  expected_amount="531.50"),
    estimate_cost(label="小面积暂定补足", rate="5900", rate_unit="m2",
                  quantity_formula="=0.1-area_m2", minimum_area_scope="per_piece",
                  expected_amount="236"),
    estimate_cost(minimum_area_scope="order"),
    estimate_cost(label="最小面积补足", rate="236", expected_amount="236"),
])
def test_area_bracket_is_not_a_minimum_charge(test_db, cost):
    raw = letter([deepcopy(cost)], pricing_family="material", width_mm=50, height_mm=70)
    assert calculate(test_db, raw)["total"] is None


def test_outsourcing_minimum_remains_available_only_for_defined_processes(test_db):
    cost = estimate_cost(label="外加工背胶", kind="outsourcing", outsourcing_type="adhesive",
                         rate="50", rate_unit="m2", quantity_formula="=billable_area_m2",
                         minimum_area_scope="per_piece", expected_amount="5")
    raw = letter([cost], pricing_family="material", width_mm=50, height_mm=70, quantity=2)
    assert calculate(test_db, raw)["total"] == "10.00"


def test_meters_and_ratios_preserve_correct_calculation(test_db):
    costs = [estimate_cost(rate="30", rate_unit="m", quantity_formula="=letter_cm/100",
                           expected_amount="60"),
             estimate_cost(label="制作利润", kind="profit", rate=".2", rate_unit="ratio",
                           quantity_formula="=unit_subtotal", expected_amount="12")]
    assert calculate(test_db, letter(costs, billing_length_mm=2000))["total"] == "72.00"


def test_letter_meters_include_all_characters(test_db):
    cost = estimate_cost(rate="30", rate_unit="m", quantity_formula="=letter_cm/100",
                         expected_amount="180")
    raw = letter([cost], billing_length_mm=2000, character_count=3)
    assert calculate(test_db, raw)["total"] == "180.00"
    cost["quantity_formula"] = "=billing_length_mm/1000"
    assert calculate(test_db, raw)["total"] is None


def test_non_letter_meters_use_item_length(test_db):
    cost = estimate_cost(rate="30", rate_unit="m", quantity_formula="=billing_length_mm/1000",
                         expected_amount="60")
    assert calculate(test_db, letter([cost], pricing_family="material",
                                     billing_length_mm=2000))["total"] == "60.00"


def test_old_unsafe_quote_becomes_stale_without_overwriting_its_payload(test_db):
    raw = letter([estimate_cost(rate="2000", quantity_formula="=letter_cm",
                               rate_unit="unknown", expected_amount=None)])
    legacy = deepcopy(raw)
    legacy["business_rules_version"] = "legacy"
    with test_db() as db:
        old_payload = build_quote(db, [legacy], automatic=True)
        old_payload["lines"][0]["requirement"] = raw
        quote = QuoteDraft(job_id=1, version=1, job_revision=1, payload=old_payload, terms="待审核")
        job = RequirementJob(id=1, title="测试", revision=1, requirements=[raw])
        before = deepcopy(quote.payload)
        assert quote_stale(db, quote, job)
        assert quote.payload == before


def test_unsafe_old_quote_cannot_be_approved_or_exported(client, test_db):
    raw = letter([estimate_cost(rate="2000", quantity_formula="=letter_cm",
                               rate_unit="unknown", expected_amount=None)])
    legacy = deepcopy(raw)
    legacy["business_rules_version"] = "legacy"
    with test_db.begin() as db:
        job = RequirementJob(title="异常报价回归", revision=1, requirements=[raw],
                             business_rules_version=business_rules.VERSION)
        db.add(job)
        db.flush()
        payload = build_quote(db, [legacy], automatic=True)
        payload["lines"][0]["requirement"] = raw
        payload.update(title=job.title, customer="", has_estimates=True)
        quote = QuoteDraft(job_id=job.id, version=1, job_revision=1, status="draft",
                           payload=payload, terms="历史待审核方案")
        db.add(quote)
        db.flush()
        quote_id = quote.id
    approval = client.post(f"/api/quotes/{quote_id}/approve", json={
        "note": "尝试审核", "confirmed_commercial_terms": True, "confirmed_estimates": True})
    assert approval.status_code == 409
    assert client.get(f"/api/quotes/{quote_id}/export?audience=review").status_code == 409
    with test_db() as db:
        stored = db.get(QuoteDraft, quote_id)
        assert stored.status == "draft"
        assert stored.payload == payload


def test_agent_repairs_rejected_cost_before_finishing_current_quote(client, monkeypatch):
    job = create_job(client)
    job = client.put(f'/api/jobs/{job["id"]}/pricing-policy', json={
        "revision": job["revision"], "reason": "测试采用当前口径"}).json()
    raw = letter()
    raw.pop("estimate")
    job = save_lines(client, job, [RequirementLine.model_validate(raw).model_dump(mode="json")])
    bad = letter([estimate_cost(rate="2000", rate_unit="cm", quantity_formula="=letter_cm")])["estimate"]
    good = letter()["estimate"]
    provider(monkeypatch, [call("complete_estimate", line_id="unitguard", estimate=bad),
                           call("complete_estimate", line_id="unitguard", estimate=good),
                           call("finish_quote")])
    result = start(client, job).json()
    assert result["agent_runs"][0]["status"] == "succeeded"
    steps = result["agent_runs"][0]["steps"]
    assert steps[0]["result"]["saved"] is False
    assert any(item["field"] == "expected_amount" for item in steps[0]["result"]["repair_fields"])
    assert steps[1]["result"]["saved"] is True
    assert result["quotes"][0]["payload"]["total"] == "200.00"
    assert result["requirements"][0]["estimate"]["costs"][0]["rate"] == "200"
