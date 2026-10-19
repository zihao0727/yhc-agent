"""Runtime repairs replay validated tools; customer facts and engine rules remain authoritative."""
import json
from copy import deepcopy

from pydantic import Field, JsonValue, ValidationError

from . import agent_work_state as work
from .config import settings
from .file_workspace import workspace_root, write_json
from .requirements_schemas import StrictModel


class RepairToolInput(StrictModel):
    line_id: str = Field(min_length=1, max_length=50)
    failure_id: str = Field(pattern=r"^[a-f0-9]{16}$")
    arguments: dict[str, JsonValue]
    reason: str = Field(min_length=2, max_length=1000)


EXCLUDED = {"repair_failed_tool", "finish_quote", "finish_pending_quote", "ask_user",
            "defer_requirement", "calculate_quote"}
GUIDANCE = (
    "self_repair是执行恢复状态，不是客户缺资料。先检查失败工具的真实schema和指定字段，"
    "用repair_failed_tool提交修正后的完整arguments；程序重新验证并在事务内执行原工具。"
    "不能原样重试。若是工具内部程序异常，可改用其他允许工具完成同一目的；"
    "文件处理可run_file_script写隔离脚本，按错误修改script并关联previous_artifact。"
    "修复不得编造价格、数量、来源，不能改变项目归属、人工确认、计量和审核规则。"
    "只有实际工具执行和报价校验通过才算修复，模型口头声明不算成功。"
)


def schema_hash(name):
    from .agent_tools import TOOL_SPECS
    schema = TOOL_SPECS[name][0].model_json_schema()
    return work.fingerprint(schema)


def hints(name, signature):
    root = workspace_root() / "runtime-repairs"
    if not root.is_dir():
        return []
    matches = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (record.get("tool") == name and record.get("schema_hash") == signature
                and record.get("verified") is True):
            matches.append({key: record[key] for key in (
                "tool", "resolution", "changed_paths", "alternative_tool") if key in record})
            if len(matches) >= 3:
                break
    return matches


def pending(job, run, line_id):
    recovery = work.line_state(job, run, line_id).get("tool_recovery")
    if not recovery:
        return None
    from .agent_tools import TOOL_SPECS
    raw = next((row for row in job.requirements if row["id"] == line_id), None)
    if (raw is None or recovery.get("input_fingerprint") != work.fingerprint(raw)
            or recovery.get("tool") not in TOOL_SPECS
            or recovery.get("schema_hash") != schema_hash(recovery["tool"])):
        return None
    return recovery


def register(job, run, name, arguments, result):
    from .agent_tools import TOOL_SPECS
    line_id = arguments.get("line_id") or run.usage.get("active_line_id")
    if (not settings().agent_runtime_recovery or not run.usage.get("durable")
            or name in EXCLUDED or name not in TOOL_SPECS
            or not any(row["id"] == line_id for row in job.requirements)):
        return False
    state = work.line_state(job, run, line_id)
    attempts = (state.get("runtime_repair_failures", 0) if pending(job, run, line_id) else 0) + 1
    if attempts > settings().agent_runtime_repair_attempts:
        return False
    signature = schema_hash(name)
    failure_id = work.fingerprint([line_id, name, arguments, result.get("type"),
                                   result.get("fields", []), signature])[:16]
    recovery = {"failure_id": failure_id, "tool": name, "arguments": deepcopy(arguments),
                "schema_hash": signature, "input_fingerprint": work.fingerprint(
                    next(row for row in job.requirements if row["id"] == line_id)),
                "error_type": result.get("type", "execution_error"),
                "fields": deepcopy(result.get("fields", [])), "attempts": 0,
                "verified_hints": hints(name, signature)}
    work.update_state(job, run, line_id, tool_recovery=recovery,
                      runtime_repair_failures=attempts, deferred=False)
    run.usage = {**run.usage, "phase": "repairing"}
    result["self_repair"] = {key: recovery[key] for key in (
        "failure_id", "tool", "error_type", "fields", "verified_hints")}
    result["next_action"] = GUIDANCE
    return True


def changed_paths(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        return [change for key in sorted(set(before) | set(after))
                for change in (
                    [path + "/" + key.replace("~", "~0").replace("/", "~1")]
                    if key not in before or key not in after else
                    changed_paths(before[key], after[key],
                                  path + "/" + key.replace("~", "~0").replace("/", "~1")))]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for index, (old, new) in enumerate(zip(before, after))
                for change in changed_paths(old, new, f"{path}/{index}")]
    return [path] if before != after else []


def learn(recovery, arguments=None, alternative=None):
    # Reusable hints contain field paths and tool names, never customer values or rates.
    record = {"tool": recovery["tool"], "schema_hash": recovery["schema_hash"],
              "verified": True, "resolution": "alternative" if alternative else "arguments",
              "changed_paths": changed_paths(recovery["arguments"], arguments)
                  if arguments is not None else [],
              "alternative_tool": alternative}
    root = workspace_root() / "runtime-repairs"
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / (work.fingerprint(record) + ".json"), record)


class RejectedRepair(Exception):
    def __init__(self, result):
        self.result = result


def repair(db, job, run, args, discovered, execute, interrupted=None):
    from .agent_execution import Stopped
    from .agent_tools import TOOL_SPECS
    recovery = pending(job, run, args.line_id)
    raw = next((row for row in job.requirements if row["id"] == args.line_id), None)
    if (not recovery or recovery["failure_id"] != args.failure_id or not raw
            or recovery["input_fingerprint"] != work.fingerprint(raw)):
        current = context(job, run, args.line_id)
        return {"error": "修复目标已变化，不执行旧修复", "saved": False,
                "self_repair": current,
                "next_action": "按当前self_repair修正failure_id和参数" if current else
                               "旧故障已过期；read_requirement读取当前行和已保存方案，"
                               "再按当前来源及诊断继续，不能重复旧failure_id"}
    if run.usage.get("active_line_id") != args.line_id:
        return {"error": "修复只能处理当前分配项目", "saved": False}
    name = recovery["tool"]
    if name in EXCLUDED or schema_hash(name) != recovery["schema_hash"]:
        return {"error": "工具契约已变化，请按当前接口重新处理", "saved": False}
    candidate = deepcopy(args.arguments)
    if candidate.get("line_id", args.line_id) != args.line_id:
        return {"error": "不能通过修复修改其他项目", "saved": False}
    if candidate == recovery["arguments"]:
        return {"error": "修复参数未变化，禁止原样重试", "saved": False}
    if len(json.dumps(candidate, ensure_ascii=False)) > 64000:
        return {"error": "修复参数超过限制", "saved": False}
    if recovery["attempts"] >= settings().agent_runtime_repair_attempts:
        return {"error": "本故障修复次数已达上限，请换工具或暂挂此项", "saved": False}
    attempt = {**recovery, "attempts": recovery["attempts"] + 1}
    work.update_state(job, run, args.line_id, tool_recovery=attempt)
    local_discovered = set(discovered)
    before_other = {row["id"]: work.fingerprint(row) for row in job.requirements
                    if row["id"] != args.line_id}
    from .requirements_schemas import RequirementLine
    protected = {"estimate", "selected_price_id", "selected_price_revision", "price_review_note"}
    before_facts = RequirementLine.model_validate(raw).model_dump(mode="json", exclude=protected)
    before_job = (job.business_rules_version, job.brief, job.title, job.customer)
    try:
        with db.begin_nested():
            if interrupted and interrupted():
                raise Stopped()
            validated = TOOL_SPECS[name][0].model_validate(candidate)
            result = execute(db, job, run, name, validated, local_discovered, interrupted)
            if interrupted and interrupted():
                raise Stopped()
            after_other = {row["id"]: work.fingerprint(row) for row in job.requirements
                           if row["id"] != args.line_id}
            after_raw = next((row for row in job.requirements if row["id"] == args.line_id), None)
            after_facts = RequirementLine.model_validate(after_raw).model_dump(
                mode="json", exclude=protected) if after_raw else None
            if (before_other != after_other or before_facts != after_facts
                    or before_job != (job.business_rules_version, job.brief, job.title, job.customer)):
                raise RejectedRepair({"error": "修复改变了原始需求、人工值或其他项目，已拒绝"})
            if result.get("error") or result.get("blockers") or result.get("status") in (
                    "execution_failed", "needs_repair", "interrupted"):
                raise RejectedRepair(result)
    except Stopped:
        raise
    except ValidationError as exc:
        result = {"error": "修复参数仍未通过真实工具契约", "fields": [
            {"path": ".".join(map(str, error["loc"])), "type": error["type"]}
            for error in exc.errors(include_input=False, include_url=False)[:12]]}
    except RejectedRepair as exc:
        result = exc.result
    except Exception as exc:
        result = {"error": "修复执行仍失败，请换执行路径", "type": type(exc).__name__}
    else:
        discovered.update(local_discovered)
        learn(recovery, arguments=candidate)
        work.update_state(job, run, args.line_id, tool_recovery=None, runtime_repair_failures=0)
        run.usage = {**run.usage, "phase": "running"}
        return {**result, "recovery_verified": True, "repaired_tool": name,
                "repaired_arguments": candidate, "repair_reason": args.reason}
    work.update_state(job, run, args.line_id, tool_recovery={
        **attempt, "last_failure": {key: result[key] for key in ("error", "fields", "type")
                                   if key in result}})
    return {**result, "saved": False, "recovery_verified": False,
            "failure_id": args.failure_id, "repair_attempt": attempt["attempts"],
            "next_action": "修复未通过，业务变更已回滚；按新诊断修改参数或更换允许工具"}


def observe_success(job, run, name, result, previous_state):
    recovery = previous_state.get("tool_recovery")
    if (not recovery or name == "repair_failed_tool"
            or not (result.get("saved") or result.get("selected"))):
        return
    learn(recovery, alternative=name)
    line_id = result.get("line_id") or run.usage.get("active_line_id")
    work.update_state(job, run, line_id, tool_recovery=None, runtime_repair_failures=0)
    result["recovery_verified"] = True
    run.usage = {**run.usage, "phase": "running"}


def context(job, run, line_id):
    recovery = pending(job, run, line_id) if line_id else None
    if not recovery:
        return None
    from .agent_tools import TOOL_SPECS
    return {**recovery, "guidance": GUIDANCE,
            "tool_schema": TOOL_SPECS[recovery["tool"]][0].model_json_schema()}
