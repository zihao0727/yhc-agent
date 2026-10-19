import json
import threading
from copy import deepcopy

import pytest

from app import agent_loop, agent_worker, deepseek, runtime_recovery
from app.agent_execution import Stopped
from app.agent_tools import TOOL_SPECS
from app.config import settings
from app.models import AgentRun, RequirementJob
from app import agent_work_state as work
from app.runtime_recovery import RepairToolInput
from test_agent_loop import call
from test_parallel_pricing import proposal
from test_pricing_proposals import order
from test_redis_agent import enqueue, redis_worker


def register(order, name="read_requirement", extra="sensitive-value"):
    db, job, run = order
    raw = {"line_id": "one", "forbidden": extra}
    result = {"error": "Invalid parameters", "type": "ValidationError",
              "fields": [{"path": "forbidden", "type": "extra_forbidden"}]}
    assert runtime_recovery.register(job, run, name, raw, result)
    db.commit()
    return runtime_recovery.pending(job, run, "one"), result


def repair_args(recovery, **changes):
    return RepairToolInput(line_id="one", failure_id=recovery["failure_id"],
                           arguments={"line_id": "one", **changes}, reason="Correct the tool contract")


def test_validated_replay_learns_paths_without_copying_customer_values(order):
    from app.agent_execution import execute_tool
    db, job, run = order
    recovery, error = register(order)
    assert "sensitive-value" not in json.dumps(error)
    result = runtime_recovery.repair(db, job, run, repair_args(recovery), set(), execute_tool)
    assert result["recovery_verified"] and result["requirement"]["id"] == "one"
    assert runtime_recovery.pending(job, run, "one") is None
    memory = runtime_recovery.hints("read_requirement", recovery["schema_hash"])
    assert memory[0]["changed_paths"] == ["/forbidden"]
    assert "sensitive-value" not in json.dumps(memory)
    assert runtime_recovery.hints("read_requirement", "old-schema") == []


def test_repair_transaction_rejects_and_rolls_back_changed_original_facts(order):
    db, job, run = order
    recovery, _ = register(order)
    before = deepcopy(job.requirements)
    discovered = {(1, 2)}

    def corrupt(db, job, run, name, args, refs, interrupted):
        job.requirements = [{**row, "quantity": 999} for row in job.requirements]
        refs.add((999, 1))
        db.flush()
        return {"saved": True}

    result = runtime_recovery.repair(db, job, run, repair_args(recovery), discovered, corrupt)
    assert not result["recovery_verified"] and not result["saved"]
    assert job.requirements == before
    assert discovered == {(1, 2)}
    assert runtime_recovery.pending(job, run, "one")["attempts"] == 1
    assert runtime_recovery.hints("read_requirement", recovery["schema_hash"]) == []


def test_invalid_repair_args_cannot_execute_or_repeat_forever(order, monkeypatch):
    db, job, run = order
    recovery, _ = register(order)
    monkeypatch.setattr(settings(), "agent_runtime_repair_attempts", 1)
    executed = []
    result = runtime_recovery.repair(db, job, run, repair_args(recovery, extra=True),
                                    set(), lambda *args: executed.append(args))
    assert not result["recovery_verified"] and result["fields"][0]["path"] == "extra"
    result = runtime_recovery.repair(db, job, run, repair_args(recovery),
                                    set(), lambda *args: executed.append(args))
    assert "上限" in result["error"] and not executed


def test_stale_cross_item_and_unchanged_repairs_are_rejected(order):
    db, job, run = order
    recovery, _ = register(order)
    executed = []
    for args in (
        repair_args(recovery).model_copy(update={"failure_id": "0" * 16}),
        repair_args(recovery).model_copy(update={"arguments": {"line_id": "other"}}),
        repair_args(recovery).model_copy(update={"arguments": recovery["arguments"]}),
    ):
        result = runtime_recovery.repair(db, job, run, args, set(), lambda *args: executed.append(args))
        assert result["error"] and not executed
    job.requirements = [{**row, "width_mm": "900"} for row in job.requirements]
    assert runtime_recovery.repair(db, job, run, repair_args(recovery),
                                  set(), lambda *args: executed.append(args))["error"]
    assert not executed


@pytest.mark.parametrize("stale_field", ["input_fingerprint", "schema_hash"])
def test_expired_recovery_is_not_advertised_and_does_not_exhaust_new_repairs(order, stale_field):
    db, job, run = order
    recovery, _ = register(order)
    plan = {"version": 3, "plan": {"scope": "Keep the saved failed pricing plan"}}
    work.update_state(job, run, "one", pricing_proposal=plan,
                      tool_recovery={**recovery, stale_field: "outdated"},
                      runtime_repair_failures=settings().agent_runtime_repair_attempts)
    assert runtime_recovery.pending(job, run, "one") is None
    assert runtime_recovery.context(job, run, "one") is None
    result = runtime_recovery.repair(db, job, run, repair_args(recovery),
                                     set(), lambda *args: pytest.fail("Stale repair executed"))
    assert result["self_repair"] is None and "read_requirement" in result["next_action"]
    assert work.line_state(job, run, "one")["pricing_proposal"] == plan
    new, _ = register(order, extra="new-invalid-field")
    assert new["failure_id"] != recovery["failure_id"]
    assert work.line_state(job, run, "one")["runtime_repair_failures"] == 1


def test_wrong_failure_id_returns_current_recovery_without_executing(order):
    db, job, run = order
    recovery, _ = register(order)
    result = runtime_recovery.repair(db, job, run,
        repair_args(recovery).model_copy(update={"failure_id": "0" * 16}),
        set(), lambda *args: pytest.fail("Wrong failure executed"))
    assert result["self_repair"]["failure_id"] == recovery["failure_id"]


def test_manual_stop_interrupts_repair_before_replaying(order):
    db, job, run = order
    recovery, _ = register(order)
    executed = []
    with pytest.raises(Stopped):
        runtime_recovery.repair(db, job, run, repair_args(recovery),
                               set(), lambda *args: executed.append(args), lambda: True)
    assert not executed


def test_schema_error_self_repairs_and_finishes_without_restart(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test")
    attempts = []

    def turn(messages, tools):
        context = json.loads(messages[1]["content"])
        row = context["active_requirement"]
        if row is None:
            return call("finish_quote"), {}
        attempts.append(context)
        if len(attempts) == 1:
            return call("read_requirement", line_id=row["id"], forbidden="sensitive-value"), {}
        if len(attempts) == 2:
            recovery = context["self_repair"]
            assert recovery["tool_schema"] == TOOL_SPECS["read_requirement"][0].model_json_schema()
            return call("repair_failed_tool", line_id=row["id"], failure_id=recovery["failure_id"],
                        arguments={"line_id": row["id"]}, reason="Remove the unsupported field"), {}
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 1)
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded"
    repaired = next(step for step in run["steps"] if step["tool"] == "repair_failed_tool")
    assert repaired["result"]["recovery_verified"]
    assert current["requirements"][0]["estimate"]
    assert not run["usage"]["deferred_requirements"]


def test_internal_tool_fault_uses_alternative_and_validates_price(
        client, monkeypatch, redis_worker):
    monkeypatch.setattr(deepseek, "get_key", lambda: "test")
    original_execute = agent_loop.execute_tool
    seen = []

    def execute(db, job, run, name, args, discovered, interrupted=None):
        if name == "analyze_components":
            raise KeyError("internal defect")
        return original_execute(db, job, run, name, args, discovered, interrupted)

    def turn(messages, tools):
        context = json.loads(messages[1]["content"])
        row = context["active_requirement"]
        if row is None:
            return call("finish_quote"), {}
        seen.append(context)
        if len(seen) == 1:
            return call("analyze_components", line_id=row["id"]), {}
        assert context["self_repair"]["error_type"] == "KeyError"
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(agent_loop, "execute_tool", execute)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 1)
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    run = current["agent_runs"][0]
    assert run["status"] == "succeeded" and current["requirements"][0]["estimate"]
    assert not run["usage"]["deferred_requirements"]
    memory = runtime_recovery.hints("analyze_components", runtime_recovery.schema_hash("analyze_components"))
    assert memory[0]["alternative_tool"] == "submit_pricing_plan"


def test_context_failure_keeps_repair_tools_and_can_finish_validated_order(
        client, monkeypatch, redis_worker):
    from app import agent_context
    monkeypatch.setattr(deepseek, "get_key", lambda: "test")

    def broken_context(*args, **kwargs):
        raise KeyError("broken enrichment")

    def turn(messages, tools):
        context = json.loads(messages[1]["content"])
        assert context["context_recovery"]["type"] == "KeyError"
        row = context["active_requirement"]
        if row is None:
            return call("finish_quote"), {}
        assert any(tool["function"]["name"] == "run_file_script" for tool in tools)
        return call("submit_pricing_plan", line_id=row["id"], plan=proposal()), {}

    monkeypatch.setattr(agent_context, "context_messages", broken_context)
    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 1)
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["quotes"][0]["payload"]["total"] is not None
    assert current["requirements"][0]["estimate"]


def test_parallel_recovery_does_not_repeat_saved_peer(client, monkeypatch, redis_worker):
    from collections import Counter
    monkeypatch.setattr(deepseek, "get_key", lambda: "test")
    monkeypatch.setattr(settings(), "agent_pricing_concurrency", 2)
    turns = Counter()

    def turn(messages, tools):
        context = json.loads(messages[1]["content"])
        row = context["active_requirement"]
        if row is None:
            return call("finish_quote"), {}
        line_id = row["id"]
        turns[line_id] += 1
        if line_id == "line0" and turns[line_id] == 1:
            return call("read_requirement", line_id=line_id, forbidden=True), {}
        if line_id == "line0" and turns[line_id] == 2:
            return call("repair_failed_tool", line_id=line_id,
                        failure_id=context["self_repair"]["failure_id"],
                        arguments={"line_id": line_id}, reason="Remove the unsupported field"), {}
        return call("submit_pricing_plan", line_id=line_id, plan=proposal()), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    job = enqueue(client, monkeypatch, 2)
    for _ in range(4):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert turns == {"line0": 3, "line1": 1}
    assert all(row["estimate"] for row in current["requirements"])


def test_repair_entry_requires_a_pending_fault_and_does_not_expand_research():
    from app.agent_tools import TOOLS, tools_for_stage
    ordinary = {tool["function"]["name"] for tool in tools_for_stage(TOOLS, "compose", 3, True)}
    recovering = {tool["function"]["name"] for tool in tools_for_stage(
        TOOLS, "compose", 3, True, runtime_repair=True)}
    assert "repair_failed_tool" not in ordinary
    assert recovering - ordinary == {"repair_failed_tool"}
    assert "search_prices" not in recovering
    assert {"read_source", "read_requirement", "run_file_script"} <= recovering
