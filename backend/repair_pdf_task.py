"""Apply source-backed coverage checks to an explicitly selected existing PDF task."""
import argparse
import uuid

from sqlalchemy import select

from app.agent_loop import pending_quote
from app.customer_files import pdf_page_text
from app.database import SessionLocal
from app.models import AgentRun, CustomerFile, RequirementJob
from app.pdf_requirements import validate_rows
from app.requirements_schemas import ExtractionResult, ExtractedLine, RequirementLine
from app.services import log_change, snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", type=int)
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        job = db.scalar(select(RequirementJob).where(RequirementJob.id == args.job_id).with_for_update())
        if not job or job.status == "processing":
            raise ValueError("Task unavailable or still running")
        before = snapshot(job)
        result = ExtractionResult(lines=[ExtractedLine.model_validate({
            k: v for k, v in line.items() if k in ExtractedLine.model_fields})
            for line in job.requirements], questions=job.extraction.get("questions", []))
        texts = {(f.id, p): pdf_page_text(f, p) for f in db.scalars(
            select(CustomerFile).where(CustomerFile.job_id == job.id))
            if f.media_type == "application/pdf" for p in range(1, f.page_count + 1)}
        validate_rows(result, texts)
        job.requirements = [RequirementLine(id=uuid.uuid4().hex[:16], **line.model_dump()).model_dump(mode="json")
                            for line in result.lines]
        job.extraction = result.model_dump(mode="json")
        log_change(db, job, "source_repair", "逐页PDF原文校验，补回遗漏序号，标记尺寸及厚度冲突", before)
        run = AgentRun(job_id=job.id, model="source-validation")
        db.add(run)
        db.flush()
        pending_quote(db, job, run, "已按PDF原始序号补齐清单；缺少的数量、图示信息、尺寸冲突及组合成品计价方案需核实。未确认金额留空。")
        print("Repaired task", job.id, "lines", len(job.requirements))


if __name__ == "__main__":
    main()
