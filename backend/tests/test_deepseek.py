import json
from types import SimpleNamespace

import httpx
import pytest

from app import deepseek
from app.customer_files import prepare_file
from test_requirements import png_bytes


def configure_transport(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-test-key")
    monkeypatch.setattr(deepseek.httpx, "Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def file_fixture():
    item = prepare_file(png_bytes(), "test.png")
    return SimpleNamespace(id=1, **item)


def test_vision_request_has_images_and_strict_output(monkeypatch):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == "deepseek-flash"
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][1]["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")
        result = {"summary": "测试", "lines": [{"product": "门牌", "quantity": 2,
                   "evidence": [{"file_id": 1, "page": 1, "text": "2件"}]}], "questions": ["尺寸?"]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                                        "usage": {"total_tokens": 10}})
    configure_transport(monkeypatch, handler)
    result, attempts, usage = deepseek.extract([file_fixture()], "brief", [], "")
    assert len(result.lines) == 1 and attempts == 1 and usage["total_tokens"] == 10


@pytest.mark.parametrize("invalid", [
    {"summary": "", "lines": [{"product": "字", "price": 1}], "questions": []},
    {"summary": "", "lines": [{"evidence": [{"file_id": 99, "page": 1, "text": "fake"}]}], "questions": []},
    {"summary": "", "lines": [{"quantity": -1}], "questions": []},
    {"lines": [{"components": [{"name": "钢板", "evidence": [
        {"file_id": 99, "page": 1, "text": "fake"}]}]}]},
    {"lines": [{"evidence": [{"file_id": 1, "page": 1, "text": "项目"}],
                "quantity_evidence": [{"file_id": 99, "page": 1, "text": "数量：1个", "quantity": 1}]}]},
])
def test_bad_model_output_is_bounded_and_rejected(monkeypatch, invalid):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(invalid)}}]})
    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as exc:
        deepseek.extract([file_fixture()], "", [], "")
    assert len(calls) == 2 and exc.value.attempts == 2


def test_network_error_no_automatic_paid_retry(monkeypatch):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": "secret provider error should not leak"})
    configure_transport(monkeypatch, handler)
    with pytest.raises(deepseek.ModelFailure) as exc:
        deepseek.extract([file_fixture()], "", [], "")
    assert len(calls) == 1 and "secret" not in str(exc.value)
