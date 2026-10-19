from decimal import Decimal

import pytest
from sqlalchemy import select

from app import deepseek
from app.calculation_scripts import run_script
from app.collaborative_quotes import consume_reply
from app.conversation import RequirementChange, changed_requirements
from app.models import AgentRun, RequirementJob
from app.requirements_schemas import RequirementLine
from app.quote_engine import calculate_line
from test_agent_loop import call, provider
from test_pipeline_v3 import line_v3
from test_requirements import save_lines


def scripted_line():
    line = line_v3()
    line.estimate.costs[0].measurement = "script"
    line.estimate.costs[0].calculation_script = (
        'assert width_mm > 0, "width must be positive"\n'
        'front = width_mm * height_mm / 1000000\n'
        'result = front * 2')
    line.estimate.costs[0].measurement_note = "Two panels, millimetres converted to square metres"
    return RequirementLine.model_validate(line.model_dump())


def new_job(client):
    result = client.post("/api/jobs", data={"title": "Collaborative quote", "brief": "Two panels"})
    assert result.status_code == 201, result.text
    return save_lines(client, result.json(), [scripted_line().model_dump(mode="json")])


def chat(client, job, text):
    return client.post(f'/api/jobs/{job["id"]}/conversation', json={
        "revision": job["revision"], "supplementary_text": text, "allow_external_processing": True})


def test_script_decimal_and_checks():
    result = run_script('area = width_mm * height_mm / 1000000\n'
                        'assert area > 0, "positive area"\n'
                        'result = max(area, 0.1)', {"width_mm": 100, "height_mm": 300})
    assert result["result"] == "0.1"
    assert result["inputs"] == {"height_mm": "300", "width_mm": "100"}
    assert result["checks"][0]["passed"]
    assert len(result["sha256"]) == 64
    assert run_script("result = ceil(quantity / 3) if quantity > 3 else 1",
                      {"quantity": 4})["result"] == "2"


@pytest.mark.parametrize("source", [
    "import os\nresult = 1", "result = __import__('os')", "result = width_mm.real",
    "while True:\n result = 1", "result = [1] * 1000000000", "result = 10 ** 999999",
    "width_mm = 1\nresult = width_mm", "result = open('secret')", "result = 1 / 0",
    "result = 1e999", "assert 1 == 2, 'wrong'\nresult = 1",
    "result = unknown", "result = True", "result = -1", "result = min()",
])
def test_script_rejects_unsafe_or_invalid_programs(source):
    with pytest.raises(ValueError):
        run_script(source, {"width_mm": 100})


def test_script_replays_with_new_dimensions_and_difficulty_once():
    line = scripted_line()
    result = calculate_line(None, line, automatic=True)
    assert not result["blockers"], result
    assert result["amount"] == "240.00"
    assert result["estimate_costs"][0]["script_run"]["result"] == "1.0"
    line.width_mm = Decimal(2000)
    newer = calculate_line(None, line, automatic=True)
    assert newer["amount"] == "480.00"
    assert newer["estimate_costs"][0]["script_run"]["inputs"]["width_mm"] == "2000"
    assert newer["estimate_costs"][1]["difficulty_multiplier"] == "2"


def test_script_cannot_bypass_order_or_power_rules():
    raw = scripted_line().model_dump()
    raw["estimate"]["costs"][0]["calculation_script"] = "result = quantity"
    with pytest.raises(ValueError, match="订购数量"):
        RequirementLine.model_validate(raw)
    line = scripted_line()
    line.estimate.costs[0].kind = "power"
    assert calculate_line(None, line, automatic=True)["amount"] is None


def test_apply_chat_is_atomic_recalculates_and_preserves_history(client, monkeypatch):
    job = new_job(client)
    change = {"line_id": "test", "field": "width_mm", "value": "2000", "reason": "User changed width"}
    provider(monkeypatch, [call("respond", action="apply", answer="根据新宽度重算。", changes=[change])])
    response = chat(client, job, "宽度改成2米")
    assert response.status_code == 200, response.text
    result = response.json()
    current = result["job"]
    assert result["applied"]
    assert current["requirements"][0]["width_mm"] == "2000"
    assert current["quotes"][0]["payload"]["total"] == "480.00"
    assert current["quotes"][0]["outdated"] is False
    assert current["quotes"][0]["status"] == "draft"
    provider(monkeypatch, [call("respond", action="apply", answer="修改数量。", changes=[
        {"line_id": "test", "field": "quantity", "value": "3", "reason": "User changed quantity"}])])
    response = chat(client, current, "改成3件")
    assert response.status_code == 200, response.text
    latest = response.json()["job"]
    assert latest["quotes"][0]["payload"]["total"] == "720.00"
    assert latest["quotes"][1]["payload"]["total"] == "480.00"
    assert latest["quotes"][1]["outdated"]
    assert chat(client, job, "旧版本修改").status_code == 409


def test_preview_changes_does_not_modify_original(client, monkeypatch):
    job = new_job(client)
    change = {"line_id": "test", "field": "quantity", "value": "3", "reason": "Compare three"}
    provider(monkeypatch, [
        call("preview_changes", revision=job["revision"], changes=[change]),
        call("respond", action="preview", answer="三件的试算方案。", changes=[change]),
    ])
    response = chat(client, job, "比较一下3件多少钱")
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result["applied"]
    assert result["preview"]["total"] == "360.00"
    assert result["job"]["revision"] == job["revision"]
    assert result["job"]["requirements"] == job["requirements"]
    assert result["job"]["quotes"] == job["quotes"]


def test_add_remove_and_cost_edit(client, monkeypatch):
    job = new_job(client)
    provider(monkeypatch, [call("respond", action="apply", answer="新增项目并列费。", operations=[
        {"kind": "add", "line_id": "new", "reason": "User added sign",
         "new_line": {"product": "Sign", "quantity": 3}},
        {"kind": "estimate", "line_id": "new", "reason": "User provided rate", "estimate": {
            "contract_version": 3, "pricing_strategy": "item", "scope": "Sign manufacturing",
            "costs": [{"label": "Sign", "kind": "finished", "measurement": "item_count",
                       "rate": 10, "rate_unit": "piece", "reason": "User quoted 10 per item"}]}},
        {"kind": "remove", "line_id": "test", "reason": "User removed panels"},
    ])])
    response = chat(client, job, "删掉原项目，新增标牌3个，制作单价10元")
    assert response.status_code == 200, response.text
    result = response.json()["job"]
    assert [row["id"] for row in result["requirements"]] == ["new"]
    assert result["quotes"][0]["payload"]["total"] == "30.00"


def test_material_change_invalidates_old_estimate():
    line = scripted_line().model_dump(mode="json")
    line["manual_unit_price"] = "500"
    rows = changed_requirements([line], [
        RequirementChange(line_id="test", field="material", value="steel", reason="User changed material")])
    assert rows[0]["estimate"] is None and rows[0]["manual_unit_price"] is None
    assert line["estimate"] is not None


def test_live_reply_queues_then_applies_at_boundary(client, monkeypatch, test_db):
    job = new_job(client)
    with test_db.begin() as db:
        current = db.get(RequirementJob, job["id"])
        current.status = "processing"
        run = AgentRun(job_id=current.id, model="test", usage={"prepared": True, "durable": True})
        db.add(run)
    response = chat(client, job, "宽度改为2米")
    assert response.status_code == 200, response.text
    assert response.json()["queued"]
    assert response.json()["job"]["requirements"][0]["width_mm"] == "1000"
    provider(monkeypatch, [call("respond", action="apply", answer="已收到宽度调整。", changes=[
        {"line_id": "test", "field": "width_mm", "value": "2000", "reason": "User reply"}])])
    with test_db() as db:
        current = db.get(RequirementJob, job["id"])
        run = db.scalar(select(AgentRun).where(AgentRun.job_id == current.id))
        assert consume_reply(db, current, run)
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["status"] == "processing"
    assert current["requirements"][0]["width_mm"] == "2000"
    assert current["quotes"][0]["payload"]["total"] == "480.00"
    assert current["quotes"][0]["outdated"] is False
    assert not any(message.get("pending_reply") for message in current["messages"])


def test_read_tools_are_available_in_conversation(client, monkeypatch):
    job = new_job(client)
    provider(monkeypatch, [
        call("list_sources"), call("read_rules", query="", offset=0),
        call("run_calculation", line_id="test", script="result = width_mm * height_mm / 1000000"),
        call("respond", action="answer", answer="单面面积为0.5平方米，临时方案包含两块。"),
    ])
    response = chat(client, job, "核对面积和规则")
    assert response.status_code == 200, response.text
    audit = response.json()["job"]["messages"][-1]["tool_audit"]
    assert len(audit) == 3
    assert audit[-1]["result"]["result"] == "0.5"
    assert response.json()["job"]["revision"] == job["revision"]


def test_invalid_edit_rolls_back_every_operation(client, monkeypatch):
    job = new_job(client)
    provider(monkeypatch, [call("respond", action="apply", answer="修改。", changes=[
        {"line_id": "test", "field": "quantity", "value": "3", "reason": "User edit"}],
        operations=[{"kind": "remove", "line_id": "missing", "reason": "Invalid item"}])])
    assert chat(client, job, "修改数量并删除一项").status_code == 422
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["requirements"] == job["requirements"]
    assert current["revision"] == job["revision"] and not current["quotes"]


def test_reply_arriving_during_finish_is_applied_before_completion(client, monkeypatch):
    job = new_job(client)
    calls = 0
    monkeypatch.setattr(deepseek, "get_key", lambda: "test-key")

    def respond(messages, tools):
        nonlocal calls
        calls += 1
        if calls == 1:
            queued = chat(client, job, "数量改为3")
            assert queued.status_code == 200, queued.text
            assert queued.json()["queued"]
            message = call("finish_quote")
        elif calls == 2:
            message = call("respond", action="apply", answer="修改数量。", changes=[
                {"line_id": "test", "field": "quantity", "value": "3", "reason": "User reply"}])
        else:
            message = call("finish_quote")
        return message, {"total_tokens": 10}

    monkeypatch.setattr(deepseek, "agent_turn", respond)
    response = client.post(f'/api/jobs/{job["id"]}/agent', json={
        "revision": job["revision"], "intent": "resume", "allow_external_processing": True})
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["requirements"][0]["quantity"] == 3
    assert current["quotes"][0]["payload"]["total"] == "360.00"
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert not any(m.get("pending_reply") for m in current["messages"])


def test_object_edits_bind_stable_ids_and_preserve_other_objects():
    line = line_v3(components=[
        {"object_id": "front", "name": "Front", "width_mm": 100, "height_mm": 200,
         "quantity_per_item": 1},
        {"object_id": "back", "name": "Back", "width_mm": 100, "height_mm": 200,
         "quantity_per_item": 1}]).model_dump(mode="json")
    edited = changed_requirements([line], [RequirementChange(
        line_id="test", field="width_mm", value="300", reason="User changed front",
        target="component", target_id="front")])[0]
    assert edited["components"][0]["width_mm"] == "300"
    assert edited["components"][1] == line["components"][1]
    assert edited["width_mm"] == line["width_mm"]
