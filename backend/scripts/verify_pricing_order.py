"""Read-only live-model acceptance: every source item must have a price or a specific issue."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

from app import deepseek
from app import agent_work_state as work
from app.agent_context import context_messages, model_result
from app.agent_execution import execute_tool
from app.agent_tools import TOOL_SPECS, TOOLS
from app.database import SessionLocal
from app.file_workspace import write_json
from app.models import RequirementJob
from app.pricing_proposals import remember_invalid_plan
from app.quote_engine import calculate_line
from app.requirements_schemas import RequirementLine


def verify_line(job_id, index, turns, previous=None):
    with SessionLocal() as db:
        saved = db.get(RequirementJob, job_id)
        raw = deepcopy(saved.requirements[index])
        existing = calculate_line(db, RequirementLine.model_validate(raw), automatic=True)
        if existing["amount"] is not None and not existing["blockers"]:
            return {"index": index, "line_id": raw["id"], "source_item": raw.get("source_item"),
                    "product": raw["product"], "status": "priced_review", "amount": existing["amount"],
                    "origin": "existing_saved_estimate", "calculation": existing, "records": []}
        # Never overwrite a customer's saved estimate during acceptance.
        job = SimpleNamespace(id=saved.id, requirements=[raw], brief=saved.brief,
            messages=[], extraction=deepcopy(saved.extraction), revision=saved.revision,
            business_rules_version=saved.business_rules_version)
        run = SimpleNamespace(id=None, steps=[], usage={
            "durable": True, "proposal_preview_only": True, "active_line_id": raw["id"]})
        tools = [t for t in TOOLS if t["function"]["name"] in {
            "submit_pricing_plan", "repair_pricing_plan", "defer_requirement", "search_prices", "evaluate_price",
            "inspect_source", "read_source", "list_sources", "read_rules", "read_requirement", "run_calculation"}]
        messages = []
        records, usage, refs = [], {}, set()
        outcome = {"index": index, "line_id": raw["id"], "source_item": raw.get("source_item"),
                   "product": raw["product"], "status": "technical_failure", "amount": None}
        if previous and previous.get("proposal"):
            arguments = {"line_id": raw["id"], "plan": previous["proposal"]["plan"]}
            try:
                parsed = TOOL_SPECS["submit_pricing_plan"][0].model_validate(arguments)
                result = execute_tool(db, job, run, "submit_pricing_plan", parsed, refs)
                if result.get("preview") and result.get("amount") is not None and not result.get("blockers"):
                    outcome.update(status="priced_review", amount=result["amount"], calculation=result)
            except (ValueError, ValidationError) as exc:
                result = {"error": str(exc)[:1600],
                          "plan_version": remember_invalid_plan(job, run, arguments)}
            record = {"tool": "submit_pricing_plan", "arguments": arguments,
                      "restored": True, "result": model_result("submit_pricing_plan", result)}
            records.append(record)
            run.steps.append({**record, "line_id": raw["id"]})
        for _ in range(turns):
            if outcome["status"] == "priced_review":
                break
            context = context_messages(job, run, db)
            current_state = work.line_state(job, run, raw["id"])
            decision_due = current_state.get("decision_due") or _ >= turns - 2
            turn_tools = [t for t in tools if not decision_due or t["function"]["name"] in {
                "submit_pricing_plan", "repair_pricing_plan", "defer_requirement"}]
            context[0]["content"] += (
                "\n当前是只读验收，只处理这一项。预检通过即结束；缺少可靠成本或单字尺寸等事实时"
                "defer_requirement给出具体问题，不能随意用外框面积替代逐字计价。不要调用finish_quote。")
            messages = [*context, *messages[2:]]
            try:
                message, tokens = deepseek.agent_turn(messages, turn_tools)
                for key, value in tokens.items():
                    if isinstance(value, int):
                        usage[key] = usage.get(key, 0) + value
            except deepseek.AgentResponseFailure as exc:
                records.append({"error": "invalid_model_response", "diagnostics": exc.diagnostics})
                for key, value in exc.usage.items():
                    if isinstance(value, int):
                        usage[key] = usage.get(key, 0) + value
                continue
            except deepseek.ModelFailure as exc:
                records.append({"error": str(exc), "diagnostics": exc.diagnostics})
                break
            messages.append(message)
            if not message.get("tool_calls"):
                records.append({"error": "no_tool_call", "text": message.get("content", "")[:2000]})
                messages.append({"role": "user", "content": "请调用工具落实本轮结果：提交或修正计算方案，确实缺依据则defer_requirement提出具体问题。正文不能代替工具执行。"})
                continue
            for call in message.get("tool_calls", []):
                name = call["function"]["name"]
                arguments = {}
                try:
                    if name not in {t["function"]["name"] for t in turn_tools}:
                        raise ValueError("Read-only acceptance rejected unsupported tool")
                    arguments = json.loads(call["function"]["arguments"])
                    parsed = TOOL_SPECS[name][0].model_validate(arguments)
                    if hasattr(parsed, "line_id") and parsed.line_id != raw["id"]:
                        raise ValueError("Only the current line may be processed")
                    if name == "defer_requirement":
                        result = execute_tool(db, job, run, name, parsed, refs)
                        if result.get("deferred"):
                            technical = parsed.cause in ("execution_error", "service_unavailable", "plan_repair")
                            outcome.update(status="technical_failure" if technical else "needs_input", issue=result)
                    else:
                        result = execute_tool(db, job, run, name, parsed, refs)
                        if name not in ("submit_pricing_plan", "repair_pricing_plan"):
                            work.remember(job, run, name, arguments, result)
                        if result.get("preview") and result.get("amount") is not None and not result.get("blockers"):
                            outcome.update(status="priced_review", amount=result["amount"],
                                           calculation=result)
                except (ValueError, ValidationError) as exc:
                    result = {"error": str(exc)[:1600]}
                    if name == "submit_pricing_plan":
                        result["plan_version"] = remember_invalid_plan(job, run, arguments)
                record = {"tool": name, "arguments": arguments, "result": model_result(name, result)}
                records.append(record)
                run.steps.append({**record, "line_id": raw["id"]})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(record["result"], ensure_ascii=False)})
            if outcome["status"] != "technical_failure" or outcome.get("issue"):
                break
        db.rollback()
        return {**outcome, "usage": usage, "records": records,
                "proposal": run.usage.get("work_state", {}).get(raw["id"], {}).get("pricing_proposal")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--indices", default="", help="Comma separated zero-based item indices.")
    parser.add_argument("--resume-from", default="", help="Retain other results from a same-revision report.")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with SessionLocal() as db:
        job = db.get(RequirementJob, args.job)
        count = len(job.requirements)
        revision = job.revision
    if args.limit:
        count = min(count, args.limit)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    indices = [int(i) for i in args.indices.split(",")] if args.indices else list(range(count))
    if any(i < 0 or i >= count for i in indices):
        raise ValueError("Item index outside this order")
    count = len(indices)
    results = []
    previous_items = {}
    if args.resume_from:
        previous = json.loads(Path(args.resume_from).read_text(encoding="utf-8"))
        if previous["job_id"] != args.job or previous["input_revision"] != revision:
            raise ValueError("Cannot merge acceptance results from another order or revision")
        previous_items = {r["index"]: r for r in previous["items"]}
        results = [r for r in previous["items"] if r["index"] not in indices]
        count += len(results)
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.workers))) as pool:
        futures = {pool.submit(verify_line, args.job, index, args.turns, previous_items.get(index)):
                   index for index in indices}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"index": futures[future], "status": "technical_failure",
                          "error": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            report = {"job_id": args.job, "input_revision": revision, "read_only": True,
                      "expected_items": count, "processed_items": len(results),
                      "priced_items": sum(r["status"] == "priced_review" for r in results),
                      "needs_input": sum(r["status"] == "needs_input" for r in results),
                      "technical_failures": sum(r["status"] == "technical_failure" for r in results),
                      "items": sorted(results, key=lambda r: r["index"])}
            write_json(output, report)
            print(json.dumps({k: v for k, v in report.items() if k != "items"}), flush=True)


if __name__ == "__main__":
    main()
