"""Run a disposable UI verification API in its own PostgreSQL schema."""
import os
import re
import json
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import PriceItem, PricingRule
from app.services import log_change, product_for
from app import deepseek
from app.requirements_schemas import ExtractionResult
from app.agent_requests import is_resume_command


def mock_extract(files, brief, previous, supplementary):
    if previous and is_resume_command(supplementary):
        raise AssertionError("A continuation message must reuse the saved requirements")
    return ExtractionResult.model_validate({"lines": [{
        "source_item": "1", "product": "自动测试牌", "material": "铝", "thickness_mm": 3,
        "width_mm": 300, "height_mm": 120, "quantity": None if any(
            key in brief for key in ("单价优先测试", "自动补全测试")) else 2, "language": "all",
        "evidence": [{"file_id": files[0].id, "page": 1, "text": "UI测试数据：300x120mm 2件"}] if files else [],
        "text_evidence": [] if files else [(supplementary or brief)[:1000]],
        "components": [{"name": "不锈钢外壳", "material": "304不锈钢", "thickness_mm": "1.2",
                        "geometry": "shell", "processes": ["烤漆"]}] if "待核价测试" in brief else (
                        [{"name": "面板", "material": "304#", "thickness_mm": "1.2",
                          "geometry": "flat_rectangle", "processes": ["烤漆"], "width_mm": 500,
                          "height_mm": 100, "quantity_per_item": 2,
                          "evidence": [{"file_id": files[0].id, "page": 1, "text": "面板500x100，2片"}]}]
                        if "单价优先测试" in brief else []),
    }]}), 1, {"total_tokens": 25}


def mock_agent(messages, tools):
    initial = json.loads(messages[1]["content"])
    previous = [m for m in messages if m["role"] == "tool"]
    index = len(previous)
    history = initial.get("history", [])
    if (history and history[-1].get("role") == "user" and is_resume_command(history[-1].get("text", ""))
            and all(r.get("estimate") or r.get("selected_price_id") or r.get("manual_unit_price") is not None
                    for r in initial["requirements"])):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": "resume-finish", "type": "function",
            "function": {"name": "finish_quote", "arguments": "{}"}}]}, {"total_tokens": 10}
    if "自动补全测试" in initial["brief"]:
        recovered = any(step["tool"] == "response_recovery" for step in initial.get("checkpoint", []))
        if index == 1 and not recovered:
            raise deepseek.AgentResponseFailure("truncated_response",
                                               {"finish_reason": "length", "tool_count": 1},
                                               {"total_tokens": 17})
        if index == 0 and not recovered:
            name, args = "complete_estimate", {"line_id": initial["requirements"][0]["id"], "estimate": {
                "assumptions": [{"field": "quantity", "value": "2", "reason": "测试：按两处位置暂定2件，待审核"}],
                "costs": [
                    {"label": "材料", "rate": "300", "quantity_formula": "=area_m2",
                     "source": "agent_estimate", "reason": "测试：按材料面积估算"},
                    {"label": "加工组装及利润", "rate": "50", "quantity_formula": "=1",
                     "source": "agent_estimate", "reason": "测试：每件制作组装与利润估算"},
                    {"label": "运输", "rate": "20", "quantity_formula": "=1", "basis": "total",
                     "source": "agent_estimate", "reason": "测试：整单运输估算"},
                ], "scope": "含材料、制作组装、利润和运输；不含税及安装，有效期7天，待审核",
            }}
        else:
            name, args = "finish_quote", {}
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"estimate-call-{index}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}, {"total_tokens": 10}
    name, args = "search_prices", {"query": "不存在的测试品"}
    if index == 1:
        args = {"query": "自动测试牌"}
    elif index in (2, 3):
        price = json.loads(previous[1]["content"])["prices"][0]
        name = "evaluate_price" if index == 2 else "select_price"
        args = {"line_id": initial["requirements"][0]["id"], "price_id": price["id"],
                "revision": price["revision"]}
        if index == 3:
            args["reason"] = "测试适用性核对"
    elif index >= 4:
        name, args = "finish_quote", {}
        if "单价优先测试" in initial["brief"]:
            name, args = "finish_pending_quote", {"reason": "请补充成品数量，已先给出单价"}
    if "待核价测试" in initial["brief"]:
        if initial["requirements"][0].get("manual_unit_price") is not None:
            name, args = "finish_quote", {}
        else:
            name, args = "finish_pending_quote", {"reason": "测试：缺少适用的组合成品单价"}
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": f"test-call-{index}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}, {"total_tokens": 10}


def main():
    schema = os.environ["UI_TEST_SCHEMA"]
    if not re.fullmatch(r"test_firefly_[0-9a-f]{32}", schema):
        raise SystemExit("Invalid test schema")
    config = settings()
    config.storage_dir = Path(__file__).resolve().parents[1] / ".runtime" / schema / "uploads"
    config.deepseek_api_key = ""
    config.admin_token = "ui-test-local-only"
    admin = create_engine(config.database_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(config.database_url, connect_args={"options": f"-csearch_path={schema}"})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        product = product_for(db, "测试报价", "铝牌")
        price = PriceItem(product_id=product.id, material="铝", thickness_mm=3, unit="m2",
                          amount=300, status="active", language="all", spec="测试平面门牌", price_kind="standard")
        db.add(price)
        log_change(db, price, "create", "UI测试价格，仅测试schema")
        db.add(PricingRule(product_id=product.id, name="附加费", content="测试：每件 UV 费 5元", rule_type="reference"))
        automatic_product = product_for(db, "测试报价", "自动测试牌")
        db.add(PriceItem(product_id=automatic_product.id, material="铝", thickness_mm=3,
                         unit="m2", amount=300, status="active", language="all"))
        material_product = product_for(db, "测试报价", "材料+表面处理建议价")
        db.add(PriceItem(product_id=material_product.id, material="304#", thickness_mm="1.2",
                         unit="m2", amount=150, status="active", process="烤漆 + 无",
                         notes="测试建议单价，非成品价",
                         attributes={"process_1": "烤漆", "process_2": "无"}))
        db.add(PricingRule(name="UI历史案例", rule_type="case", content=json.dumps({
            "kind": "manual_quote_v1", "source_item": "1", "product": "自动测试牌",
            "formula": "=193*2.5*16+100*2", "quantity": 1, "tax_rate": "0.13",
            "issues": ["历史系数待确认，未套用"], "inputs": {},
        }, ensure_ascii=False)))

    deepseek.extract = mock_extract
    deepseek.agent_turn = mock_agent

    def override():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = override
    uvicorn.run(app, host="127.0.0.1", port=8001)


if __name__ == "__main__":
    main()
