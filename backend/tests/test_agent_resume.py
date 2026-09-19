import json

import pytest

from app import deepseek
from app.agent_requests import is_resume_command, resolve_agent_intent
from app.models import RequirementJob
from app.requirements_schemas import ExtractionResult
from test_agent_loop import call, prepared, provider, start
from test_deepseek import configure_transport, file_fixture
from test_requirements import pdf_bytes, save_lines
import httpx


@pytest.mark.parametrize("text", ["继续", "  请继续报价！ ", "重试", "再试一次", "继续 查价。", "RETRY", "重新计价"])
def test_exact_control_messages_reuse_requirements(text):
    assert is_resume_command(text)
    assert resolve_agent_intent(True, text, True) == "resume"
    assert resolve_agent_intent(True, text, False) == "resume"


@pytest.mark.parametrize("text", [
    "继续，数量改成3件", "继续报价，增加安装费", "重试，宽度改成600mm", "重新识别",
    "继续使用304不锈钢", "数量仍为2件，请重新核对一次。", "retry with 3 items",
])
def test_new_facts_are_never_classified_as_resume(text):
    assert not is_resume_command(text)
    assert resolve_agent_intent(True, text, True) == "extract"
    with pytest.raises(ValueError):
        resolve_agent_intent(True, text, False, "resume")


def test_explicit_intent_and_missing_requirements():
    assert resolve_agent_intent(True, "继续", True, "extract") == "extract"
    assert resolve_agent_intent(True, "", True) == "extract"
    assert resolve_agent_intent(True, "", False) == "resume"
    assert resolve_agent_intent(False, "继续", False, "resume") == "extract"
    with pytest.raises(ValueError):
        resolve_agent_intent(True, "继续", True, "resume")


@pytest.mark.parametrize("options", [
    {"supplementary_text": "继续", "replace_existing": True},
    {"supplementary_text": "继续", "replace_existing": False},
    {"supplementary_text": "继续", "replace_existing": False, "intent": "resume"},
    {"supplementary_text": "重试", "replace_existing": True},
])
def test_failed_29_line_job_resumes_without_reextracting(client, monkeypatch, test_db, options):
    job = prepared(client)
    original = [{**job["requirements"][0], "id": f"line{i}", "manual_unit_price": "100",
                 "manual_price_note": "已保存的人工核价依据"} for i in range(29)]
    job = save_lines(client, job, original)
    with test_db.begin() as db:
        db.get(RequirementJob, job["id"]).status = "failed"
    job = client.get(f'/api/jobs/{job["id"]}').json()
    original = job["requirements"]

    def must_not_extract(*args):
        raise AssertionError("继续不应重新发送PDF识别")

    monkeypatch.setattr(deepseek, "extract", must_not_extract)
    provider(monkeypatch, [call("finish_quote")])
    response = start(client, job, **options)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["requirements"] == original
    assert result["quotes"][0]["payload"]["total"] == "5800.00"
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded"
    assert run["usage"]["reused_line_count"] == 29
    assert run["usage"]["extraction_tokens"] == 0
    assert [step["tool"] for step in run["steps"]] == ["finish_quote"]
    assert result["messages"][-2]["text"] == options["supplementary_text"]


def test_new_fact_requires_replacement_consent(client, monkeypatch):
    job = prepared(client)
    provider(monkeypatch, [])
    assert start(client, job, supplementary_text="继续，数量改成3件").status_code == 409
    assert start(client, job, supplementary_text="继续，数量改成3件", intent="resume").status_code == 422
    assert client.get(f'/api/jobs/{job["id"]}').json()["requirements"] == job["requirements"]


@pytest.mark.parametrize("text,intent", [("继续，数量改成3件", "auto"), ("继续", "extract")])
def test_fact_change_and_explicit_extraction_are_honored(client, monkeypatch, text, intent):
    job = prepared(client)
    calls = []

    def extract(files, brief, previous, supplementary):
        calls.append(supplementary)
        return ExtractionResult.model_validate({"lines": [{
            "product": "修改后的牌", "quantity": 3,
            "evidence": [{"file_id": files[0].id, "page": 1, "text": "来源"}],
        }]}), 1, {"total_tokens": 5}

    monkeypatch.setattr(deepseek, "extract", extract)
    provider(monkeypatch, [call("finish_pending_quote", reason="测试留存新需求")])
    result = start(client, job, supplementary_text=text, replace_existing=True, intent=intent).json()
    assert calls == [text]
    assert result["requirements"][0]["quantity"] == 3
    assert result["requirements"][0]["id"] != job["requirements"][0]["id"]
    assert result["agent_runs"][0]["usage"]["extraction_tokens"] == 5


def test_continue_with_new_attachment_cannot_skip_extraction(client, monkeypatch):
    job = prepared(client)
    job = client.post(f'/api/jobs/{job["id"]}/files', data={"revision": job["revision"]},
                      files={"files": ("补充.pdf", pdf_bytes())}).json()
    seen = []

    def extract(files, *args):
        seen.append(len(files))
        return ExtractionResult.model_validate({"lines": [{"product": "新资料需求", "quantity": 3}]}), 1, {}

    monkeypatch.setattr(deepseek, "extract", extract)
    provider(monkeypatch, [call("finish_pending_quote", reason="测试待核价")])
    result = start(client, job, supplementary_text="继续", intent="resume").json()
    assert seen == [2]
    assert result["requirements"][0]["product"] == "新资料需求"
    assert result["agent_runs"][0]["steps"][0]["tool"] == "extract_requirements"


def test_extraction_failure_records_stage_usage_and_preserves_saved_data(client, monkeypatch):
    job = prepared(client)
    original = job["requirements"]

    def fail(*args):
        raise deepseek.ModelFailure("模型校验失败", 2, {"total_tokens": 17},
                                   {"failures": [{"stage": "file_source"}], "page": 2})

    monkeypatch.setattr(deepseek, "extract", fail)
    provider(monkeypatch, [])
    result = start(client, job, supplementary_text="修改为3件", replace_existing=True).json()
    run = result["agent_runs"][0]
    assert run["status"] == "failed"
    assert result["requirements"] == original
    assert run["usage"]["extraction_tokens"] == run["usage"]["total_tokens"] == 17
    assert run["usage"]["agent_tokens"] == 0
    assert run["steps"][0]["tool"] == "extract_requirements_failed"
    assert run["steps"][0]["result"]["diagnostics"]["page"] == 2
    assert run["steps"][0]["result"]["preserved_line_count"] == 1


def test_real_extraction_schema_diagnostics_do_not_include_model_content(monkeypatch):
    secret = "untrusted-sensitive-output"

    def handler(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"lines": [{"product": secret, "quantity": -1}]})}}],
            "usage": {"total_tokens": 10}})

    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as error:
        deepseek.extract([file_fixture()], "", [], "")
    assert error.value.usage["total_tokens"] == 20
    failures = error.value.diagnostics["failures"]
    assert len(failures) == 2 and failures[0]["stage"] == "schema"
    assert failures[0]["fields"][0]["path"] == "lines.0.quantity"
    assert secret not in json.dumps(error.value.diagnostics)
