from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import agent_work_state, business_rules, deepseek
from app.agent_loop import DeferInput, defer_requirement
from app.models import PriceItem
from app.pricing_strategies import STRATEGIES
from app.quote_engine import build_quote
from app.requirements_schemas import ExtractedLine, RequirementLine


def fee(**changes):
    return {"label": "制作", "rate": "10", "rate_unit": "piece", "expected_amount": "10",
            "quantity_formula": "=1", "source": "agent_estimate", "kind": "finished",
            "reason": "明确暂估依据，需审核单价", **changes}


def line(costs=None, assumptions=None, **changes):
    return RequirementLine.model_validate({
        "id": "generic", "product": "通用测试项目", "quantity": 2,
        "business_rules_version": business_rules.VERSION, "pricing_family": "material",
        "estimate": {"contract_version": 2, "pricing_strategy": "auto", "scope": "制作范围待审核",
                     "assumptions": assumptions or [], "costs": costs if costs is not None else [fee()]},
        **changes})


def quote(test_db, requirement):
    with test_db() as db:
        return build_quote(db, [requirement.model_dump(mode="json")], automatic=True)


@pytest.mark.parametrize("unit,formula,fields,rate,amount", [
    ("piece", "=1", {}, "15", "15"),
    ("set", "=1", {}, "25", "25"),
    ("fixed", "=1", {}, "50", "50"),
    ("m", "=billing_length_mm/1000", {"billing_length_mm": 3500}, "30", "105"),
    ("cm", "=billing_length_mm/10", {"billing_length_mm": 150}, "4", "60"),
    ("m2", "=width_mm*height_mm/1000000", {"width_mm": 500, "height_mm": 300}, "100", "15"),
    ("m2", "=area_m2", {"area_m2": ".125"}, "100", "12.50"),
    ("hour", "=labor_hours", {"labor_hours": "1.5"}, "80", "120"),
])
def test_strategy_matrix_keeps_units_and_order_quantity(test_db, unit, formula, fields, rate, amount):
    requirement = line([fee(rate_unit=unit, quantity_formula=formula, rate=rate, expected_amount=amount)], **fields)
    result = quote(test_db, requirement)
    assert result["total"] == str((Decimal(amount) * 2).quantize(Decimal(".01")))
    assert result["lines"][0]["resolution"] == "priced_review"
    assert result["measurement_contract"] == "measurement.v2"


def test_different_letter_groups_can_be_completed_and_priced_independently(test_db):
    groups = [{"label": "digits", "count": 6, "height_mm": 80},
              {"label": "connector", "count": 1, "height_mm": 15}]
    assumptions = [{"target": "letter_group", "target_index": index, "field": "billing_length_mm",
                    "value": length, "reason": "按该字组标注尺寸暂定计价长度"} for index, length in enumerate(["80", "15"])]
    costs = [fee(target="letter_group", target_index=0, rate_unit="cm", quantity_formula="=letter_cm",
                 expected_amount="480"),
             fee(label="连接符", target="letter_group", target_index=1, rate_unit="cm",
                 quantity_formula="=letter_cm", expected_amount="15")]
    requirement = line(costs, assumptions, pricing_family="letter", letter_groups=groups)
    result = quote(test_db, requirement)
    assert result["total"] == "990.00", result["lines"][0]["blockers"]
    assert [cost["measure"] for cost in result["lines"][0]["estimate_costs"]] == ["48", "1.5"]
    assert all(group.billing_length_mm is None for group in requirement.letter_groups)
    assert result["lines"][0]["estimate_review"][0]["path"] == "letter_groups[0].billing_length_mm"


def test_whole_item_piece_fee_does_not_require_unused_letter_dimensions(test_db):
    requirement = line(pricing_family="letter", billing_length_mm=80,
                       letter_groups=[{"label": "unknown width", "count": 3}])
    assert quote(test_db, requirement)["total"] == "20.00"


@pytest.mark.parametrize("formula,unit,total", [
    ("=billable_area_m2", "m2", "12.00"),
    ("=area_m2*component_count", "m2", "12.00"),
    ("=width_mm*height_mm/1000000*component_count", "m2", "12.00"),
    ("=component_count", "piece", "60.00"),
    ("=billing_length_mm/1000*component_count", "m", "120.00"),
    ("=labor_hours*component_count", "hour", "90.00"),
])
def test_component_measurement_includes_its_usage_once(test_db, formula, unit, total):
    component = {"name": "panel", "width_mm": 500, "height_mm": 400,
                 "quantity_per_item": 3, "billing_length_mm": 2000, "labor_hours": "1.5"}
    requirement = line([fee(target="component", target_index=0, rate_unit=unit,
                            quantity_formula=formula, expected_amount=str(Decimal(total) / 2))],
                       width_mm=2000, height_mm=1000, components=[component])
    assert quote(test_db, requirement)["total"] == total


def test_composite_product_mixes_area_length_labor_and_fixed_fees(test_db):
    costs = [
        fee(label="面板", target="component", target_index=0, kind="material", rate_unit="m2",
            quantity_formula="=billable_area_m2", expected_amount="4"),
        fee(label="框架", target="component", target_index=1, kind="material", rate_unit="m",
            quantity_formula="=billing_length_mm/1000*component_count", expected_amount="20"),
        fee(label="组装", kind="process", rate_unit="hour", quantity_formula="=labor_hours", expected_amount="15"),
        fee(label="制版", basis="total", rate_unit="fixed", expected_amount="10"),
    ]
    requirement = line(costs, components=[
        {"name": "panel", "width_mm": 500, "height_mm": 400, "quantity_per_item": 2},
        {"name": "frame", "billing_length_mm": 1000, "quantity_per_item": 2},
    ], labor_hours="1.5")
    result = quote(test_db, requirement)
    assert result["total"] == "88.00"
    assert result["lines"][0]["pricing_strategy"]["key"] == "composite"
    requirement.quantity = 3
    assert quote(test_db, requirement)["total"] == "127.00"


def test_component_missing_usage_is_not_silently_one(test_db):
    requirement = line([fee(target="component", target_index=0, rate_unit="m2",
                            quantity_formula="=billable_area_m2")],
                       components=[{"name": "unknown usage", "width_mm": 500, "height_mm": 500}])
    assert quote(test_db, requirement)["total"] is None


@pytest.mark.parametrize("changes", [
    {"target": "component"}, {"target": "item", "target_index": 0},
    {"rate_basis_amount": 10}, {"included_kinds": ["material", "material"]},
])
def test_invalid_object_contract_rejected(changes):
    with pytest.raises(ValidationError):
        line([fee(**changes)])


def test_target_cannot_address_missing_component(test_db):
    result = quote(test_db, line([fee(target="component", target_index=0)]))
    assert result["total"] is None and "不存在" in result["lines"][0]["blockers"][0]


def test_catalog_v2_accepts_equivalent_area_formula_and_preserves_unit(client, price_input, test_db):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": "m2", "amount": "100"}).json()
    requirement = line([fee(source="catalog", rate=100, rate_unit="m2", quantity_formula="=area_m2",
                            expected_amount=None, reference_id=price["id"], reference_revision=price["revision"])],
                       width_mm=500, height_mm=400, material=price["material"])
    assert quote(test_db, requirement)["total"] == "40.00"
    missing_material = quote(test_db, requirement.model_copy(update={"material": ""}))
    assert missing_material["lines"][0]["diagnostics"][0]["code"] == "missing_target_material"


def test_declared_finished_coverage_prevents_duplicate_processing(test_db):
    costs = [fee(included_kinds=["material", "process"]), fee(label="加工", kind="process")]
    result = quote(test_db, line(costs))
    assert result["total"] is None
    assert result["lines"][0]["diagnostics"][0]["code"] == "duplicate_coverage"
    costs[1].update(adjusts_cost_index=0, reason="更改实际工艺增加的加工差价，待审核")
    assert quote(test_db, line(costs))["total"] == "40.00"


def test_same_fee_on_different_components_is_not_a_duplicate(test_db):
    costs = [fee(target="component", target_index=index, quantity_formula="=component_count")
             for index in range(2)]
    requirement = line(costs, components=[{"name": "one", "quantity_per_item": 1},
                                         {"name": "two", "quantity_per_item": 1}])
    assert quote(test_db, requirement)["total"] == "40.00"


def test_reference_coverage_prevents_duplicates(client, price_input, test_db):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": "m2", "amount": "100",
                        "included_cost_kinds": ["process"]}).json()
    costs = [fee(source="catalog", rate=100, rate_unit="m2", quantity_formula="=area_m2",
                 expected_amount=None, reference_id=price["id"], reference_revision=price["revision"]),
             fee(label="再次加工", kind="process")]
    result = quote(test_db, line(costs, width_mm=500, height_mm=400, material=price["material"]))
    assert result["total"] is None and "已由费用" in result["lines"][0]["blockers"][0]


def test_structured_piece_basis_cannot_be_reinterpreted_as_centimeter_rate(test_db):
    requirement = line([fee(rate=10, rate_unit="cm", quantity_formula="=billing_length_mm/10",
                            expected_amount=100, rate_basis_amount=10, rate_basis_unit="piece")],
                       billing_length_mm=100)
    assert quote(test_db, requirement)["total"] is None


def correction(field="height_mm", value="1000", previous="100"):
    return {"field": field, "value": value, "previous_value": previous, "mode": "correct",
            "reason": "局部标注错误识别为整体高度，按原文纠正，待审核",
            "evidence": [{"file_id": 1, "page": 1, "text": "成品500×1000mm", "source": "text"}]}


def test_source_backed_correction_keeps_original_facts_and_records_review(test_db):
    requirement = line([fee(rate_unit="m2", quantity_formula="=area_m2", expected_amount="5")],
                       [correction()], width_mm=500, height_mm=100,
                       evidence=[{"file_id": 1, "page": 1, "text": "成品500×1000mm"}])
    result = quote(test_db, requirement)
    assert result["total"] == "10.00"
    detail = result["lines"][0]
    assert detail["requirement"]["height_mm"] == "100"
    assert detail["effective_requirement"]["height_mm"] == "1000"
    assert detail["estimate_review"][0]["kind"] == "correction"


@pytest.mark.parametrize("changes", [
    {"confirmed": True}, {"height_mm": 200},
    {"estimate": {"contract_version": 2, "scope": "审核", "assumptions": [correction(value="900")], "costs": [fee()]}},
])
def test_correction_cannot_overwrite_confirmed_changed_or_unsupported_values(test_db, changes):
    requirement = line(**{"assumptions": [correction()], "height_mm": 100,
                       "evidence": [{"file_id": 1, "page": 1, "text": "成品500×1000mm"}], **changes})
    assert quote(test_db, requirement)["total"] is None


def test_classification_correction_makes_letter_strategy_usable(test_db):
    requirement = line([fee(rate_unit="cm", quantity_formula="=letter_cm", expected_amount=80)],
                       [correction("pricing_family", "letter", "material"),
                        {"field": "billing_length_mm", "value": "80", "reason": "单字计价长度暂定待核"}],
                       character_count=1, evidence=[{"file_id": 1, "page": 1, "text": "成品500×1000mm"}])
    assert quote(test_db, requirement)["total"] == "160.00"
    assert requirement.pricing_family == "material"


def test_group_character_count_is_characters_not_words():
    with pytest.raises(ValidationError):
        ExtractedLine(letter_groups=[{"label": "English", "text_content": "utility room",
                                     "count_basis": "characters", "count": 2}])
    assert ExtractedLine(letter_groups=[{"text_content": "utility room", "count_basis": "characters",
                                         "count": 11}]).letter_groups[0].count == 11


def test_local_dimension_does_not_override_overall_table_dimension():
    requirement = ExtractedLine(width_mm=500, height_mm=1000,
        evidence=[{"file_id": 1, "page": 1, "text": "drawing"}], dimension_evidence=[
            {"file_id": 1, "page": 1, "text": "500×1000", "field": "height_mm", "value": 1000, "source": "text"},
            {"file_id": 1, "page": 1, "text": "100", "field": "height_mm", "value": 100, "role": "local"},
        ])
    deepseek.validate_dimension_evidence(requirement, {(1, 1): "500×1000"})
    assert requirement.height_mm == 1000


def test_group_and_component_dimensions_are_validated_separately():
    requirement = ExtractedLine(height_mm=1000, evidence=[{"file_id": 1, "page": 1, "text": "drawing"}],
        letter_groups=[{"label": "one", "count": 1}], components=[{"name": "panel"}],
        dimension_evidence=[
            {"file_id": 1, "page": 1, "text": "80", "field": "height_mm", "value": 80,
             "target": "letter_group", "target_index": 0, "role": "character"},
            {"file_id": 1, "page": 1, "text": "400", "field": "height_mm", "value": 400,
             "target": "component", "target_index": 0},
        ])
    deepseek.validate_dimension_evidence(requirement, {})
    assert requirement.height_mm == 1000
    assert requirement.letter_groups[0].height_mm == 80
    assert requirement.components[0].height_mm == 400


def test_missing_group_diagnostic_points_to_groups_not_whole_line(test_db):
    requirement = line([fee(rate_unit="cm", quantity_formula="=letter_cm")], pricing_family="letter",
                       letter_groups=[{"count": 2, "label": "two"}, {"count": 1, "label": "one"}])
    result = quote(test_db, requirement)
    assert result["lines"][0]["diagnostics"][0]["paths"] == [
        "letter_groups[0].billing_length_mm", "letter_groups[1].billing_length_mm"]


def test_customer_deferral_cannot_claim_known_quantity_is_missing():
    requirement = line().model_dump(mode="json")
    job = SimpleNamespace(requirements=[requirement], messages=[])
    run = SimpleNamespace(usage={})
    result = defer_requirement(job, run, DeferInput(line_id="generic", cause="customer_input",
        reason="缺数量", question="多少件？", missing_fields=["quantity"]))
    assert result["known_fields"] == ["quantity"]
    assert not agent_work_state.deferred_lines(job, run)


@pytest.mark.parametrize("reason", [
    "各型数量仍在核对点位与套数组成，暂未核清",
    "系统尚未核清原件中的分型数量",
    "尚未核对原图中的每套构成",
    "分型数量继续由系统查阅全册点位",
])
def test_unfinished_source_review_cannot_be_deferred_to_customer(reason):
    requirement = line().model_dump(mode="json")
    job = SimpleNamespace(requirements=[requirement], messages=[])
    run = SimpleNamespace(usage={})
    before = deepcopy(requirement)
    result = defer_requirement(job, run, DeferInput(line_id="generic", cause="customer_input",
        reason=reason, question="各型各多少？", missing_fields=["分型数量"]))
    assert result["cause"] == "recognition_review"
    assert result["owner"] == "系统核查"
    assert not agent_work_state.deferred_lines(job, run)
    assert job.requirements == [before] and not job.messages


def test_verified_missing_customer_fact_can_still_be_deferred():
    requirement = line(quantity=None).model_dump(mode="json")
    job = SimpleNamespace(requirements=[requirement], messages=[])
    run = SimpleNamespace(usage={})
    result = defer_requirement(job, run, DeferInput(line_id="generic", cause="customer_input",
        reason="已核对清单及全部关联图纸，均未提供订购数量",
        question="请提供订购数量。", missing_fields=["quantity"]))
    assert result["deferred"] and result["cause"] == "customer_input"
    assert agent_work_state.deferred_lines(job, run)[0]["line_id"] == "generic"


def test_repair_progress_is_bounded_and_repeated_candidates_do_not_reset_idle():
    job = SimpleNamespace(requirements=[line().model_dump(mode="json")])
    run = SimpleNamespace(usage={"active_line_id": "generic"})
    for index in range(4):
        result = {"prices": [{"id": index + 1, "revision": 1}]}
        assert agent_work_state.record_repair_progress(job, run, "search_prices", {}, result) == (index < 3)
        assert not agent_work_state.record_repair_progress(job, run, "search_prices", {}, result)


def test_catalog_api_lists_strategies_without_business_policy_downgrade(client):
    result = client.get("/api/pricing-strategies").json()
    assert {strategy["key"] for strategy in result["strategies"]} == set(STRATEGIES)
    assert result["business_rules"]["version"] == business_rules.VERSION


def test_peer_rate_anomaly_is_review_signal_not_fabricated_market_limit(test_db):
    requirements = [line([fee(rate=rate, expected_amount=rate)], id=f"peer{index}",
                         pricing_category="same process", material="same", thickness_mm=3)
                    for index, rate in enumerate(["10", "100"])]
    with test_db() as db:
        result = build_quote(db, [r.model_dump(mode="json") for r in requirements], automatic=True)
    assert result["total"] == "220.00"
    assert all(any(signal["code"] == "peer_rate_variation" for signal in row["review_signals"])
               for row in result["lines"])


@pytest.mark.parametrize("reverse", [False, True])
def test_parent_coverage_is_order_independent(test_db, reverse):
    costs = [fee(included_kinds=["process"]),
             fee(label="部件加工", kind="process", target="component", target_index=0,
                 quantity_formula="=component_count")]
    if reverse:
        costs.reverse()
    requirement = line(costs, components=[{"name": "panel", "quantity_per_item": 1}])
    assert "重复" in quote(test_db, requirement)["lines"][0]["blockers"][0]


def test_manual_extra_cannot_duplicate_declared_coverage(test_db):
    requirement = line([fee(included_kinds=["process"])],
                       extras=[{"label": "加工补价", "kind": "process", "amount": 10, "reason": "人工加工价"}])
    assert quote(test_db, requirement)["total"] is None


@pytest.mark.parametrize("geometry", ["shaped", "shell"])
def test_nonflat_component_requires_explicit_area_or_basis(test_db, geometry):
    cost = fee(target="component", target_index=0, rate_unit="m2",
               quantity_formula="=billable_area_m2", expected_amount="2")
    requirement = line([cost], components=[{"name": "shape", "geometry": geometry,
                                          "width_mm": 500, "height_mm": 400, "quantity_per_item": 1}])
    assert quote(test_db, requirement)["total"] is None
    cost["measurement_note"] = "供应商按该部件包围矩形面积报价，待管理员审核"
    requirement.estimate.costs[0].measurement_note = cost["measurement_note"]
    assert quote(test_db, requirement)["total"] == "4.00"


def test_component_outsourcing_keeps_usage_and_minimum_area_scope(test_db):
    cost = fee(target="component", target_index=0, rate_unit="m2", rate=50, kind="outsourcing",
               outsourcing_type="adhesive", minimum_area_scope="per_piece",
               quantity_formula="=billable_area_m2", expected_amount="5")
    requirement = line([cost], components=[{"name": "sticker", "area_m2": ".02", "quantity_per_item": 4}])
    assert quote(test_db, requirement)["total"] == "10.00"
    requirement.components[0].quantity_per_item = 8
    requirement.estimate.costs[0].expected_amount = Decimal("8")
    assert quote(test_db, requirement)["total"] == "16.00"
    requirement.estimate.costs[0].basis = "total"
    requirement.estimate.costs[0].minimum_area_scope = "order"
    requirement.estimate.costs[0].expected_amount = Decimal("16")
    assert quote(test_db, requirement)["total"] == "16.00"


def test_invalid_numeric_correction_is_diagnostic_not_server_error(test_db):
    requirement = line(assumptions=[correction(value="not a number")], height_mm=100,
                       evidence=[{"file_id": 1, "page": 1, "text": "成品500×1000mm"}])
    assert quote(test_db, requirement)["total"] is None


def test_catalog_coverage_can_be_maintained_without_losing_source_attributes(client, price_input, test_db):
    response = client.post("/api/prices", json={**price_input, "included_cost_kinds": ["material", "process"]})
    assert response.status_code == 201, response.text
    price = response.json()
    with test_db.begin() as db:
        row = db.get(PriceItem, price["id"])
        row.attributes = {**row.attributes, "source_parameter": "preserved"}
    price = client.get(f'/api/prices/{price["id"]}').json()
    # Older clients omit coverage; a normal price change must not erase it.
    response = client.put(f'/api/prices/{price["id"]}', json={**price_input, "revision": price["revision"]})
    assert response.status_code == 200
    price = response.json()
    assert price["included_cost_kinds"] == ["material", "process"]
    assert price["attributes"]["source_parameter"] == "preserved"
    response = client.put(f'/api/prices/{price["id"]}', json={
        **price_input, "revision": price["revision"], "included_cost_kinds": []})
    assert response.json()["included_cost_kinds"] == []
    assert response.json()["attributes"]["source_parameter"] == "preserved"


@pytest.mark.parametrize("value,blocked", [(None, False), ("process", True), ([{}], True)])
def test_malformed_legacy_catalog_coverage_does_not_crash(client, price_input, test_db, value, blocked):
    price = client.post("/api/prices", json={**price_input, "status": "active", "unit": "piece", "amount": 10}).json()
    with test_db.begin() as db:
        row = db.get(PriceItem, price["id"])
        row.attributes = {"included_cost_kinds": value}
    price = client.get(f'/api/prices/{price["id"]}').json()
    requirement = line([fee(source="catalog", reference_id=price["id"], reference_revision=price["revision"])],
                       material=price["material"])
    assert (quote(test_db, requirement)["total"] is None) == blocked


def test_generic_agent_repairs_scoped_groups_then_finishes_reviewable_quote(client, monkeypatch):
    from test_agent_loop import prepared, provider, call, start
    from test_requirements import save_lines
    job = prepared(client)
    requirement = line(pricing_family="letter", letter_groups=[
        {"label": "main", "count": 2, "height_mm": 80},
        {"label": "small", "count": 1, "height_mm": 15}]).model_dump(mode="json")
    requirement["evidence"] = job["requirements"][0]["evidence"]
    requirement["estimate"] = None
    job = save_lines(client, job, [requirement])
    plan = {"contract_version": 2, "scope": "制作费待审核，不含现场排除费用",
            "assumptions": [], "costs": [fee(rate_unit="cm", quantity_formula="=letter_cm", expected_amount=175)]}
    repaired = deepcopy(plan)
    repaired["assumptions"] = [{"field": "billing_length_mm", "value": str(value),
        "target": "letter_group", "target_index": index, "reason": "按字组字高暂估计价长度，待核"}
        for index, value in enumerate([80, 15])]
    provider(monkeypatch, [call("complete_estimate", line_id="generic", estimate=plan),
                          call("complete_estimate", line_id="generic", estimate=repaired), call("finish_quote")])
    result = start(client, job).json()
    steps = result["agent_runs"][0]["steps"]
    assert steps[0]["result"]["saved"] is False
    assert steps[0]["result"]["repair_contract"]["fields"][0]["path"] == "letter_groups[0].billing_length_mm"
    assert steps[1]["result"]["saved"] is True
    payload = result["quotes"][0]["payload"]
    assert payload["total"] == "350.00"
    assert payload["resolution_counts"]["priced_review"] == 1
    assert result["requirements"][0]["letter_groups"][0]["billing_length_mm"] is None
    assert client.get(f'/api/quotes/{result["quotes"][0]["id"]}/export').status_code == 409
    assert client.get(f'/api/quotes/{result["quotes"][0]["id"]}/export?audience=review').status_code == 200


def test_excel_corrections_cannot_invent_text_or_borrow_other_item_rows(client):
    from test_customer_excel import workbook_bytes
    from test_requirements import create_job, save_lines
    job = create_job(client, "source.xlsx", workbook_bytes(drawing=False))
    file_id = job["files"][0]["id"]
    original = {"file_id": file_id, "page": 1, "sheet": "客户明细", "cell": "E2", "text": "2"}
    source = {"file_id": file_id, "page": 1, "sheet": "客户明细", "cell": "E2", "text": "2"}
    adopted = correction("quantity", "2", "1")
    adopted["evidence"] = [source]
    requirement = line(assumptions=[adopted], quantity=1, evidence=[original]).model_dump(mode="json")
    job = save_lines(client, job, [requirement])
    for invalid in [{**source, "text": "9"}, {**source, "cell": "E3"},
                    {**source, "source": "image", "text": "9"}]:
        changed = deepcopy(job["requirements"])
        changed[0]["estimate"]["assumptions"][0]["evidence"] = [invalid]
        response = client.put(f'/api/jobs/{job["id"]}/requirements', json={
            "revision": job["revision"], "reason": "模拟错误纠正来源", "lines": changed})
        assert response.status_code == 422, response.text
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["revision"] == job["revision"]
    assert current["requirements"] == job["requirements"]


def test_generic_catalog_update_invalidates_old_snapshot(client, price_input):
    from test_agent_loop import prepared
    from test_requirements import save_lines
    price = client.post("/api/prices", json={
        **price_input, "status": "active", "unit": "piece", "amount": "10"}).json()
    job = prepared(client)
    requirement = line([fee(source="catalog", reference_id=price["id"],
                            reference_revision=price["revision"])],
                       material=price["material"]).model_dump(mode="json")
    requirement["evidence"] = job["requirements"][0]["evidence"]
    job = save_lines(client, job, [requirement])
    response = client.post(f'/api/jobs/{job["id"]}/quotes', json={
        "revision": job["revision"], "terms": "按当前制作口径报价，待审核"})
    assert response.status_code == 201, response.text
    assert response.json()["payload"]["total"] == "20.00"
    updated = client.put(f'/api/prices/{price["id"]}', json={
        **price_input, "status": "active", "unit": "piece", "amount": "10",
        "revision": price["revision"], "included_cost_kinds": ["process"]})
    assert updated.status_code == 200
    assert client.get(f'/api/jobs/{job["id"]}').json()["quotes"][0]["outdated"]


def test_invalid_formula_is_not_automatically_a_customer_question(test_db):
    payload = quote(test_db, line([fee(rate_unit="cm", quantity_formula="=1")]))
    assert payload["total"] is None
    item = payload["confirmation_items"][0]
    assert item["owner"] == "系统核价"
    assert item["cause"] == "plan_repair"


def test_equivalent_formulas_do_not_bypass_duplicate_fee_check(test_db):
    costs = [fee(rate_unit="m2", quantity_formula="=area_m2", expected_amount=2),
             fee(rate_unit="m2", quantity_formula="=width_mm*height_mm/1000000", expected_amount=2)]
    assert quote(test_db, line(costs, width_mm=500, height_mm=400))["total"] is None


@pytest.mark.parametrize("changes", [{"status": "draft"}, {"price_kind": "starting"}, {"price_kind": "reference"}])
def test_v2_catalog_source_cannot_masquerade_unapproved_or_starting_price(client, price_input, test_db, changes):
    price = client.post("/api/prices", json={
        **price_input, "status": "active", "unit": "piece", "amount": 10, **changes}).json()
    requirement = line([fee(source="catalog", reference_id=price["id"], reference_revision=price["revision"])])
    assert quote(test_db, requirement)["total"] is None
