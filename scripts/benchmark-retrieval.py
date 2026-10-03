"""Read-only, live-provider retrieval benchmark. Never persists the API key."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / ".runtime" / "retrieval-benchmark-deps"))

import httpx
import numpy as np
from sqlalchemy import or_, select, text

from app.database import SessionLocal
from app.models import PriceItem, Product, RequirementJob

MODEL = "Qwen/Qwen3-Embedding-4B"
INSTRUCTION = ("Given a signage production requirement, retrieve relevant catalog pricing "
               "references matching the product, material and process.")
CASES = [
    ("原词", "迷你字", "迷你字", [1], {}),
    ("原词", "霓虹灯", "霓虹灯", [2], {}),
    ("原词", "平面发光字", "平面发光字", [3], {}),
    ("原词", "高脚背光字", "高脚背光字", [4], {}),
    ("原词", "无边字", "无边字", [5], {}),
    ("原词", "侧发光字", "侧发光字", [6], {}),
    ("原词", "不锈钢平面字", "不锈钢平面字", [7], {}),
    ("原词", "浮雕铝牌", "浮雕铝牌", [16], {}),
    ("原词", "浮雕铜牌", "浮雕铜牌", [17], {}),
    ("原词", "导视组合报价", "导视", [19], {}),
    ("语义", "小尺寸亚克力LED立体发光字体", "迷你字", [1], {}),
    ("语义", "霓虹风格的灯光造型字", "霓虹灯", [2], {}),
    ("语义", "正面发亮、金属包住侧边的字", "包边发光字", [3], {}),
    ("语义", "字背面发光在墙上形成光晕", "背光字", [4], {}),
    ("语义", "正面没有包边的发光字体", "无边字", [5], {}),
    ("语义", "从字体侧面透出光线的立体字", "侧发光字", [6], {}),
    ("语义", "不发光的不锈钢薄板平面文字", "不锈钢平面字", [7], {}),
    ("语义", "铝板雕刻出凹凸立体效果的牌匾", "浮雕铝牌", [16], {}),
    ("语义", "铜板雕刻出凹凸立体效果的牌匾", "浮雕铜牌", [17], {}),
    ("语义", "室外方向指引标识牌带灯和龙骨", "导视", [19], {}),
    ("参数", "3毫米亚克力板UV喷绘", "亚克力 UV打印", [18],
     {"material": "亚克力", "thickness_mm": "3.000", "process_contains": "UV打印"}),
    ("参数", "1.5毫米304不锈钢烤漆", "304# 烤漆", [18],
     {"material": "304#", "thickness_mm": "1.500", "process_contains": "烤漆"}),
    ("参数", "2毫米6061铝板丝网印刷", "6061铝 丝印", [18],
     {"material": "6061铝", "thickness_mm": "2.000", "process_contains": "丝印"}),
    ("参数", "3毫米H62黄铜板镀色", "H62黄铜 电镀", [18],
     {"material": "H62黄铜", "thickness_mm": "3.000", "process_contains": "电镀"}),
    ("参数", "1毫米镀锌板喷漆", "镀锌板 烤漆", [18],
     {"material": "镀锌板", "thickness_mm": "1.000", "process_contains": "烤漆"}),
    ("参数", "5毫米亚克力板不做表面处理", "亚克力 无", [18],
     {"material": "亚克力", "thickness_mm": "5.000", "process_equals": "无 + 无"}),
]
MISSING = ["玻璃门磨砂膜UV印刷", "紫铜本色哑光实心雕刻标识", "水泥浇筑景观雕塑"]


class Embeddings:
    def __init__(self, key):
        self.client = httpx.Client(
            base_url="https://api.siliconflow.cn/v1",
            headers={"Authorization": f"Bearer {key}"}, timeout=60)
        self.lock = threading.Lock()
        self.requests = []

    def embed(self, inputs, phase):
        start = time.perf_counter()
        response = self.client.post("/embeddings", json={
            "model": MODEL, "input": inputs, "encoding_format": "float"})
        elapsed = time.perf_counter() - start
        if response.status_code != 200:
            raise RuntimeError(f"Embedding request failed: HTTP {response.status_code}; phase={phase}")
        payload = response.json()
        data = sorted(payload.get("data", []), key=lambda item: item["index"])
        if len(data) != len(inputs) or [item["index"] for item in data] != list(range(len(inputs))):
            raise RuntimeError("Provider returned an incomplete embedding batch")
        vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
        if vectors.ndim != 2 or not np.isfinite(vectors).all():
            raise RuntimeError("Provider returned invalid vectors")
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise RuntimeError("Provider returned zero vectors")
        with self.lock:
            self.requests.append({"phase": phase, "inputs": len(inputs),
                                  "seconds": elapsed, "usage": payload.get("usage", {})})
        return vectors / norms


def query_text(query):
    return f"Instruct: {INSTRUCTION}\nQuery: {query}"


def load_catalog():
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        records = db.execute(select(PriceItem, Product.name).join(Product).where(
            PriceItem.status == "active").order_by(PriceItem.id)).all()
        rows = []
        for price, name in records:
            row = {field: str(getattr(price, field) or "") for field in (
                "material", "thickness_mm", "spec", "language", "process", "quality", "unit", "notes")}
            row.update(id=price.id, revision=price.revision, product_id=price.product_id, product=name)
            rows.append(row)
        job = db.get(RequirementJob, 8)
        actual = ["；".join(str(raw.get(field) or "") for field in (
            "product", "material", "process", "notes"))[:800] for raw in job.requirements] if job else []
    return rows, actual


def document(row):
    return "\n".join(f"{field}: {row[field]}" for field in (
        "product", "material", "thickness_mm", "spec", "language", "process", "quality", "unit", "notes")
        if row[field])


def keyword_search(db, query):
    statement = select(PriceItem.id).join(Product).where(PriceItem.status == "active")
    for term in re.split(r"\s+", query.strip()):
        if term:
            statement = statement.where(or_(*(column.icontains(term, autoescape=True) for column in (
                Product.name, PriceItem.material, PriceItem.spec, PriceItem.process, PriceItem.notes))))
    return list(db.scalars(statement.order_by(PriceItem.id).limit(20)))


def relevant(row, products, conditions):
    if row["product_id"] not in products:
        return False
    return all(
        value in row["process"] if field == "process_contains" else
        row["process"] == value if field == "process_equals" else row[field] == value
        for field, value in conditions.items())


def scored(ids, expected):
    hits = [i for i, item in enumerate(ids[:10]) if item in expected]
    return {"hit_at_10": int(bool(hits)), "mrr_at_10": 1 / (hits[0] + 1) if hits else 0,
            "relevant_in_top10": len(hits)}


def fused(lexical, semantic):
    scores = {}
    for ranking in (lexical, semantic[:100]):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0) + 1 / (60 + rank)
    return sorted(scores, key=lambda item: (-scores[item], item))


def preview(ids, by_id):
    return [{key: by_id[item][key] for key in (
        "id", "product", "material", "thickness_mm", "spec", "process")} for item in ids[:5]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(ROOT / "outputs" / "embedding-benchmark-4b"))
    args = parser.parse_args()
    key = os.environ.get("SILICONFLOW_API_KEY") or getpass.getpass("SiliconFlow API key (not saved): ")
    if not key.strip():
        raise RuntimeError("API key is required")
    service = Embeddings(key.strip())
    key = None
    rows, actual = load_catalog()
    if not rows or len(rows) > 3000:
        raise RuntimeError("Unexpected catalog size; refusing an unbounded embedding job")
    by_id = {row["id"]: row for row in rows}
    documents = [document(row) for row in rows]
    signature = hashlib.sha256(json.dumps(
        {"model": MODEL, "documents": documents}, ensure_ascii=False).encode()).hexdigest()
    cache_dir = ROOT / ".runtime" / "embedding-benchmark"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{signature}.npz"
    smoke = service.embed(["发光字制作报价", query_text("亚克力UV打印")], "connectivity")
    print(f"Provider connected; dimension={smoke.shape[1]}; active catalog={len(rows)}", flush=True)
    index_started = time.perf_counter()
    cache_hit = cache.exists()
    if cache_hit:
        with np.load(cache, allow_pickle=False) as saved:
            matrix = saved["vectors"]
        print("Reusing this benchmark's unchanged catalog cache", flush=True)
    else:
        matrix = np.empty((len(rows), smoke.shape[1]), dtype=np.float32)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(service.embed, documents[offset:offset + 32], "index"): offset
                       for offset in range(0, len(rows), 32)}
            completed = 0
            for future in as_completed(futures):
                offset = futures[future]
                batch = future.result()
                matrix[offset:offset + len(batch)] = batch
                completed += len(batch)
                if completed % 256 == 0 or completed == len(rows):
                    print(f"Indexed {completed}/{len(rows)} references", flush=True)
        np.savez_compressed(cache, vectors=matrix)
    index_seconds = time.perf_counter() - index_started
    queries = [case[1] for case in CASES] + MISSING + actual
    vectors = np.concatenate([
        service.embed([query_text(query) for query in queries[offset:offset + 32]], "evaluation")
        for offset in range(0, len(queries), 32)])
    vector_started = time.perf_counter()
    similarities = vectors @ matrix.T
    rankings = [[rows[i]["id"] for i in np.argsort(-scores, kind="stable")] for scores in similarities]
    batch_local_seconds = time.perf_counter() - vector_started
    results = []
    sql_times = []
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        for index, (group, query, keywords, products, conditions) in enumerate(CASES):
            expected = {row["id"] for row in rows if relevant(row, products, conditions)}
            if not expected:
                raise RuntimeError(f"Invalid benchmark case: no catalog ground truth for {query}")
            start = time.perf_counter()
            direct = keyword_search(db, query)
            sql_times.append(time.perf_counter() - start)
            manual = keyword_search(db, keywords)
            candidates = {"raw_keyword": direct, "manual_keyword": manual, "vector": rankings[index],
                          "hybrid_manual_keyword": fused(manual, rankings[index])}
            results.append({"group": group, "query": query, "manual_keywords": keywords,
                            "relevance": {"products": products, "conditions": conditions},
                            "relevant_catalog_rows": len(expected),
                            "methods": {name: {**scored(ids, expected), "top5": preview(ids, by_id)}
                                        for name, ids in candidates.items()}})
    latency = []
    probes = [CASES[i][1] for i in (0, 12, 18, 20, 22, 25)]
    for repeat in range(3):
        for query in probes:
            start = time.perf_counter()
            vector = service.embed([query_text(query)], "online_latency")
            embedded_at = time.perf_counter()
            scores = matrix @ vector[0]
            np.argsort(-scores, kind="stable")[:20]
            finished_at = time.perf_counter()
            latency.append({"query": query, "repeat": repeat + 1,
                            "embedding_ms": (embedded_at - start) * 1000,
                            "local_search_ms": (finished_at - embedded_at) * 1000,
                            "total_ms": (finished_at - start) * 1000})
        print(f"Online latency round {repeat + 1}/3 completed", flush=True)
    summaries = {}
    for group in ("全部", "原词", "语义", "参数"):
        subset = results if group == "全部" else [item for item in results if item["group"] == group]
        summaries[group] = {method: {
            "cases": len(subset),
            "hit_at_10": statistics.mean(item["methods"][method]["hit_at_10"] for item in subset),
            "mrr_at_10": statistics.mean(item["methods"][method]["mrr_at_10"] for item in subset)}
            for method in results[0]["methods"]}
    usage = {}
    for request in service.requests:
        phase = request["phase"]
        stats = usage.setdefault(phase, {"requests": 0, "inputs": 0, "total_tokens": 0})
        stats["requests"] += 1
        stats["inputs"] += request["inputs"]
        stats["total_tokens"] += request["usage"].get("total_tokens", 0)
    report = {
        "tested_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "model": MODEL,
        "catalog_rows": len(rows), "dimensions": int(matrix.shape[1]),
        "index": {"cache_hit": cache_hit, "wall_seconds": index_seconds,
                  "vector_bytes_float32": int(matrix.nbytes)},
        "protocol": {
            "top_k": 10, "query_instruction": INSTRUCTION, "document_format": "labelled catalog fields",
            "case_source": "26 manually authored cases; product-family/parameter relevance, not final pricing suitability",
            "raw_keyword": "Current SQL AND/substring search with the full query, first 20 by id",
            "manual_keyword": "Current SQL search with manually chosen short catalog keywords",
            "hybrid_manual_keyword": "RRF of manual keywords and vector search; not an automatic production retriever",
            "unknown_cases": "Unscored; nearest matches are references, not proof of catalog coverage"},
        "summary": summaries,
        "latency": {"samples": len(latency),
                    "online_median_ms": statistics.median(item["total_ms"] for item in latency),
                    "online_p95_ms": float(np.percentile([item["total_ms"] for item in latency], 95)),
                    "embedding_median_ms": statistics.median(item["embedding_ms"] for item in latency),
                    "local_search_median_ms": statistics.median(item["local_search_ms"] for item in latency),
                    "raw_sql_median_ms": statistics.median(sql_times) * 1000,
                    "batch_local_ms_per_query": batch_local_seconds * 1000 / len(queries)},
        "usage": usage, "cases": results, "latency_samples": latency,
        "missing_catalog_examples": [{"query": query, "top5": preview(rankings[len(CASES) + i], by_id)}
                                     for i, query in enumerate(MISSING)],
        "actual_requirement_examples": [{"query": query, "top5": preview(
            rankings[len(CASES) + len(MISSING) + i], by_id)} for i, query in enumerate(actual)],
    }
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    methods = {"raw_keyword": "原句关键词", "manual_keyword": "人工缩词关键词",
               "vector": "Qwen向量", "hybrid_manual_keyword": "人工关键词+向量"}
    lines = [
        "# 报价库向量检索实测", "",
        f"测试时间：{report['tested_at']}", f"模型：`{MODEL}`",
        f"报价库：{len(rows)} 条；向量维度：{matrix.shape[1]}。", "",
        "命中定义：前10条出现预先指定的制作类别，参数案例还须同时符合材质、厚度及工艺。",
        "26个手工编写的小样本，不代表全库准确率；没有验证最终价格适用性或整单报价时间。", "",
        "| 案例组 | 样本数 | 原句关键词 Hit@10 | 人工缩词 Hit@10 | 向量 Hit@10 | 混合 Hit@10 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for group, scores in summaries.items():
        lines.append(f"| {group} | {scores['vector']['cases']} | " + " | ".join(
            f"{scores[method]['hit_at_10']:.1%}" for method in methods) + " |")
    timing = report["latency"]
    lines += ["", "## 速度与用量", "",
              f"- 首次建库/读取缓存：{index_seconds:.2f}秒；缓存命中：{cache_hit}。",
              f"- 18次单查询（复用HTTP连接），中位数：{timing['online_median_ms']:.1f}毫秒；P95：{timing['online_p95_ms']:.1f}毫秒。",
              f"- 在线Embedding中位数：{timing['embedding_median_ms']:.1f}毫秒；本地向量计算中位数：{timing['local_search_median_ms']:.2f}毫秒。",
              f"- 原有SQL检索中位数：{timing['raw_sql_median_ms']:.2f}毫秒。",
              f"- 向量float32原始体积：{matrix.nbytes / 1024 / 1024:.2f}MiB。",
              "- 以下为接口返回用量，不是账户实际扣费；未读取计费账单。", "",
              "```json", json.dumps(usage, ensure_ascii=False, indent=2), "```", "",
              "## 未覆盖与实际需求", "",
              "向量总能找到最近项，不能因此认定存在适用单价。材质替换、未知单位、尺寸档位、价格引用版本仍需AI判断和程序计价复核。"]
    for item in report["missing_catalog_examples"]:
        lines += ["", f"### {item['query']}", *[
            f"- #{row['id']} {row['product']} / {row['material']} / {row['process']}"
            for row in item["top5"][:3]]]
    lines += ["", "完整逐题结果、实际需求候选和延迟样本见同目录 `results.json`。",
              "测试只读数据库；未修改报价Agent、现有报价或生产检索。密钥不保存。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    service.client.close()
    print(json.dumps({"summary": summaries, "latency": report["latency"],
                      "usage": usage, "index": report["index"],
                      "report": str(output / "report.md")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Benchmark stopped: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
