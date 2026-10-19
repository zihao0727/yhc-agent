from decimal import Decimal

import pytest

from app import business_rules as rules
from app.quote_engine import build_quote
from app.requirements_schemas import RequirementLine, LetterGroup
from app.estimate_pricing import effective_line
from app.pricing_cases import arithmetic_formula
from test_requirements import create_job, save_lines


def cost(kind, rate, formula="=1", **kwargs):
    unit = ("m2" if "area_m2" in formula else
            "cm" if "letter_cm" in formula or "billing_length_mm/10" in formula else "piece")
    return {"label": {"power": "电源", "bracket": "支架制作"}.get(kind, kind),
            "kind": kind, "rate": str(rate), "rate_unit": unit, "quantity_formula": formula,
            "reason": "现场规则及图纸，暂定待审核", **kwargs}


def requirement(**kwargs):
    return RequirementLine.model_validate({
        "id": "meeting", "product": "现场测试", "quantity": 2,
        "business_rules_version": rules.VERSION, "pricing_family": "material",
        "difficulty": "C", "confirmed": True, **kwargs})


def priced(test_db, line):
    # These tests exercise meeting rules; supply the new independent control-total fields.
    if line.estimate and line.manual_unit_price is None:
        effective, _ = effective_line(line)
        inputs = {name: getattr(effective, name) for name in (
            "quantity", "width_mm", "height_mm", "billing_length_mm")}
        inputs.update(area_m2=rules.area(effective), unit_subtotal=Decimal(0),
                      fixed_subtotal=Decimal(0))
        for variable, unit in (("letter_cm", "cm"), ("billable_area_m2", "m2")):
            try:
                inputs[variable] = (rules.letter_measure(effective, unit)
                                    if effective.pricing_family == "letter" else rules.area(effective))
            except ValueError:
                inputs[variable] = None
        try:
            inputs["power_units"] = rules.power_units(effective)
        except ValueError:
            inputs["power_units"] = None
        for item in line.estimate.costs:
            if item.source != "agent_estimate":
                continue
            try:
                measure, multiplier = rules.cost_measure(
                    item, effective, arithmetic_formula(item.quantity_formula, inputs))
                item.expected_amount = (item.rate * measure * multiplier).quantize(Decimal(".01"))
            except ValueError:
                pass
    with test_db() as db:
        return build_quote(db, [line.model_dump(mode="json")], automatic=True)


def test_processing_coefficient_never_multiplies_finished_or_material(test_db):
    line = requirement(difficulty="A", estimate={"scope": "含制作，不含现场安装等排除费用",
        "costs": [cost("material", 100), cost("finished", 200), cost("process", 50)]})
    quote = priced(test_db, line)
    assert quote["total"] == "900.00"
    costs = quote["lines"][0]["estimate_costs"]
    assert [c["difficulty_multiplier"] for c in costs] == ["1", "1", "3"]
    assert costs[2]["resolved_formula"] == "50 × 1 × 3 = 150.00"
    assert costs[2]["parameters"]["unit_subtotal"] == "300.00"
    assert quote["business_rules"]["version"] == rules.VERSION


def test_assumptions_remain_separate_but_validate_full_plan(test_db):
    line = requirement(quantity=None, lighting="yes", power_count=None, difficulty="B",
        estimate={"scope": "制作与电源，人工待核", "assumptions": [
            {"field": "quantity", "value": "3", "reason": "暂定三处位置"},
            {"field": "power_count", "value": "2", "reason": "暂定逐字核算两台"}],
            "costs": [cost("process", 20), cost("power", 200, "=power_units")]})
    result = priced(test_db, line)
    assert result["total"] == "1320.00"
    assert result["lines"][0]["requirement"]["quantity"] is None
    assert result["lines"][0]["effective_requirement"]["quantity"] == 3


@pytest.mark.parametrize("kind", ["tax", "transport", "logistics", "installation", "invoice", "packing"])
def test_excluded_fees_are_blocked_even_with_innocent_labels(test_db, kind):
    line = requirement(estimate={"scope": "现场制作价",
        "costs": [{**cost(kind, 20), "label": "附加项目"}]})
    result = priced(test_db, line)
    assert result["total"] is None and "不纳入" in result["lines"][0]["blockers"][0]


def test_labels_cannot_disguise_excluded_fees_or_power(test_db):
    for label in ("运费", "电源"):
        line = requirement(estimate={"scope": "人工待核价",
            "costs": [{**cost("other", 10), "label": label}]})
        assert priced(test_db, line)["total"] is None


def test_letter_count_is_not_assembled_quantity_and_rounding_is_explicit(test_db):
    line = requirement(pricing_family="letter", character_count=4, billing_length_mm="220",
        estimate={"scope": "四字一组，数量两组", "costs": [cost("finished", 19, "=letter_cm")]})
    assert priced(test_db, line)["total"] == "3344.00"
    assert rules.letter_measure(line, "cm") == 88
    line.billing_length_mm = Decimal("225")
    assert rules.letter_measure(line, "cm") == 90
    line.length_rounding = "ceil_cm"
    assert rules.letter_measure(line, "cm") == 92
    line.character_count = None
    assert priced(test_db, line)["total"] is None


def test_letter_groups_and_power_are_calculated_per_letter(test_db):
    line = requirement(pricing_family="letter", lighting="yes", installation_scene="rooftop",
        power_mode="per_letter_ceiling", letter_groups=[
            {"label": "六字", "count": 6, "width_mm": 2600, "height_mm": 2600,
             "billing_length_mm": 2600}],
        estimate={"scope": "主体、电源与支架制作；不含现场安装", "costs": [
            cost("finished", 10, "=letter_cm"), cost("power", 200, "=power_units"),
            cost("bracket", 250, "=billable_area_m2")]})
    assert rules.power_units(line) == 18
    result = priced(test_db, line)
    assert result["total"] == "58680.00"
    assert result["lines"][0]["estimate_costs"][2]["measure"] == "40.56"
    line.power_adjustment = -2
    assert priced(test_db, line)["total"] is None
    line.power_note = "逐字笔画核算后人工减两台"
    assert rules.power_units(line) == 16
    line.letter_groups.append(LetterGroup(label="Logo", count=1, width_mm=1000,
                                         height_mm=1000, billing_length_mm=1000))
    assert rules.power_units(line) == 17


@pytest.mark.parametrize("rate,formula", [(100, "=power_units"), (200, "=1")])
def test_power_fixed_rate_and_formula_cannot_be_bypassed(test_db, rate, formula):
    line = requirement(lighting="yes", power_count=2, estimate={
        "scope": "电源费用待审核", "costs": [cost("power", rate, formula)]})
    assert priced(test_db, line)["total"] is None


def test_typed_manual_power_extra_uses_same_math(test_db):
    line = requirement(power_count=2, manual_unit_price=100, manual_price_note="人工制作，不含电源",
        extras=[{"label": "电源", "kind": "power", "amount": 10, "reason": "人工核对电源"}])
    assert priced(test_db, line)["total"] is None
    line.extras[0].amount = Decimal(800)
    assert priced(test_db, line)["total"] == "1000.00"


@pytest.mark.parametrize("kind,rate", [("adhesive", 50), ("cut_adhesive", 100), ("3m", 200)])
def test_outsource_minimum_is_explicit_and_not_universal(test_db, kind, rate):
    line = requirement(width_mm=100, height_mm=100, quantity=5, estimate={
        "scope": "背胶外加工，最低面积暂定待核", "costs": [
            cost("outsourcing", rate, "=billable_area_m2", outsourcing_type=kind)]})
    assert priced(test_db, line)["total"] is None
    line.estimate.costs[0].minimum_area_scope = "per_piece"
    assert Decimal(priced(test_db, line)["total"]) == Decimal(rate) * Decimal(".5")
    line.estimate.costs[0].minimum_area_scope = "order"
    line.estimate.costs[0].basis = "total"
    assert Decimal(priced(test_db, line)["total"]) == Decimal(rate) * Decimal(".1")
    line.estimate.costs[0] = type(line.estimate.costs[0]).model_validate(cost("material", rate, "=area_m2"))
    assert Decimal(priced(test_db, line)["total"]) == Decimal(rate) * Decimal(".05")


def test_required_process_power_and_bracket_are_not_silently_omitted(test_db):
    for fields in ({"difficulty": "A"}, {"lighting": "yes"}, {"installation_scene": "rooftop"}):
        line = requirement(**fields, estimate={"scope": "只报主体，待审核", "costs": [cost("finished", 100)]})
        assert priced(test_db, line)["total"] is None


def test_default_policy_antidowngrade_rounding_and_customer_privacy(client):
    response = client.post("/api/jobs", data={"title": "现场报价", "brief": "客户文字"})
    job = response.json()
    assert job["business_rules_version"] == rules.VERSION
    line = requirement(manual_unit_price="13.34", manual_price_note="核定制作费用").model_dump(mode="json")
    line["business_rules_version"] = "legacy"
    job = save_lines(client, job, [line])
    assert job["requirements"][0]["business_rules_version"] == rules.VERSION
    path = f'/api/jobs/{job["id"]}/quotes'
    assert client.post(path, json={"revision": job["revision"], "terms": "含税报价"}).status_code == 422
    quote = client.post(path, json={"revision": job["revision"], "terms": "有效期七天",
                                    "rounding": "ten"}).json()
    assert quote["payload"]["total"] == "30.00"
    assert quote["payload"]["unrounded_total"] == "26.68"
    assert quote["payload"]["rounding_adjustment"] == "3.32"
    review = client.get(f'/api/quotes/{quote["id"]}/export?audience=review')
    assert review.status_code == 200 and "原始需求" in review.content.decode("utf-8-sig")
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409
    assert client.post(f'/api/quotes/{quote["id"]}/approve', json={
        "note": "审核范围及金额", "confirmed_commercial_terms": True}).status_code == 200
    customer = client.get(f'/api/quotes/{quote["id"]}/export').content.decode("utf-8-sig")
    assert "工艺难度备注" in customer and "取整调整,3.32" in customer
    assert "原始需求" not in customer and "费用依据" not in customer


def test_upgrade_preserves_legacy_quotes_and_invalidates_them(client):
    job = save_lines(client, create_job(client), [requirement(
        manual_unit_price=100, manual_price_note="人工确认").model_dump(mode="json")])
    assert job["business_rules_version"] == "legacy"
    old = client.post(f'/api/jobs/{job["id"]}/quotes', json={
        "revision": job["revision"], "terms": "含税报价"}).json()
    job = client.put(f'/api/jobs/{job["id"]}/pricing-policy', json={
        "revision": job["revision"], "reason": "明确采用九月现场规则"}).json()
    assert job["business_rules_version"] == rules.VERSION
    assert not job["requirements"][0]["confirmed"]
    assert job["quotes"][0]["payload"] == old["payload"]
    assert job["quotes"][0]["outdated"]


def test_catalog_letter_measure_cannot_skip_character_count(client, price_input, test_db):
    price = client.post("/api/prices", json={
        **price_input, "status": "active", "unit": "cm", "amount": "19"}).json()
    line = requirement(pricing_family="letter", billing_length_mm=220, character_count=4,
        material=price["material"], thickness_mm=price["thickness_mm"],
        estimate={"scope": "字类成品引用，图纸单字22厘米四字一组", "costs": [
            cost("finished", 19, "=billing_length_mm/10", source="catalog",
                 reference_id=price["id"], reference_revision=price["revision"])]})
    assert priced(test_db, line)["total"] is None
    line.estimate.costs[0].quantity_formula = "=letter_cm"
    result = priced(test_db, line)
    assert result["total"] == "3344.00", result["lines"][0]["blockers"]


def test_explicit_area_is_available_but_letter_area_never_omits_count(test_db):
    line = requirement(area_m2="1.23", estimate={"scope": "图纸明确面积计价", "costs": [
        cost("finished", 100, "=area_m2")]})
    assert priced(test_db, line)["total"] == "246.00"
    line.pricing_family = "letter"
    line.estimate.costs[0].quantity_formula = "=billable_area_m2"
    assert priced(test_db, line)["total"] is None
    line.character_count = 4
    assert priced(test_db, line)["total"] == "984.00"


@pytest.mark.parametrize("formula,field,value,total", [
    ("=billable_area_m2", "width_mm", "1500", "750.00"),
    ("=letter_cm", "billing_length_mm", "2500", "50000.00"),
])
def test_single_letter_group_uses_reviewable_line_completion(test_db, formula, field, value, total):
    line = requirement(pricing_family="letter", character_count=1, height_mm=2500,
        letter_groups=[{"label": "A", "count": 1, "height_mm": 2500}],
        estimate={"scope": "单字制作，尺寸暂定待审核", "assumptions": [
            {"field": field, "value": value, "reason": "单字缺项暂定，非原文事实"}],
            "costs": [cost("finished", 100, formula)]})
    result = priced(test_db, line)
    assert result["total"] == total, result["lines"][0]["blockers"]
    calculated = result["lines"][0]
    assert calculated["requirement"]["letter_groups"][0][field] is None
    assert calculated["effective_requirement"]["letter_groups"][0][field] == value
    assert calculated["estimate_review"][0]["applied"]


def test_single_letter_completion_never_overwrites_group_facts(test_db):
    line = requirement(pricing_family="letter", character_count=1, height_mm=2500,
        letter_groups=[{"label": "A", "count": 1, "width_mm": 1000, "height_mm": 2500}],
        estimate={"scope": "原始字组尺寸优先", "assumptions": [
            {"field": "width_mm", "value": "1500", "reason": "暂定宽度供审核"}],
            "costs": [cost("finished", 100, "=billable_area_m2")]})
    assert priced(test_db, line)["total"] == "500.00"


@pytest.mark.parametrize("groups", [
    [{"label": "AB", "count": 2, "height_mm": 2500}],
    [{"label": "A", "count": 1, "height_mm": 2500},
     {"label": "B", "count": 1, "height_mm": 1000}],
])
def test_multiple_letters_never_use_whole_line_dimensions_as_group_dimensions(test_db, groups):
    line = requirement(pricing_family="letter", letter_groups=groups,
        width_mm=5000, height_mm=2500, billing_length_mm=5000,
        estimate={"scope": "整行宽高不得当作各字尺寸",
                  "costs": [cost("finished", 100, "=billable_area_m2")]})
    assert priced(test_db, line)["total"] is None
