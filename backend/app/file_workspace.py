"""Order-scoped script artifacts and host-validated extraction proposals."""
import hashlib
import json
import re
import shutil
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import Field
from sqlalchemy import select

from .config import settings
from .customer_files import pdf_page_text, storage_path
from .models import CustomerFile
from .requirements_schemas import StrictModel


class WorkspaceInput(StrictModel):
    pass


class FileScriptInput(StrictModel):
    file_ids: list[int] = Field(min_length=1, max_length=4)
    script: str = Field(min_length=1, max_length=30000)
    purpose: str = Field(min_length=2, max_length=500)
    # Explicit pages keep the prompt and validator bounded; scripts may inspect the originals.
    pages: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=20)
    previous_artifact: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")


class ArtifactInput(StrictModel):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    offset: int = Field(default=0, ge=0)
    section: Literal["result", "script"] = "result"


class AdapterInput(StrictModel):
    adapter_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_ids: list[int] = Field(min_length=1, max_length=4)
    pages: list[int] = Field(default_factory=lambda: [1], min_length=1, max_length=20)


class SaveAdapterInput(StrictModel):
    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=2, max_length=100)


WORKSPACE_GUIDANCE = (
    "遇到特殊表格、解析错误或需独立核验时，先list_file_workspace查看输入约定和既有适配器，"
    "可run_file_script编写Python读取本订单原文件，错误后用previous_artifact关联修正重试。"
    "脚本在无网络容器执行，可导入openpyxl/xlrd/pandas/pypdf/PIL/docx/标准库。"
    "读取/input/manifest.json，其files含file_id/path/pages/media_type，source_pages含原文text及页号。"
    "path始终是原件；CSV/TSV/TXT/DOCX/XLS另有normalized_path可用openpyxl读取，单元格origin指回原文位置。"
    "原件只读，临时文件写/work，结果必须写/work/result.json（JSON对象）。"
    "仅print不会交付结果；用json.dump(result, open('/work/result.json','w',encoding='utf-8'),ensure_ascii=False)。"
    "结果可含observations、checks；提取结果放extraction，结构同ExtractionResult，每行须保留evidence。"
    "evidence和quantity_evidence均为数组，raw_quantity为字符串；不要添加unit或row等未定义字段。"
    "程序独立核验来源/数量/尺寸/重复/覆盖，脚本自称通过不能替代校验，结果不自动改写报价或人工值。"
    "脚本不要硬编码客户内容、价格或file_id，应从manifest读取。"
    "验证通过的提取脚本可save_file_adapter保存为候选；复用仍须在新文件上重新校验。"
    "工程量单位m/m2/m3/kg/hour须放source_measurements，不冒充成品数量；无法确认的业务参数保持未知并说明。"
    "没有原文的扫描图不能伪造文本证据，须inspect_source或read_extraction_source核图。"
)


def workspace_root():
    # Follows the configured storage root, including isolated test storage.
    return settings().storage_dir.resolve() / "_workspaces"


def job_root(job_id):
    if type(job_id) is not int or job_id <= 0:
        raise ValueError("无效订单")
    root = workspace_root() / str(job_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path, value):
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def read_artifact(job_id, artifact_id):
    if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise ValueError("无效工作区记录")
    path = job_root(job_id) / artifact_id / "audit.json"
    if not path.is_file():
        raise ValueError("工作区记录不属于本订单或尚未完成")
    return json.loads(path.read_text(encoding="utf-8"))


def selected_files(db, job_id, ids):
    files = list(db.scalars(select(CustomerFile).where(CustomerFile.job_id == job_id,
                                                      CustomerFile.id.in_(ids))))
    if {f.id for f in files} != set(ids):
        raise ValueError("文件不属于本订单")
    return files


def source_layout(files, texts):
    from .customer_excel import MEDIA_TYPE, table_columns
    layouts = []
    for file in files:
        pages = []
        for (fid, page), text in sorted(texts.items()):
            if fid != file.id:
                continue
            if file.media_type == MEDIA_TYPE:
                source = json.loads(text)
                columns, header = table_columns(source)
                headers = [[c["value"] for c in row["cells"]] for row in source["rows"] if row["row"] == header]
                pages.append({"columns": columns, "headers": headers})
            else:
                # Conservative exact-source matching until a trusted layout detector exists.
                pages.append({"sha256": file.sha256, "page": page})
        layouts.append({"type": file.media_type, "pages": pages})
    return hashlib.sha256(json.dumps(layouts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_output(output, files, texts, brief="", supplementary="", **validation):
    from .extraction_validation import validate_extraction, ExtractionValidationFailure
    if not isinstance(output, dict):
        raise ValueError("result.json必须为JSON对象")
    raw = deepcopy(output.get("extraction"))
    if raw is None:
        return {"status": "observations_only", "valid": False, "accepted": False,
                "notice": "仅为脚本观察，尚未通过需求校验，不可直接计价"}
    if not isinstance(raw, dict) or not isinstance(raw.get("lines"), list) or not raw["lines"]:
        raise ValueError("extraction必须包含非空lines")
    original = deepcopy(raw["lines"])
    seen = set()
    for line in raw["lines"]:
        if not isinstance(line, dict) or not line.get("evidence"):
            raise ValueError("每项必须提供原始文件来源")
        evidence = line["evidence"]
        if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
            raise ValueError("evidence必须是来源对象数组，例如[{file_id:1,page:1,sheet:'清单',cell:'B2',text:'产品'}]")
        key = (line.get("source_item", ""), line.get("product", ""),
               tuple(sorted((e.get("file_id", 0), e.get("page", 0), e.get("sheet", ""), e.get("cell", ""),
                             e.get("text", "")) for e in evidence)))
        if key in seen:
            raise ValueError("发现重复来源项目，请合并或明确区分子项目")
        seen.add(key)
        for e in evidence:
            text = texts.get((e.get("file_id"), e.get("page")))
            if text is None:
                raise ValueError("引用来源不在本次提供的页面范围")
            if not e.get("cell") and (e.get("source", "text") != "text"
                                     or not e.get("text") or e["text"] not in text):
                raise ValueError("脚本提取的非单元格来源必须引用可核实原文；图像观察须另行核图")
    try:
        scope = set(texts)
        if validation.get("page_scope") is not None:
            scope &= set(validation.pop("page_scope"))
        else:
            validation.pop("page_scope", None)
        result = validate_extraction(raw, files, texts, brief, supplementary,
                                     page_scope=scope, **validation)
    except ExtractionValidationFailure as exc:
        return {"status": "needs_repair", "valid": False, "accepted": False, "diagnostics": exc.diagnostics}
    missing = [c.model_dump(mode="json") for c in result.source_coverage if c.status == "unresolved"]
    no_quantity = [i for i, line in enumerate(result.lines) if line.quantity is None]
    corrected = []
    for index, (claim, line) in enumerate(zip(original, result.lines)):
        for field in ("quantity", "width_mm", "height_mm", "depth_mm", "thickness_mm", "area_m2"):
            value = claim.get(field)
            if value is not None and str(value) != str(getattr(line, field)):
                from decimal import Decimal, InvalidOperation
                try:
                    equal = Decimal(str(value)) == Decimal(str(getattr(line, field)))
                except InvalidOperation:
                    equal = False
                if not equal:
                    corrected.append({"line_index": index, "field": field,
                                      "claimed": value, "validated": str(getattr(line, field))})
    warnings = [i for i, line in enumerate(result.lines) if line.uncertainties]
    locations, duplicate_sources = {}, []
    for index, line in enumerate(result.lines):
        identity = tuple(sorted({(e.file_id, e.page, e.sheet, e.cell, e.text) for e in line.evidence}))
        if identity in locations:
            duplicate_sources.append([locations[identity], index])
        else:
            locations[identity] = index
    # Existing fallback may restore omitted Excel rows; expose them rather than claim completeness.
    restored = max(0, len(result.lines) - len(raw["lines"]))
    valid = not (missing or no_quantity or restored or corrected or warnings or duplicate_sources)
    return {"status": "validated" if valid else "needs_review",
            "valid": valid, "corrected_fields": corrected, "lines_with_warnings": warnings,
            "accepted": not duplicate_sources,
            "duplicate_sources": duplicate_sources,
            "unresolved_sources": missing[:50], "quantity_missing": no_quantity,
            "restored_rows": restored, "extraction": result.model_dump(mode="json"),
            "notice": "来源及结构校验不代表商业价格或图纸完整性已获批准"}


def run_script(job_id, files, args, *, texts=None, brief="", supplementary="",
               check=lambda: None, validation=None, observation_mode=False):
    from .file_sandbox import run_python
    if args.previous_artifact:
        read_artifact(job_id, args.previous_artifact)
    if set(args.file_ids) != {f.id for f in files}:
        raise ValueError("文件范围不匹配")
    if any(f.job_id != job_id for f in files):
        raise ValueError("文件不属于本订单")
    check()
    if texts is None:
        texts = {}
        for file in files:
            for page in sorted(set(args.pages)):
                if not 1 <= page <= file.page_count:
                    raise ValueError("页码超出所选文件范围")
                texts[file.id, page] = pdf_page_text(file, page)
    else:
        texts = {k: v for k, v in texts.items() if k[0] in args.file_ids and k[1] in args.pages}
        if not texts:
            raise ValueError("本批未提供请求的文件页面")
    artifact_id = uuid.uuid4().hex
    directory = job_root(job_id) / artifact_id
    inputs = directory / "input"
    inputs.mkdir(parents=True)
    code_hash = hashlib.sha256(args.script.encode()).hexdigest()
    audit = {"artifact_id": artifact_id, "job_id": job_id, "purpose": args.purpose,
             "previous_artifact": args.previous_artifact, "script_sha256": code_hash,
             "created_at": datetime.now(timezone.utc).isoformat(),
             "status": "running", "file_hashes": {str(f.id): f.sha256 for f in files},
             "layout": source_layout(files, texts)}
    write_json(directory / "audit.json", audit)
    manifest = {"files": [], "source_pages": [
        {"file_id": fid, "page": page, "text": text} for (fid, page), text in sorted(texts.items())]}
    try:
        for file in files:
            check()
            stored = storage_path(file.storage_key)
            conversion = (json.loads((stored / "conversion.json").read_text(encoding="utf-8"))
                          if (stored / "conversion.json").is_file() else None)
            suffix = Path(file.filename).suffix.casefold() if conversion else {"application/pdf": ".pdf",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                      "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(file.media_type, ".bin")
            name = f"file-{file.id}{suffix}"
            source = storage_path(file.storage_key) / "original"
            shutil.copyfile(source, inputs / name)
            with (inputs / name).open("rb") as copied:
                digest = hashlib.file_digest(copied, "sha256").hexdigest()
            if digest != file.sha256:
                raise ValueError("原文件校验和已变化，请重新核对来源")
            entry = {"file_id": file.id, "filename": file.filename,
                     "media_type": conversion["original_media_type"] if conversion else file.media_type,
                     "pages": file.page_count, "sha256": digest, "path": f"/input/{name}"}
            if conversion:
                normalized_name = f"file-{file.id}-normalized.xlsx"
                if hashlib.sha256((stored / "normalized.xlsx").read_bytes()).hexdigest() != conversion["normalized_sha256"]:
                    raise ValueError("转换文件校验和已变化，请重新解析原件")
                shutil.copyfile(stored / "normalized.xlsx", inputs / normalized_name)
                entry.update(normalized_path=f"/input/{normalized_name}",
                             normalized_media_type=file.media_type, conversion=conversion["warnings"])
            manifest["files"].append(entry)
        write_json(inputs / "manifest.json", manifest)
        (inputs / "script.py").write_text(args.script, encoding="utf-8")
        result = run_python(inputs, check)
        check()
        audit["execution"] = result
        if result.get("ok"):
            try:
                if observation_mode:
                    if not isinstance(result.get("output"), dict):
                        raise ValueError("result.json必须为JSON对象")
                    checked = {"status": "observations_only", "valid": False, "accepted": False,
                               "notice": "脚本输出仅为观察，由资料Agent结合原件判断，不自动修改需求"}
                else:
                    checked = validate_output(result.get("output"), files, texts, brief, supplementary,
                                              **(validation or {}))
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                checked = {"status": "needs_repair", "valid": False, "accepted": False, "error": str(exc)[:1200]}
            audit["validation"] = checked
            audit["status"] = checked["status"]
        else:
            audit["status"] = "execution_failed"
        write_json(directory / "audit.json", audit)
        return audit
    except BaseException:
        audit["status"] = "interrupted"
        write_json(directory / "audit.json", audit)
        raise
    finally:
        # Keep code and manifest for audit, not duplicate potentially large originals.
        for path in inputs.glob("file-*"):
            if path.is_file():
                path.unlink()


def summary(audit):
    validation = audit.get("validation", {})
    execution = audit.get("execution", {})
    result = {k: audit.get(k) for k in ("artifact_id", "status", "purpose", "script_sha256", "previous_artifact")}
    next_action = "用read_workspace_artifact读取已校验提取结果；保留待确认字段，再进入原有计价流程"
    if audit["status"] == "observations_only":
        next_action = "已完成原件观察；下一次run_file_script须输出extraction，不重复打印整表；关联previous_artifact"
    elif audit["status"] in ("execution_failed", "needs_repair", "interrupted"):
        next_action = "根据具体错误修改script，关联previous_artifact重试；结果必须写/work/result.json"
    result.update(validation={k: v for k, v in validation.items() if k != "extraction"},
                  cause=execution.get("cause"),
                  execution_error=execution.get("error"), log=execution.get("log", "")[:4000],
                  line_count=len(validation.get("extraction", {}).get("lines", [])),
                  next_action=next_action)
    return result


def workspace_info(job_id, files):
    from .file_sandbox import availability
    from .requirements_schemas import ExtractionResult
    paths = sorted(job_root(job_id).glob("*/audit.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    return {"runtime": availability(), "instructions": WORKSPACE_GUIDANCE,
            "files": [{"file_id": f.id, "filename": f.filename, "pages": f.page_count} for f in files],
            "recent": [summary(json.loads(p.read_text(encoding="utf-8"))) for p in paths],
            "adapters": list_adapters(), "extraction_schema": ExtractionResult.model_json_schema()}


def artifact_page(job_id, args):
    audit = read_artifact(job_id, args.artifact_id)
    text = ((job_root(job_id) / args.artifact_id / "input" / "script.py").read_text(encoding="utf-8")
            if args.section == "script" else json.dumps(audit, ensure_ascii=False))
    return {"artifact_id": args.artifact_id, "section": args.section, "text": text[args.offset:args.offset + 12000],
            "next_offset": args.offset + 12000 if len(text) > args.offset + 12000 else None}


def adapter_root():
    root = workspace_root() / "adapters"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_adapter(job_id, artifact_id, name):
    audit = read_artifact(job_id, artifact_id)
    if not audit.get("validation", {}).get("valid"):
        raise ValueError("只有通过独立校验的提取结果可以保存适配器")
    script = (job_root(job_id) / artifact_id / "input" / "script.py").read_text(encoding="utf-8")
    if hashlib.sha256(script.encode()).hexdigest() != audit["script_sha256"]:
        raise ValueError("归档脚本已变化，须重新执行验证后再保存适配器")
    identifier = hashlib.sha256((audit["script_sha256"] + audit["layout"]).encode()).hexdigest()
    path = adapter_root() / f"{identifier}.json"
    entry = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "adapter_id": identifier, "name": name, "layout": audit["layout"], "script": script,
        "script_sha256": audit["script_sha256"], "validated_sources": []}
    signature = hashlib.sha256(json.dumps(sorted(audit["file_hashes"].values())).encode()).hexdigest()
    entry["validated_sources"] = sorted(set([*entry["validated_sources"], signature]))
    entry["status"] = "verified" if len(entry["validated_sources"]) >= 2 else "candidate"
    write_json(path, entry)
    return {k: v for k, v in entry.items() if k != "script"}


def list_adapters():
    return [{k: entry[k] for k in ("adapter_id", "name", "status", "layout")}
            for path in sorted(adapter_root().glob("*.json"))[:100]
            for entry in [json.loads(path.read_text(encoding="utf-8"))]]


def run_adapter(job_id, files, args, **kwargs):
    if not re.fullmatch(r"[a-f0-9]{64}", args.adapter_id):
        raise ValueError("无效适配器")
    path = adapter_root() / f"{args.adapter_id}.json"
    if not path.is_file():
        raise ValueError("适配器不存在")
    entry = json.loads(path.read_text(encoding="utf-8"))
    supplied = kwargs.pop("texts", None)
    if any(not 1 <= p <= f.page_count for f in files for p in args.pages):
        raise ValueError("页码超出所选文件范围")
    texts = ({k: v for k, v in supplied.items() if k[0] in args.file_ids and k[1] in args.pages}
             if supplied is not None else {(f.id, p): pdf_page_text(f, p) for f in files for p in args.pages})
    if source_layout(files, texts) != entry["layout"]:
        raise ValueError("文件结构与适配器验证样本不同，请编写或调整脚本")
    if hashlib.sha256(entry["script"].encode()).hexdigest() != entry["script_sha256"]:
        raise ValueError("适配器脚本校验失败")
    result = run_script(job_id, files, FileScriptInput(
        file_ids=args.file_ids, pages=args.pages, script=entry["script"],
        purpose=f"复用适配器：{entry['name']}"), texts=texts, **kwargs)
    if result.get("validation", {}).get("valid"):
        save_adapter(job_id, result["artifact_id"], entry["name"])
    return result
