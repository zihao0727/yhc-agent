import json
import threading
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from sqlalchemy import select

from app import agent_loop, semantic_search
from app.config import settings
from app.models import PriceItem, RequirementJob
from test_agent_loop import call, prepared, provider, seed, start
from test_redis_agent import enqueue, redis_worker


@pytest.fixture
def embeddings(monkeypatch):
    monkeypatch.setattr(settings(), "siliconflow_api_key", "fake-vector-key")
    calls = []

    def embed(self, inputs, phase):
        calls.append((phase, inputs))
        self.usage["requests"] += 1
        self.usage["total_tokens"] += 10
        self.usage[f"{phase}_tokens"] += 10
        return np.asarray([[1.0, 0.0] if "发光" in value else [0.0, 1.0]
                           for value in inputs], dtype=np.float32)

    monkeypatch.setattr(semantic_search.Provider, "embed", embed)
    return calls


def test_config_has_separate_redacted_credentials_and_default_4b(client):
    model = client.get("/api/model-config").json()
    response = client.get("/api/embedding-config")
    assert response.status_code == 200
    assert response.json()["model"] == "Qwen/Qwen3-Embedding-4B"
    assert not response.json()["configured"]
    key = "sk-vector-test-secret-only"
    response = client.put("/api/embedding-config", json={"api_key": key})
    assert response.status_code == 200 and response.json()["configured"]
    assert key not in response.text
    assert semantic_search.get_key() == key
    assert client.get("/api/model-config").json() == model
    assert client.get("/api/embedding-config", headers={"X-Admin-Token": "invalid"}).status_code == 401
    assert client.delete("/api/embedding-config").status_code == 200
    assert not semantic_search.get_key()


def test_environment_managed_key_cannot_be_replaced_or_deleted(client, monkeypatch):
    monkeypatch.setattr(settings(), "siliconflow_api_key", "environment-test-secret")
    assert client.get("/api/embedding-config").json()["environment_managed"]
    assert client.put("/api/embedding-config", json={"api_key": "new-test-secret"}).status_code == 409
    assert client.delete("/api/embedding-config").status_code == 409


def test_vector_candidates_never_select_or_modify_requirements(
        client, price_input, monkeypatch, test_db, embeddings):
    first = seed(client, price_input, product="发光字参考一")
    seed(client, price_input, product="其他工艺")
    job = prepared(client)
    with test_db() as db:
        before = db.get(RequirementJob, job["id"]).requirements
        result = semantic_search.search_prices(db, "户外发光字")
        assert result["prices"][0]["id"] == first["id"]
        assert result["prices"][0]["similarity"] == 1.0
        assert result["candidate_only"] and "不自动采用" in result["notice"]
        assert result["model"] == "Qwen/Qwen3-Embedding-4B"
        assert result["prices"][0]["revision"] == first["revision"]
        assert db.get(RequirementJob, job["id"]).requirements == before
        assert all(line["selected_price_id"] is None for line in before)


def test_cache_reuses_documents_and_reads_new_amount_revision_without_reembedding(
        client, price_input, test_db, embeddings):
    price = seed(client, price_input, product="发光字")
    with test_db() as db:
        semantic_search.search_prices(db, "发光字")
        changed = db.get(PriceItem, price["id"])
        changed.amount = "450"
        db.commit()
        result = semantic_search.search_prices(db, "发光字")
        assert result["prices"][0]["amount"] == "450.000000"
        assert result["prices"][0]["revision"] > price["revision"]
        assert result["embedding_usage"]["cache_hits"] == 1
    assert [phase for phase, _ in embeddings].count("index") == 1
    assert [phase for phase, _ in embeddings].count("query") == 2


def test_editing_semantic_fields_and_changing_model_invalidates_cache(
        client, price_input, test_db, monkeypatch, embeddings):
    price = seed(client, price_input, product="发光字")
    with test_db() as db:
        semantic_search.search_prices(db, "发光字")
        changed = db.get(PriceItem, price["id"])
        changed.process = "电镀"
        db.commit()
        semantic_search.search_prices(db, "发光字")
        monkeypatch.setattr(settings(), "siliconflow_embedding_model", "Qwen/Qwen3-Embedding-0.6B")
        semantic_search.search_prices(db, "发光字")
    assert [phase for phase, _ in embeddings].count("index") == 3


def test_price_changes_during_query_are_refetched_not_returned_as_stale(
        client, price_input, test_db, monkeypatch, embeddings):
    price = seed(client, price_input, product="发光字")
    real = semantic_search.Provider.embed

    def change(self, inputs, phase):
        result = real(self, inputs, phase)
        if phase == "query":
            with test_db.begin() as other:
                item = other.get(PriceItem, price["id"])
                item.amount = "550"
        return result

    monkeypatch.setattr(semantic_search.Provider, "embed", change)
    with test_db() as db:
        result = semantic_search.search_prices(db, "发光字")
    assert result["prices"][0]["amount"] == "550.000000"
    assert result["prices"][0]["revision"] > price["revision"]


@pytest.mark.parametrize("field,value", [("status", "inactive"), ("process", "另一种工艺")])
def test_stopped_or_semantically_changed_price_is_removed_after_query(
        client, price_input, test_db, monkeypatch, embeddings, field, value):
    price = seed(client, price_input, product="发光字")
    real = semantic_search.Provider.embed

    def change(self, inputs, phase):
        result = real(self, inputs, phase)
        if phase == "query":
            with test_db.begin() as other:
                setattr(other.get(PriceItem, price["id"]), field, value)
        return result

    monkeypatch.setattr(semantic_search.Provider, "embed", change)
    with test_db() as db:
        result = semantic_search.search_prices(db, "发光字")
    assert not result["prices"] and result["total"] == 0


def test_filters_pagination_and_empty_results(client, price_input, test_db, embeddings):
    for i in range(22):
        seed(client, price_input, product=f"发光字{i}", material="铝")
    draft = seed(client, price_input, product="发光字草稿", status="draft", material="铜")
    with test_db() as db:
        first = semantic_search.search_prices(db, "发光字")
        second = semantic_search.search_prices(db, "发光字", offset=20)
        assert len(first["prices"]) == 20 and first["next_offset"] == 20
        assert len(second["prices"]) == 2 and second["next_offset"] is None
        assert not set(p["id"] for p in first["prices"]) & set(p["id"] for p in second["prices"])
        assert semantic_search.search_prices(db, "发光字", "铜", "all")["prices"][0]["id"] == draft["id"]
        assert semantic_search.search_prices(db, "发光字", "不存在材质")["total"] == 0


def test_model_can_choose_lower_ranked_catalog_candidate_and_finish(
        client, price_input, test_db, monkeypatch, embeddings):
    first = seed(client, price_input, product="发光字参考一")
    second = seed(client, price_input, product="发光字参考二", amount="500")
    job = prepared(client)
    seen = provider(monkeypatch, [
        call("search_prices_semantic", query="发光字"),
        call("complete_estimate", line_id="line1", estimate={
            "scope": "自主判断第二候选更适用，列明条件待审核", "costs": [{
                "label": "制作费用", "rate": "500", "quantity_formula": "=area_m2",
                "source": "catalog", "reference_id": second["id"],
                "reference_revision": second["revision"], "reason": "比较候选后自主采用第二候选"}]}),
        call("finish_quote"),
    ])
    result = start(client, job).json()
    run = result["agent_runs"][0]
    assert run["status"] == "succeeded", run
    assert run["steps"][0]["result"]["prices"][0]["id"] == first["id"]
    assert result["requirements"][0]["selected_price_id"] is None
    assert result["requirements"][0]["estimate"]["costs"][0]["reference_id"] == second["id"]
    assert result["quotes"][0]["payload"]["total"] == "36.00"
    assert run["usage"]["embedding_tokens"] == 20
    assert run["usage"]["embedding_model"] == "Qwen/Qwen3-Embedding-4B"
    tool_result = json.loads(seen[1][-1]["content"])
    assert tool_result["prices"][0]["similarity"] == 1
    assert tool_result["candidate_only"]
    assert (second["id"], second["revision"]) in agent_loop.discovered_prices(
        SimpleNamespace(steps=run["steps"]))


def test_semantic_tool_failure_does_not_block_ai_completion(client, monkeypatch):
    job = prepared(client)
    seen = provider(monkeypatch, [
        call("search_prices_semantic", query="发光字"),
        call("ask_user", question="用户明确禁止估算，等待补充"),
    ])
    result = start(client, job).json()
    assert "尚未配置" in result["agent_runs"][0]["steps"][0]["result"]["error"]
    assert "search_prices" in json.loads(seen[1][-1]["content"])["next_action"]


def test_tool_availability_follows_embedding_configuration(client, monkeypatch):
    from app import deepseek
    job = prepared(client)
    monkeypatch.setattr(deepseek, "get_key", lambda: "fake-quote-key")
    seen = []

    def turn(messages, tools):
        seen.append({tool["function"]["name"] for tool in tools})
        return call("ask_user", question="用户要求等待补充"), {}

    monkeypatch.setattr(deepseek, "agent_turn", turn)
    start(client, job)
    assert "search_prices_semantic" not in seen[-1]
    semantic_search.save_key("configured-vector-test-key")
    job = client.get(f'/api/jobs/{job["id"]}').json()
    start(client, job)
    assert "search_prices_semantic" in seen[-1]


def test_semantic_discovery_survives_worker_yield(
        client, price_input, monkeypatch, embeddings, redis_worker):
    from app import agent_worker
    price = seed(client, price_input, product="发光字")
    monkeypatch.setattr(settings(), "agent_slice_rounds", 1)
    provider(monkeypatch, [
        call("search_prices_semantic", query="发光字"),
        call("complete_estimate", line_id="line0", estimate={
            "scope": "引用自主核对的制作费用，数量暂定待审核",
            "assumptions": [{"field": "quantity", "value": "2", "reason": "两处暂定，等待人工审核"}],
            "costs": [{"label": "制作", "rate": "300", "quantity_formula": "=area_m2",
                      "source": "catalog", "reference_id": price["id"],
                      "reference_revision": price["revision"], "reason": "模型自主核对参考条件"}]}),
        call("finish_quote"),
    ])
    job = enqueue(client, monkeypatch, 1)
    for _ in range(3):
        agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "succeeded"
    assert current["quotes"][0]["payload"]["total"] == "21.60"
    assert current["agent_runs"][0]["usage"]["embedding_tokens"] == 20


def test_manual_stop_during_embedding_does_not_write_tool_result(
        client, price_input, monkeypatch, embeddings, redis_worker):
    from app import agent_worker
    seed(client, price_input, product="发光字")
    provider(monkeypatch, [call("search_prices_semantic", query="发光字")])
    job = enqueue(client, monkeypatch, 1)
    real = semantic_search.Provider.embed

    def stop(self, inputs, phase):
        result = real(self, inputs, phase)
        if phase == "query":
            response = client.post(f'/api/jobs/{job["id"]}/agent/stop', json={
                "revision": job["revision"], "reason": "向量请求期间管理员停止"})
            assert response.status_code == 200
        return result

    monkeypatch.setattr(semantic_search.Provider, "embed", stop)
    agent_worker.run_pending_once(threading.Event())
    current = client.get(f'/api/jobs/{job["id"]}').json()
    assert current["agent_runs"][0]["status"] == "cancelled"
    assert current["agent_runs"][0]["steps"] == []
    assert current["requirements"] == job["requirements"]
    assert not current["quotes"]


def test_search_checks_interruption_and_does_not_return_candidates(
        client, price_input, test_db, embeddings):
    seed(client, price_input, product="发光字")
    count = 0

    def stop():
        nonlocal count
        count += 1
        if count >= 3:
            raise agent_loop.Stopped()

    with test_db() as db, pytest.raises(agent_loop.Stopped):
        semantic_search.search_prices(db, "发光字", check=stop)
    assert not any(phase == "query" for phase, _ in embeddings)


def test_provider_wire_model_and_instruction_have_no_credentials_in_result(monkeypatch):
    monkeypatch.setattr(settings(), "siliconflow_api_key", "fake-vector-key")
    original = httpx.Client
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer fake-vector-key"
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0, 3]}, {"index": 0, "embedding": [4, 0]}],
            "usage": {"total_tokens": 20}})

    monkeypatch.setattr(semantic_search.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    service = semantic_search.Provider()
    result = service.embed(["doc1", "doc2"], "index")
    service.close()
    assert requests[0]["model"] == "Qwen/Qwen3-Embedding-4B"
    assert result.tolist() == [[1, 0], [0, 1]]
    assert service.usage["index_tokens"] == 20


@pytest.mark.parametrize("body", [
    {"data": [{"index": 0, "embedding": [0, 0]}]},
    {"data": [{"index": 0, "embedding": [float("nan"), 1]}]},
    {"data": []}, {"data": "invalid"}, {"data": [{"index": 1, "embedding": [1, 0]}]},
    {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},
])
def test_malformed_vectors_are_rejected(monkeypatch, body):
    monkeypatch.setattr(settings(), "siliconflow_api_key", "fake-vector-key")
    original = httpx.Client
    # Pre-encode nonfinite values to exercise the actual provider JSON parser.
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))
    monkeypatch.setattr(semantic_search.httpx, "Client", lambda **kwargs: original(
        transport=transport, **kwargs))
    service = semantic_search.Provider()
    with pytest.raises(semantic_search.EmbeddingFailure):
        service.embed(["doc"], "index")
    service.close()


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_provider_error_does_not_echo_body_or_key(monkeypatch, status):
    monkeypatch.setattr(settings(), "siliconflow_api_key", "fake-vector-key")
    original = httpx.Client
    monkeypatch.setattr(semantic_search.httpx, "Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            status, json={"error": "secret body fake-vector-key"})), **kwargs))
    service = semantic_search.Provider()
    with pytest.raises(semantic_search.EmbeddingFailure) as error:
        service.embed(["doc"], "index")
    assert "secret" not in str(error.value) and "fake-vector-key" not in str(error.value)
    assert str(status) in str(error.value)
    service.close()
