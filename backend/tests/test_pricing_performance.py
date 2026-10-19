import json
from copy import deepcopy
from types import SimpleNamespace

from app import agent_context, agent_execution, agent_work_state as work, parallel_pricing
from app.agent_context_budget import compact_history, context_chars, without_empty_fields
from app.requirements_schemas import RequirementLine
from test_agent_loop import call
from test_pricing_proposals import order, proposal, submit  # noqa: F401
from test_parallel_pricing import proposal as parallel_proposal
from test_redis_agent import enqueue, redis_worker  # noqa: F401


def base_messages():
    return [{"role": "system", "content": "Safety and pricing contract"},
            {"role": "user", "content": "Current facts, costs and saved plan"}]


def exchange(name, result, call_id):
    message = call(name)
    message["tool_calls"][0]["id"] = call_id
    return [message, {"role": "tool", "tool_call_id": call_id,
                      "content": json.dumps(result)}]


def test_compaction_preserves_latest_diagnostic_with_oversized_base_and_followup():
    base = base_messages()
    base[1]["content"] *= 3000
    latest = exchange("repair_pricing_plan", {"error": "repair /costs/1/unit"}, "latest")
    followup = {"role": "user", "content": "Correct the failed field"}
    messages = [*base, *exchange("read_source", {"text": "Old source"}, "old"),
                *latest, followup]
    original = deepcopy(messages)
    assert compact_history(messages) == [*base, *latest, followup]
    assert messages == original


def test_compaction_never_splits_multi_tool_response():
    base = base_messages()
    latest = exchange("read_rules", {"rules": ["Units must match"]}, "first")
    second = call("read_source")["tool_calls"][0]
    second["id"] = "second"
    latest[0]["tool_calls"].append(second)
    latest.append({"role": "tool", "tool_call_id": "second", "content": "Source facts"})
    compacted = compact_history([*base, *exchange("read_source", {"text": "Old"}, "old"), *latest],
                                max_chars=1)
    assert compacted == [*base, *latest]
    assert {call["id"] for call in compacted[2]["tool_calls"]} == {
        message["tool_call_id"] for message in compacted[3:]}


def test_image_transport_bytes_do_not_force_history_compaction():
    messages = [*base_messages(), {"role": "user", "content": [
        {"type": "text", "text": "Actual drawing for this item"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 300000}},
    ]}, *exchange("read_rules", {"rules": ["Preserve these dimensions"]}, "rules")]
    original = deepcopy(messages)
    assert context_chars(messages) < 1000
    assert compact_history(messages) == original
    assert messages == original


def test_empty_field_compaction_keeps_unknowns_zero_false_and_array_positions():
    data = {"empty": "", "missing": None, "items": [{}, {"count": 0}],
            "count": 0, "confirmed": False, "material": "unknown",
            "diagnostics": [None, ""]}
    assert without_empty_fields(data) == {
        "items": [{}, {"count": 0}], "count": 0, "confirmed": False,
        "material": "unknown", "diagnostics": [None, ""]}


def test_dispatch_calculates_each_item_once_and_shares_only_current_batch(
        order, monkeypatch):
    db, job, run = order
    job.requirements = [{**job.requirements[0], "id": f"line{i}"} for i in range(5)]
    counts, snapshots, pages = [], [], []
    original = parallel_pricing.calculate_line

    def calculate(db, line, **kwargs):
        counts.append(line.id)
        return original(db, line, **kwargs)

    def prepare(self, db, job, run, raw, discovered, interrupted, *,
                calculated=None, source_pages=None):
        snapshots.append(calculated)
        pages.append(source_pages)
        assert len(calculated) == 5
        return parallel_pricing.Turn(raw["id"], "test", 0, [], [], {}, False)

    monkeypatch.setattr(parallel_pricing, "calculate_line", calculate)
    monkeypatch.setattr(parallel_pricing.PricingRequests, "prepare", prepare)
    monkeypatch.setattr(parallel_pricing, "model_turn", lambda turn: ({}, {}))
    requests = parallel_pricing.PricingRequests(4)
    try:
        requests.dispatch(db, job, run, set(), None)
        assert counts == [f"line{i}" for i in range(5)]
        assert all(snapshot is snapshots[0] for snapshot in snapshots)
        assert all(page is pages[0] for page in pages)
        requests.pending.clear()
        job.requirements = [{**raw, "width_mm": "1200"} for raw in job.requirements]
        requests.dispatch(db, job, run, set(), None)
        assert snapshots[0] is not snapshots[4] and pages[0] is not pages[4]
    finally:
        requests.pool.shutdown(wait=True)


def test_packet_reads_same_excel_page_once_without_cross_request_cache(order, monkeypatch):
    from app import pricing_proposals
    db, job, run = order
    calls = []
    original = pricing_proposals.page_source

    def read(path, page):
        calls.append((path, page))
        return original(path, page)

    monkeypatch.setattr(pricing_proposals, "page_source", read)
    pages = {}
    first = pricing_proposals.prepare_packet(db, job, job.requirements[0], source_pages=pages)
    second = pricing_proposals.prepare_packet(db, job, job.requirements[0], source_pages=pages)
    assert first == second and len(calls) == 1
    pricing_proposals.prepare_packet(db, job, job.requirements[0])
    assert len(calls) == 2


def test_context_retains_repair_plan_and_diagnostics_without_duplicate_engine_plan(order):
    db, job, run = order
    plan = proposal(order)
    plan["costs"][0]["measurement"] = "component_area"
    result = submit(order, plan)
    assert not result["saved"]
    previous = deepcopy(work.line_state(job, run, "one"))
    payload = json.loads(agent_context.context_messages(job, run, db)[1]["content"])
    assert payload["pricing_proposal"]["plan"] == previous["pricing_proposal"]["plan"]
    assert payload["work_state"]["diagnostics"] == previous["diagnostics"]
    assert "failed_estimate" not in payload["work_state"]
    assert work.line_state(job, run, "one") == previous
    assert payload["active_requirement"]["confirmed"] is False
    assert payload["pricing_packet"]["sources"]
    assert payload["pricing_packet"]["catalog_candidates"] == []


def test_deferred_check_skips_calculation_without_deferrals_and_stops_at_active_item(monkeypatch):
    rows = [RequirementLine(id=f"line{i}", product="Sign").model_dump(mode="json") for i in range(10)]
    job = SimpleNamespace(requirements=rows)
    run = SimpleNamespace(usage={})
    calls = []

    def calculate(db, line, **kwargs):
        calls.append(line.id)
        return {"line_id": line.id, "amount": None, "blockers": ["Missing price"]}

    monkeypatch.setattr(agent_execution, "calculate_line", calculate)
    assert not agent_execution.finish_deferred_quote(None, job, run)
    assert not calls
    work.update_state(job, run, "line0", deferred=True)
    assert not agent_execution.finish_deferred_quote(None, job, run)
    assert calls == ["line1"]


def test_parallel_loop_builds_context_only_for_dispatched_items_and_finalization(
        client, monkeypatch, redis_worker):
    from app import agent_loop, agent_worker, deepseek
    import threading
    calls = []
    original = agent_context.context_messages
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-only-key")
    monkeypatch.setattr(agent_loop.settings(), "agent_pricing_concurrency", 2)
    monkeypatch.setattr(agent_loop.settings(), "agent_slice_rounds", 20)
    monkeypatch.setattr(agent_loop, "MAX_TOKENS", 1)

    def context(*args, **kwargs):
        calls.append(kwargs.get("line_id"))
        return original(*args, **kwargs)

    def turn(messages, tools):
        row = json.loads(messages[1]["content"])["active_requirement"]
        if row is None:
            return call("finish_quote"), {"total_tokens": 2}
        return call("submit_pricing_plan", line_id=row["id"], plan=parallel_proposal()), {"total_tokens": 2}

    monkeypatch.setattr(agent_context, "context_messages", context)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert sorted(call for call in calls if call) == ["line0", "line1"]
    assert calls.count(None) == 1
    assert current["agent_runs"][0]["usage"]["context_compactions"] == 0
    assert not any(step["tool"] == "context_compact" for step in current["agent_runs"][0]["steps"])
    assert current["quotes"][0]["payload"]["total"] == "17.28"
