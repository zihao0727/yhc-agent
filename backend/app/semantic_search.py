"""Semantic candidates are references, never automatic price selections."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid

import httpx
import numpy as np
from sqlalchemy import select

from .config import settings
from .models import PriceItem, Product
from .quote_engine import preload_sources, price_source
from .services import snapshot

logger = logging.getLogger(__name__)

INSTRUCTION = ("Given a signage production requirement, retrieve relevant catalog pricing "
               "references matching the product, material and process.")
NOTICE = ("相似度仅表示语义接近，不代表材质、尺寸、工艺、计价单位或价格适用性一致。"
          "由AI核对候选差异并自主选择参考或估价，不自动采用第一条。")
INDEX_LOCK = threading.Lock()
BATCH_SIZE = 32
WORKERS = 4


class EmbeddingFailure(Exception):
    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage or {}


def key_path():
    return settings().storage_dir.parent / "siliconflow.json"


def get_key():
    if settings().siliconflow_api_key:
        return settings().siliconflow_api_key
    try:
        return json.loads(key_path().read_text(encoding="utf-8")).get("api_key", "")
    except (OSError, ValueError, AttributeError):
        return ""


def save_key(value):
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"api_key": value}), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cache_path():
    return settings().storage_dir.parent / "semantic-prices.sqlite3"


def document(price, name):
    fields = {"product": name, **{field: getattr(price, field) for field in (
        "material", "thickness_mm", "spec", "language", "process", "quality", "unit", "notes")}}
    return "\n".join(f"{field}: {value}" for field, value in fields.items() if value is not None and value != "")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Provider:
    def __init__(self):
        config = settings()
        key = get_key()
        if not key:
            raise EmbeddingFailure("尚未配置硅基流动密钥，可使用关键词检索或自主估价")
        self.model = config.siliconflow_embedding_model
        self.namespace = digest(config.siliconflow_base_url + "|" + self.model + "|catalog-fields-v1")
        self.client = httpx.Client(
            base_url=config.siliconflow_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {key}"},
            timeout=config.siliconflow_timeout, follow_redirects=False)
        self.lock = threading.Lock()
        self.usage = {"total_tokens": 0, "index_tokens": 0, "query_tokens": 0, "requests": 0}

    def embed(self, inputs, phase):
        with self.lock:
            self.usage["requests"] += 1
        logger.info("SiliconFlow embedding request: model=%s, phase=%s, inputs=%d",
                    self.model, phase, len(inputs))
        try:
            response = self.client.post("embeddings", json={
                "model": self.model, "input": inputs, "encoding_format": "float"})
        except httpx.HTTPError as exc:
            logger.warning("SiliconFlow embedding network error: %s", type(exc).__name__)
            raise EmbeddingFailure("硅基流动连接超时或网络异常，可改用关键词检索") from None
        if response.status_code != 200:
            logger.warning("SiliconFlow embedding rejected: status=%d", response.status_code)
            raise EmbeddingFailure(
                f"硅基流动向量服务拒绝或暂时无法处理请求（HTTP {response.status_code}），"
                "请检查密钥、余额及模型配置；可改用关键词检索") from None
        try:
            body = response.json()
            tokens = body.get("usage", {}).get("total_tokens", 0)
            if type(tokens) is not int or not 0 <= tokens <= 10**9:
                tokens = 0
            with self.lock:
                self.usage["total_tokens"] += tokens
                self.usage[f"{phase}_tokens"] += tokens
            data = sorted(body["data"], key=lambda item: item["index"])
            if len(data) != len(inputs) or [item["index"] for item in data] != list(range(len(inputs))):
                raise ValueError("Incomplete vectors")
            vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
            if (vectors.ndim != 2 or not 1 <= vectors.shape[1] <= 8192
                    or not np.isfinite(vectors).all()):
                raise ValueError("Invalid vectors")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0) or not np.isfinite(norms).all():
                raise ValueError("Invalid vector norm")
            return vectors / norms
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise EmbeddingFailure("向量响应不完整或格式无效，本轮未采用候选；可改用关键词检索") from None

    def close(self):
        self.client.close()


def cached_vectors(provider, documents, check):
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    hashes = [digest(value) for value in documents]
    found = {}
    # Cache writes are independent of the business transaction and survive interruption.
    with INDEX_LOCK, sqlite3.connect(path, timeout=30) as cache:
        cache.execute("""CREATE TABLE IF NOT EXISTS embeddings (
            namespace TEXT NOT NULL, content_hash TEXT NOT NULL,
            dimensions INTEGER NOT NULL, vector BLOB NOT NULL,
            PRIMARY KEY(namespace, content_hash))""")
        for key, dimensions, blob in cache.execute(
                "SELECT content_hash, dimensions, vector FROM embeddings WHERE namespace=?",
                (provider.namespace,)):
            if not 1 <= dimensions <= 8192 or len(blob) != dimensions * 4:
                continue
            vector = np.frombuffer(blob, dtype="<f4")
            if np.isfinite(vector).all() and abs(float(np.linalg.norm(vector)) - 1) < .01:
                found[key] = vector
        cached = sum(key in found for key in hashes)
        missing = list(dict.fromkeys(key for key in hashes if key not in found))
        texts = dict(zip(hashes, documents))
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            for offset in range(0, len(missing), BATCH_SIZE * WORKERS):
                check()
                keys = missing[offset:offset + BATCH_SIZE * WORKERS]
                futures = {}
                for start in range(0, len(keys), BATCH_SIZE):
                    batch = keys[start:start + BATCH_SIZE]
                    futures[executor.submit(provider.embed, [texts[key] for key in batch], "index")] = batch
                for future in as_completed(futures):
                    vectors = future.result()
                    for key, vector in zip(futures[future], vectors):
                        cache.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?, ?)", (
                            provider.namespace, key, len(vector), vector.astype("<f4").tobytes()))
                        found[key] = vector
                    cache.commit()
                    check()
        try:
            matrix = np.stack([found[key] for key in hashes])
        except ValueError:
            raise EmbeddingFailure("缓存向量维度不一致，请检查向量模型配置") from None
    return matrix, cached


def search_prices(db, query, material="", status="active", offset=0, check=lambda: None):
    provider = None
    try:
        provider = Provider()
        statement = select(PriceItem, Product.name).join(Product)
        statement = statement.where(PriceItem.status != "inactive" if status == "all" else PriceItem.status == status)
        if material:
            statement = statement.where(PriceItem.material.contains(material, autoescape=True))
        records = db.execute(statement.order_by(PriceItem.id)).all()
        ids = [price.id for price, name in records]
        documents = [document(price, name) for price, name in records]
        # Do not hold the job row lock while waiting for external embedding requests.
        db.commit()
        if not ids:
            return {"prices": [], "total": 0, "next_offset": None, "model": provider.model,
                    "candidate_only": True, "notice": NOTICE, "embedding_usage": provider.usage}
        check()
        matrix, cache_hits = cached_vectors(provider, documents, check)
        check()
        vector = provider.embed([f"Instruct: {INSTRUCTION}\nQuery: {query}"], "query")[0]
        check()
        if matrix.shape[1] != len(vector):
            raise EmbeddingFailure("查询与报价库向量维度不一致，本轮未采用候选")
        similarities = matrix @ vector
        ranking = np.argsort(-similarities, kind="stable")
        # Re-read current prices after the network call; never return stale snapshots.
        current = {price.id: (price, name) for price, name in db.execute(
            statement.execution_options(populate_existing=True)).all()}
        candidates = []
        for index in ranking:
            pair = current.get(ids[index])
            if pair is None or document(*pair) != documents[index]:
                continue
            candidates.append((pair, float(similarities[index])))
        page = candidates[offset:offset + 20]
        preload_sources(db, [price for (price, _), _ in page])
        prices = [{**snapshot(price), "product": name, "source": price_source(db, price),
                   "similarity": round(score, 6)}
                  for (price, name), score in page]
        return {"prices": prices, "total": len(candidates),
                "next_offset": offset + 20 if offset + 20 < len(candidates) else None,
                "model": provider.model, "candidate_only": True, "notice": NOTICE,
                "embedding_usage": {**provider.usage, "cache_hits": cache_hits,
                                    "catalog_rows": len(ids), "dimensions": int(matrix.shape[1])}}
    except EmbeddingFailure as exc:
        return {"error": str(exc), "candidate_only": True, "prices": [],
                "embedding_usage": dict(provider.usage) if provider else exc.usage,
                "next_action": "由AI决定使用search_prices关键词检索、调整条件或提出有依据的待审核估价"}
    except (sqlite3.Error, OSError):
        return {"error": "向量缓存暂时不可用，已保存的需求及价格不变；可改用关键词检索",
                "candidate_only": True, "prices": [],
                "embedding_usage": dict(provider.usage) if provider else {},
                "next_action": "可使用search_prices继续，不要求向量检索成功后才能报价"}
    finally:
        if provider:
            provider.close()
