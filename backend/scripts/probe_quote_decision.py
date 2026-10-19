"""Read-only live-model check using a saved line and its audited candidates."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from pydantic import ValidationError

from app import deepseek
from app.agent_context import context_messages, discovered_prices, model_result
from app.agent_execution import execute_tool
from app.agent_tools import TOOLS, TOOL_SPECS, tools_for_stage
from app.database import SessionLocal
from app.models import RequirementJob, AgentRun
from app import agent_work_state as work


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--line", type=int, default=0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        saved = db.get(RequirementJob, args.job)
        prior = db.get(AgentRun, args.run)
        raw = deepcopy(saved.requirements[args.line])
        job = SimpleNamespace(id=saved.id, requirements=[raw], brief=saved.brief,
                              messages=[], extraction={}, business_rules_version=saved.business_rules_version)
        old_state = deepcopy(prior.usage.get("work_state", {}).get(raw["id"], {}))
        run = SimpleNamespace(id=None, steps=deepcopy(prior.steps),
                              usage={"durable": True, "active_line_id": raw["id"],
                                     "work_state": {raw["id"]: {**old_state, "deferred": False,
                                         "line_tokens": 0, "decision_due": True}}})
        messages = context_messages(job, run, db)
        messages[0]["content"] += (
            "\n本次只读核验：依据已有候选立即preview_estimate试算，"
            "或defer_requirement说明具体不可推导的缺项和可回答的问题。不要继续重复读资料。")
        tools = tools_for_stage(TOOLS, "compose", 3, True)
        tools = [t for t in tools if t["function"]["name"] != "select_price"]
        records, usage = [], {}
        discovered = discovered_prices(prior) | work.discovered_references(job, run)
        finished = False
        for _ in range(4):
            message, tokens = deepseek.agent_turn(messages, tools)
            for key, value in tokens.items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value
            messages.append(message)
            for call in message.get("tool_calls", []):
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"])
                try:
                    if name == "preview_estimate":
                        parsed = TOOL_SPECS[name][0].model_validate(arguments)
                        result = execute_tool(db, job, run, name, parsed, discovered)
                        finished = result.get("amount") is not None and not result.get("blockers")
                    elif name in ("defer_requirement", "ask_user"):
                        TOOL_SPECS[name][0].model_validate(arguments)
                        result = {"read_only": True, "question": arguments.get("question"),
                                  "reason": arguments.get("reason")}
                        finished = True
                    else:
                        result = {"error": "Read-only probe rejected unsupported tool"}
                except ValidationError as exc:
                    result = {"error": "参数校验未通过", "fields": [
                        {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]}
                        for e in exc.errors(include_input=False, include_url=False)]}
                except ValueError as exc:
                    result = {"error": str(exc)}
                compact = model_result(name, result)
                records.append({"tool": name, "arguments": arguments, "result": compact})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(compact, ensure_ascii=False, default=str)})
            if finished:
                break
        db.rollback()
        output = {"job_id": saved.id, "line_id": raw["id"], "product": raw["product"],
                  "usage": usage, "records": records}
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"product": raw["product"], "usage": usage, "tools": [
            {"name": entry["tool"], "result": entry["result"]} for entry in records]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
