"""Bounded real-model script probe; writes workspace audit only, never requirements/prices."""
import argparse
import json

from sqlalchemy import select

from app import deepseek, file_workspace as workspace
from app.agent_tools import TOOL_SPECS, tool_definitions
from app.database import SessionLocal
from app.models import CustomerFile, RequirementJob


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int, required=True)
    parser.add_argument("--file", type=int)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--turns", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--resume-artifact")
    args = parser.parse_args()
    with SessionLocal() as db:
        job = db.get(RequirementJob, args.job)
        if not job:
            raise ValueError("Job does not exist")
        files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job.id).order_by(CustomerFile.id)))
        file = next((f for f in files if f.id == args.file), None) if args.file else files[0]
        if not file:
            raise ValueError("File does not belong to job")
        names = {"run_file_script", "read_workspace_artifact", "list_file_workspace"}
        tools = [t for t in tool_definitions() if t["function"]["name"] in names]
        messages = [
            {"role": "system", "content": workspace.WORKSPACE_GUIDANCE
             + "这是只读验收，必须通过脚本读取原件并完整提取指定页产品名称、成品数量及真实来源。"
               "不要报价，不猜尺寸，不把标题和小计当产品。通过提取校验后停止。"
               "extraction.lines每项可包含product/source_item/quantity/evidence/quantity_evidence；"
               "evidence需要file_id,page,sheet,cell,text；quantity_evidence额外提供quantity及raw_quantity。"
               "只处理manifest.source_pages标明的行，不能把整本工作簿混入当前页面。"},
            {"role": "user", "content": json.dumps(
                {"file_id": file.id, "filename": file.filename, "page": args.page}, ensure_ascii=False)}]
        if args.resume_artifact:
            from app.requirements_schemas import ExtractionResult
            prior = workspace.read_artifact(job.id, args.resume_artifact)
            code = (workspace.job_root(job.id) / args.resume_artifact / "input" / "script.py").read_text(encoding="utf-8")
            messages.append({"role": "user", "content": json.dumps({
                "previous_result": workspace.summary(prior), "previous_script": code,
                "extraction_schema": ExtractionResult.model_json_schema(),
                "request": "接着处理。已经读取结构，请在此脚本基础上直接输出extraction，不重复打印整张表。"
                           "注意evidence和quantity_evidence必须为数组；严格按schema，不允许unit、row等额外字段。",
            }, ensure_ascii=False)})
        total, last = 0, None
        for index in range(args.turns):
            message, usage = deepseek.agent_turn(messages, tools)
            total += usage.get("total_tokens", 0)
            messages.append(message)
            for call in message.get("tool_calls", [])[:2]:
                name = call["function"]["name"]
                try:
                    if name not in names:
                        raise ValueError("Probe tool unavailable")
                    parsed = TOOL_SPECS[name][0].model_validate_json(call["function"]["arguments"])
                    if name == "list_file_workspace":
                        result = workspace.workspace_info(job.id, [file])
                    elif name == "read_workspace_artifact":
                        result = workspace.artifact_page(job.id, parsed)
                    else:
                        if parsed.file_ids != [file.id] or parsed.pages != [args.page]:
                            raise ValueError("Probe is restricted to selected file/page")
                        last = workspace.run_script(job.id, [file], parsed, brief=job.brief)
                        result = workspace.summary(last)
                except (ValueError, TypeError) as exc:
                    result = {"error": str(exc)[:2000]}
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
                print(json.dumps({"turn": index + 1, "tool": name, "tokens": total,
                                  "result": result if name == "run_file_script" else "read"}, ensure_ascii=False),
                      flush=True)
            if last and last.get("validation", {}).get("accepted"):
                break
        db.rollback()
        print(json.dumps({"job_id": job.id, "tokens": total,
                          "result": workspace.summary(last) if last else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
