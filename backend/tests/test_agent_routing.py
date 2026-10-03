import json
import threading
from copy import deepcopy

import pytest

from app import agent_loop, agent_work_state, agent_worker
from app import deepseek
from app.config import settings
from app.models import AgentRun, RequirementJob
from test_agent_loop import call, prepared, provider, seed, start
from test_estimate_pricing import plan
from test_redis_agent import enqueue, redis_worker
from test_requirements import save_lines


@pytest.fixture(autouse=True)
def fake_model_key(monkeypatch):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only-key")


def run_workers(count=5):
    for _ in range(count):
        agent_worker.run_pending_once(threading.Event())


def test_three_routes_preserve_known_amounts_and_ask_without_blocking_order(
        client, price_input, monkeypatch, redis_worker, test_db):
    price = seed(client, price_input)
    job = enqueue(client, monkeypatch, 3)
    with test_db.begin() as db:
        current = db.get(RequirementJob, job["id"])
        current.requirements = [{**raw, "quantity": 2 if raw["id"] == "line0" else None}
                                for raw in current.requirements]
    seen = provider(monkeypatch, [
        call("search_prices"),
        call("evaluate_price", line_id="line0", price_id=price["id"], revision=price["revision"]),
        call("select_price", line_id="line0", price_id=price["id"], revision=price["revision"],
             reason="Material, dimensions and process checked"),
        call("defer_requirement", line_id="line1", cause="customer_input",
             reason="Quantity has no reasonable estimating basis",
             question="How many pieces are required for this item?", missing_fields=["quantity"]),
        call("complete_estimate", line_id="line2", estimate=plan()),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "waiting"
    assert current["status"] == "needs_review"
    quote = current["quotes"][0]
    assert quote["status"] == "blocked" and not quote["outdated"]
    assert quote["payload"]["total"] is None and not quote["payload"]["complete"]
    assert quote["payload"]["known_subtotal"] == "163.20"
    assert [row["amount"] for row in quote["payload"]["lines"]] == ["21.60", None, "141.60"]
    assert quote["payload"]["lines"][1]["deferred_issue"]["cause"] == "customer_input"
    assert quote["payload"]["review_questions"]
    assert quote["payload"]["confirmation_items"][0]["owner"] == "需求方"
    assert json.loads(seen[-1][1]["content"])["active_requirement"]["id"] == "line2"
    assert current["requirements"][1]["quantity"] is None
    assert current["requirements"][2]["quantity"] is None
    assert "work_state" not in run["usage"]
    assert run["usage"]["deferred_requirements"][0]["missing_fields"] == ["quantity"]
    assert client.get(f'/api/quotes/{quote["id"]}/export').status_code == 409
    assert client.post(f'/api/quotes/{quote["id"]}/approve', json={
        "note": "Incomplete quote must not be approved", "confirmed_commercial_terms": True,
        "terms": "Test terms"}).status_code == 409


def test_legacy_ask_user_defers_current_item_and_continues(
        client, monkeypatch, redis_worker):
    job = enqueue(client, monkeypatch, 2)
    seen = provider(monkeypatch, [
        call("ask_user", question="Please specify the exact production quantity"),
        call("complete_estimate", line_id="line1", estimate=plan()),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert len(seen) == 2
    assert current["quotes"][0]["payload"]["known_subtotal"] == "141.60"
    assert current["agent_runs"][0]["usage"]["deferred_requirements"][0]["line_id"] == "line0"
    assert any("production quantity" in message["text"] for message in current["messages"])


def test_identical_calls_are_not_reexecuted_and_dont_erase_repair_context(
        client, monkeypatch, redis_worker):
    job = enqueue(client, monkeypatch, 2)
    seen = provider(monkeypatch, [
        *[call("read_requirement", line_id="line0") for _ in range(4)],
        call("defer_requirement", line_id="line0", cause="catalog_gap",
             reason="No explainable price basis", question="Please provide a comparable supplier quote"),
        call("complete_estimate", line_id="line1", estimate=plan()),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    reads = [step for step in current["agent_runs"][0]["steps"] if step["tool"] == "read_requirement"]
    assert "requirement" in reads[0]["result"] and "requirement" in reads[1]["result"]
    assert reads[2]["result"]["repeated_call"] and reads[3]["result"]["repeated_call"]
    assert any(message["role"] == "tool" for message in seen[4][2:])
    assert current["quotes"][0]["payload"]["lines"][0]["deferred_issue"]["cause"] == "catalog_gap"


def test_stalled_item_is_classified_as_execution_problem_not_customer_omission(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(settings(), "agent_stall_rounds", 6)
    job = enqueue(client, monkeypatch, 2)
    seen = provider(monkeypatch, [
        *[call("read_requirement", line_id="line0") for _ in range(6)],
        call("complete_estimate", line_id="line1", estimate=plan()),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    issue = current["agent_runs"][0]["usage"]["deferred_requirements"][0]
    assert issue["cause"] == "execution_error" and not issue["question"]
    assert len(seen) == 7
    assert current["quotes"][0]["payload"]["known_subtotal"] == "141.60"
    assert current["quotes"][0]["payload"]["total"] is None
    assert current["quotes"][0]["payload"]["confirmation_items"][0]["owner"] == "报价管理员"


def test_manual_resume_restores_failed_plan_and_candidate_without_resetting_facts(
        client, price_input, monkeypatch, redis_worker, test_db):
    price = seed(client, price_input)
    job = enqueue(client, monkeypatch, 1)
    bad = plan()
    bad["costs"][0]["quantity_formula"] = "=letter_cm"
    provider(monkeypatch, [
        call("search_prices"),
        call("evaluate_price", line_id="line0", price_id=price["id"], revision=price["revision"]),
        call("complete_estimate", line_id="line0", estimate=bad),
        call("defer_requirement", line_id="line0", cause="execution_error",
             reason="Unable to repair the formula yet"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    seen = provider(monkeypatch, [
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    resumed = start(client, current, intent="resume").json()
    run_workers()
    final = client.get(f'/api/jobs/{job["id"]}').json()
    context = json.loads(seen[0][1]["content"])
    assert context["work_state"]["failed_estimate"]["costs"][0]["quantity_formula"] == "=letter_cm"
    assert context["work_state"]["diagnostics"]["repair_fields"][0]["field"] == "billing_length_mm"
    assert context["work_state"]["candidates"][0]["amount"] == "300.000000"
    assert "select_price" in [tool["function"]["name"] for tool in agent_loop.TOOLS]
    assert final["agent_runs"][0]["id"] == resumed["agent_runs"][0]["id"]
    assert final["agent_runs"][0]["status"] == "succeeded"
    assert final["requirements"][0]["quantity"] is None
    assert final["quotes"][0]["payload"]["total"] == "141.60"


def test_defer_requires_specific_question_and_cannot_discard_priced_item(
        client, monkeypatch, redis_worker):
    job = enqueue(client, monkeypatch, 2)
    provider(monkeypatch, [
        call("defer_requirement", line_id="line0", cause="customer_input",
             reason="No quantity"),
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("defer_requirement", line_id="line0", cause="catalog_gap",
             reason="Do not discard a valid price"),
        call("complete_estimate", line_id="line1", estimate=plan()),
        call("finish_quote"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    steps = current["agent_runs"][0]["steps"]
    assert "error" in steps[0]["result"] and "error" in steps[2]["result"]
    assert current["quotes"][0]["payload"]["total"] == "283.20"
    assert not current["agent_runs"][0]["usage"]["deferred_requirements"]


def test_changed_input_invalidates_deferral_and_old_memory():
    from types import SimpleNamespace
    raw = {"id": "a", "product": "A", "quantity": None}
    job = SimpleNamespace(requirements=[raw])
    run = SimpleNamespace(usage={})
    agent_work_state.update_state(job, run, "a", deferred=True, question="How many?")
    assert agent_work_state.deferred_lines(job, run)
    job.requirements = [{**raw, "quantity": 2}]
    assert not agent_work_state.line_state(job, run, "a")
    assert not agent_work_state.deferred_lines(job, run)


def test_checkpoint_keeps_failed_plan_even_after_many_reads():
    from types import SimpleNamespace
    from app.requirements_schemas import RequirementLine
    raw = RequirementLine(id="a", product="A").model_dump(mode="json")
    job = SimpleNamespace(requirements=[raw], brief="", messages=[], extraction={},
                          business_rules_version="legacy")
    run = SimpleNamespace(usage={"durable": True}, steps=[])
    attempted = plan()
    agent_work_state.remember(job, run, "complete_estimate", {"line_id": "a", "estimate": attempted},
                              {"saved": False, "blockers": ["Missing measurement"]})
    assert agent_work_state.line_state(job, run, "a")["failed_estimate"] == attempted
    for _ in range(30):
        run.steps.append({"tool": "read_requirement", "arguments": {"line_id": "a"},
                          "result": {"requirement": deepcopy(raw)}})
    # Memory is not evicted by the most recent 20 audit events.
    assert agent_work_state.line_state(job, run, "a")["diagnostics"]["blockers"]


def test_resume_of_pre_fix_audit_restores_failed_plan_and_catalog_candidates(
        client, price_input, monkeypatch, redis_worker, test_db):
    price = seed(client, price_input)
    job = enqueue(client, monkeypatch, 1)
    bad = plan()
    bad["costs"][0]["quantity_formula"] = "=letter_cm"
    provider(monkeypatch, [
        call("search_prices"),
        call("complete_estimate", line_id="line0", estimate=bad),
        call("defer_requirement", line_id="line0", cause="execution_error",
             reason="Legacy run stopped with a failed plan"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    with test_db.begin() as db:
        old = db.get(AgentRun, current["agent_runs"][0]["id"])
        old.usage = {key: value for key, value in old.usage.items()
                     if key not in ("work_state", "work_state_restored")}
    seen = provider(monkeypatch, [
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("finish_quote"),
    ])
    start(client, current, intent="resume")
    run_workers()
    state = json.loads(seen[0][1]["content"])["work_state"]
    assert state["failed_estimate"]["costs"][0]["quantity_formula"] == "=letter_cm"
    assert any(candidate["id"] == price["id"] for candidate in state["candidates"])
    assert client.get(f'/api/jobs/{job["id"]}').json()["agent_runs"][0]["status"] == "succeeded"


def test_saved_estimate_cannot_be_overwritten_by_automatic_resume(
        client, monkeypatch, redis_worker):
    job = enqueue(client, monkeypatch, 2)
    changed = plan()
    changed["costs"][1]["rate"] = "9000"
    provider(monkeypatch, [
        call("complete_estimate", line_id="line0", estimate=plan()),
        call("complete_estimate", line_id="line0", estimate=changed),
        call("complete_estimate", line_id="line1", estimate=plan()),
        call("finish_quote"),
    ])
    run_workers()
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["steps"][1]["result"]["already_priced"]
    assert current["quotes"][0]["payload"]["total"] == "283.20"


def test_foreground_explicit_deferral_also_continues_remaining_items(client, monkeypatch):
    job = prepared(client)
    job = save_lines(client, job, [
        {**job["requirements"][0], "id": "line0", "quantity": None},
        {**job["requirements"][0], "id": "line1", "quantity": None}])
    provider(monkeypatch, [
        call("defer_requirement", line_id="line0", cause="customer_input",
             reason="No reasonable quantity basis", question="Please confirm the quantity"),
        call("complete_estimate", line_id="line1", estimate=plan()),
    ])
    current = start(client, job, intent="resume").json()
    assert current["agent_runs"][0]["status"] == "waiting"
    assert current["quotes"][0]["payload"]["known_subtotal"] == "141.60"
