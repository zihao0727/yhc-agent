from copy import deepcopy

from app.pricing_inventory import prepare_pricing_inventory
from test_pricing_proposals import order  # noqa: F401


def test_schedule_duplicates_and_drawings_remain_auditable_without_duplicate_billing(order):
    db, job, _ = order
    schedule = {**job.requirements[0], "document_role": "requirements"}
    duplicate = {**deepcopy(schedule), "id": "duplicate"}
    drawing = {**deepcopy(schedule), "id": "drawing", "document_role": "drawing",
               "quantity": None, "evidence": []}
    placeholder = {**deepcopy(schedule), "id": "placeholder", "product": "阅读占位",
                   "quantity": None, "material": "", "source_item": "", "source_code": "",
                   "width_mm": None, "height_mm": None, "quantity_evidence": [],
                   "dimension_evidence": [], "source_measurements": [],
                   "evidence": [{**schedule["evidence"][0], "cell": ""}]}
    job.requirements = [schedule, duplicate, drawing, placeholder]
    job.extraction = {"source_agent": {"mode": "ai"}}
    result = prepare_pricing_inventory(db, job)
    assert [raw["id"] for raw in job.requirements] == ["one"]
    assert result["schedule_line_count"] == 1
    assert result["pending_observation_count"] == 1
    assert job.extraction["source_agent"]["reference_observations"]["drawing"] == drawing
    assert {entry["requirement"]["id"] for entry in result["excluded_observations"]} == {
        "duplicate", "placeholder"}
    before = deepcopy(job.extraction)
    assert prepare_pricing_inventory(db, job) is None
    assert job.extraction == before


def test_confirmed_orders_and_conflicting_protected_duplicates_are_preserved(order):
    db, job, _ = order
    schedule = {**job.requirements[0], "document_role": "requirements", "confirmed": True}
    duplicate = {**deepcopy(schedule), "id": "confirmed-duplicate"}
    drawing = {**deepcopy(schedule), "id": "promoted", "document_role": "drawing",
               "evidence": [], "quantity_evidence": [], "dimension_evidence": []}
    job.requirements = [schedule, duplicate, drawing]
    job.extraction = {"source_agent": {"mode": "ai"}}
    assert prepare_pricing_inventory(db, job) is None
    assert job.requirements == [schedule, duplicate, drawing]


def test_unrecognized_schedule_rows_are_preserved(order):
    db, job, _ = order
    schedule = {**job.requirements[0], "document_role": "requirements"}
    unknown = {**deepcopy(schedule), "id": "extra", "evidence": [],
               "quantity_evidence": [], "dimension_evidence": [], "product": "Extra product"}
    job.requirements = [schedule, unknown]
    job.extraction = {"source_agent": {"mode": "ai"}}
    assert prepare_pricing_inventory(db, job) is None
    assert len(job.requirements) == 2
