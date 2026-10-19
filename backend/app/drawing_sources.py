"""Discover drawing references without adopting their extracted measurements."""
from copy import deepcopy
from sqlalchemy import select

from .catalog_matching import lexical_score
from .models import CustomerFile
from .requirements_schemas import Evidence


def references(job):
    return (job.extraction or {}).get("source_agent", {}).get("reference_observations", {})


def drawing_candidates(db, job, raw):
    files = {f.id: f for f in db.scalars(
        select(CustomerFile).where(CustomerFile.job_id == job.id))}
    query = " ".join(raw.get(key) or "" for key in ("product", "source_code", "text_content"))
    ranked = []
    for key, observation in references(job).items():
        title = observation.get("product", "")
        code = observation.get("source_code", "")
        score = lexical_score(query, title, title + " " + code)
        if score <= 0:
            continue
        pages = sorted({(e["file_id"], e["page"]) for e in observation.get("evidence", [])
                        if e.get("file_id") in files
                        and files[e["file_id"]].media_type == "application/pdf"
                        and 1 <= e.get("page", 0) <= files[e["file_id"]].page_count})
        if pages:
            ranked.append((score, str(key), {
                "reference_id": str(key), "product": title, "source_code": code,
                "pages": [{"file_id": file_id, "page": page} for file_id, page in pages],
                "candidate_only": True,
            }))
    ranked.sort(key=lambda value: (-value[0], value[1]))
    return [entry for _, _, entry in ranked[:8]]


def bind_drawing(db, job, run, args):
    active_id = run.usage.get("active_line_id")
    if active_id and active_id != args.line_id:
        raise ValueError("只能绑定当前报价行的图纸来源")
    raw = next((row for row in job.requirements if row["id"] == args.line_id), None)
    observation = references(job).get(args.reference_id)
    if raw is None or observation is None:
        raise ValueError("需求行或图纸参考不存在")
    file = db.get(CustomerFile, args.file_id)
    if (not file or file.job_id != job.id or file.media_type != "application/pdf"
            or not 1 <= args.page <= file.page_count
            or not any(e.get("file_id") == file.id and e.get("page") == args.page
                       for e in observation.get("evidence", []))):
        raise ValueError("所选页面不属于本任务的该图纸参考")
    # A reference match alone is not evidence: require a successful image read for this line.
    viewed = next((step for step in reversed(run.steps)
                   if step.get("tool") == "view_source_region"
                   and (step.get("line_id") or step.get("arguments", {}).get("line_id")) == args.line_id
                   and step.get("arguments", {}).get("file_id") == file.id
                   and step.get("arguments", {}).get("page") == args.page
                   and step.get("result", {}).get("images_follow") is True
                   and step.get("result", {}).get("read_id")
                   and not step.get("result", {}).get("error")), None)
    if viewed is None:
        raise ValueError("先用view_source_region查看本行对应的实际PDF原图，再说明对应关系绑定；旧提取不算核图")
    evidence = Evidence(file_id=file.id, page=args.page, source="image",
                        region=viewed["result"]["region"], text="已查看并关联的图纸原图")
    anchor = evidence.model_dump(mode="json")
    if anchor not in raw.get("evidence", []):
        from . import agent_work_state as work
        state = deepcopy(work.line_state(job, run, args.line_id))
        job.requirements = [{**row, "evidence": [*row.get("evidence", []), anchor]}
                            if row["id"] == args.line_id else row for row in job.requirements]
        updated = next(row for row in job.requirements if row["id"] == args.line_id)
        if state.get("pricing_proposal"):
            state["pricing_proposal"].update(
                input_fingerprint=work.fingerprint(updated), requires_revalidation=True)
        state.update(validated_plan=None, preview_passed=False)
        work.update_state(job, run, args.line_id, **state)
        from .services import log_change
        log_change(db, job, "agent_drawing_source",
                   f'{raw["product"]}关联图纸#{file.id}第{args.page}页：{args.reason}')
    from .pricing_proposals import prepare_packet
    updated = next(row for row in job.requirements if row["id"] == args.line_id)
    return {"line_id": args.line_id, "bound": True, "evidence": anchor,
            "pricing_packet": prepare_packet(db, job, updated),
            "notice": "仅新增已查看区域的来源；数量、尺寸、材料、人工修改及旧方案均保留。"
                      "从原图核对所需参数后，使用新source_ids提交方案；旧观察值不能自动采用。"}
