"""Revalidate a stopped task and append an audited reference-only quote version."""
import argparse

from sqlalchemy import select

from app.agent_loop import pending_quote
from app.customer_files import pdf_page_text
from app.database import SessionLocal
from app.models import AgentRun, CustomerFile, RequirementJob
from app.pdf_requirements import validate_rows
from app.requirements_schemas import ExtractionResult, ExtractedLine
from app.services import log_change, snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", type=int)
    parser.add_argument("--revision", type=int, required=True)
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        job = db.scalar(select(RequirementJob).where(
            RequirementJob.id == args.job_id).with_for_update())
        if not job or job.status == "processing" or job.revision != args.revision:
            raise ValueError("Task unavailable, processing, or changed")
        before = snapshot(job)
        extracted = ExtractionResult(lines=[ExtractedLine.model_validate({
            k: v for k, v in raw.items() if k in ExtractedLine.model_fields
        }) for raw in job.requirements])
        texts = {(f.id, p): pdf_page_text(f, p) for f in db.scalars(
            select(CustomerFile).where(CustomerFile.job_id == job.id))
            if f.media_type == "application/pdf" for p in range(1, f.page_count + 1)}
        validate_rows(extracted, texts)
        if len(extracted.lines) != len(job.requirements):
            raise ValueError("Coverage changed; requires full extraction review")
        # Preserve line IDs, approvals and selections. Only source-backed fields are refreshed.
        job.requirements = [{**raw, **line.model_dump(mode="json")}
                            for raw, line in zip(job.requirements, extracted.lines)]
        job.extraction = {**job.extraction, "lines": [l.model_dump(mode="json") for l in extracted.lines]}
        log_change(db, job, "source_repair", "重新校验原文尺寸并刷新部件材料候选；不补造数量或费用", before)
        run = AgentRun(job_id=job.id, model="source-validation")
        db.add(run)
        db.flush()
        pending_quote(db, job, run, "已逐项查询材料建议单价。组合产品仍需核实部件实际计价面积、"
                      "原文加工工序及对应费用；缺失数量和原文尺寸冲突保留，不以材料价代替成品报价。")
        print("Updated task", job.id, "revision", job.revision, "lines", len(job.requirements))


if __name__ == "__main__":
    main()
