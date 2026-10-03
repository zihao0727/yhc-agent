"""Verify the configured embedding model and warm the reusable catalog cache."""
import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import statistics
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select, text

from app.config import settings
from app.database import SessionLocal
from app.models import PriceItem
from app import semantic_search


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure-key", action="store_true",
                        help="Save the provided key locally after successful verification")
    args = parser.parse_args()
    key = os.environ.get("SILICONFLOW_API_KEY") or (
        getpass.getpass("SiliconFlow API key (hidden): ") if args.configure_key else semantic_search.get_key())
    if not key:
        raise RuntimeError("Configure a SiliconFlow key first")
    settings().siliconflow_api_key = key
    queries = ["亚克力加紫铜的大型立体发光字", "3毫米亚克力板UV喷绘",
               "字背面发光在墙上形成光晕", "紫铜本色哑光实心雕刻标识", "玻璃门磨砂膜UV印刷"]
    results = []
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        before = list(db.execute(select(PriceItem.id, PriceItem.revision, PriceItem.amount).order_by(PriceItem.id)))
        db.commit()
        for query in queries:
            db.execute(text("SET TRANSACTION READ ONLY"))
            print(f"Testing {settings().siliconflow_embedding_model}: {query}", flush=True)
            start = time.perf_counter()
            result = semantic_search.search_prices(db, query)
            elapsed = time.perf_counter() - start
            if result.get("error"):
                raise RuntimeError(result["error"])
            results.append({"query": query, "seconds": elapsed, "usage": result["embedding_usage"],
                            "candidate_only": result["candidate_only"],
                            "top5": [{k: price[k] for k in (
                                "id", "revision", "product", "material", "thickness_mm",
                                "process", "spec", "similarity")} for price in result["prices"][:5]]})
            db.commit()
            print(f"Returned {len(result['prices'])} candidates in {elapsed:.3f}s; "
                  f"usage={result['embedding_usage']}", flush=True)
        after = list(db.execute(select(PriceItem.id, PriceItem.revision, PriceItem.amount).order_by(PriceItem.id)))
        if before != after:
            raise RuntimeError("Catalog changed during verification; inspect before drawing conclusions")
    if args.configure_key:
        semantic_search.save_key(key)
    key = None
    report = {"tested_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "model": settings().siliconflow_embedding_model, "catalog_rows": len(before),
              "business_prices_unchanged": True, "key_saved_locally": args.configure_key,
              "warm_median_ms": statistics.median(r["seconds"] for r in results[1:]) * 1000,
              "queries": results}
    directory = ROOT / "outputs" / "semantic-search-4b"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "queries"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Verification stopped: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
