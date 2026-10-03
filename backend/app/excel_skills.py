"""AI-selected Excel parsing guides; generated guides are data, never executable code."""
import hashlib
import json
import re
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field
from openpyxl.utils import coordinate_to_tuple

from .config import settings
from .requirements_schemas import StrictModel

ROOT = Path(__file__).resolve().parents[1] / "skills"
COMMON = "excel-source-contract"
MAX_GENERATED = 32
MAX_CREATIONS_PER_FILE = 2
CREATE_LOCK = threading.Lock()
Column = Literal["item", "product", "quantity", "unit", "type_note", "difficulty",
                 "material", "process", "dimensions", "notes"]
Layout = Literal["row_table", "multi_level_header", "transposed", "matrix", "multi_block", "mixed"]


class SkillMetadata(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str = Field(min_length=5, max_length=300)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    guidance: str
    origin: str
    fingerprint: str


class SheetPlan(StrictModel):
    sheet: str = Field(min_length=1, max_length=100)
    skill: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    layout: Layout = "row_table"
    purpose: Literal["customer_requirements", "price_reference", "historical_quote"] = "customer_requirements"
    header_row: int | None = Field(default=None, ge=1, le=2000)
    columns: dict[Column, str] = Field(default_factory=dict, max_length=10)
    reason: str = Field(min_length=2, max_length=500)
    skill_origin: Literal["builtin", "model"] = "builtin"
    skill_revision: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    created: bool = False


class WorkbookPlan(StrictModel):
    sheets: list[SheetPlan] = Field(min_length=1, max_length=100)


class GeneratedSkill(StrictModel):
    description: str = Field(min_length=5, max_length=300)
    guidance: str = Field(min_length=30, max_length=4000)


def generated_root():
    return settings().storage_dir.parent / "excel-skills"


def read_skill(path, origin):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("Skill缺少frontmatter")
    header, guidance = text[4:].split("\n---\n", 1)
    metadata = SkillMetadata.model_validate(json.loads(header))
    if not guidance.strip() or len(guidance) > 6000 or metadata.name != path.parent.name:
        raise ValueError("Skill名称或正文无效")
    return Skill(metadata.name, metadata.description, guidance.strip(), origin,
                 hashlib.sha256(text.encode()).hexdigest())


def library():
    result = {skill.name: skill for skill in
              (read_skill(path, "builtin") for path in sorted(ROOT.glob("*/SKILL.md")))}
    directory = generated_root()
    for path in sorted(directory.glob("excel-generated-*/SKILL.md"))[:MAX_GENERATED]:
        try:
            skill = read_skill(path, "model")
        except (ValueError, OSError):
            continue
        if re.fullmatch(r"excel-generated-[0-9a-f]{16}", skill.name):
            result[skill.name] = skill
    return result


def library_fingerprint():
    # New generated guides don't invalidate already saved facts; built-in changes do.
    return hashlib.sha256("".join(skill.fingerprint for skill in library().values()
                                   if skill.origin == "builtin").encode()).hexdigest()


def workbook_profile(pages):
    profiles = []
    for sheet in dict.fromkeys(page["sheet"] for page in pages):
        selected = [page for page in pages if page["sheet"] == sheet]
        rows = [row for page in selected for row in page["rows"]]
        profiles.append({"sheet": sheet, "page_count": len(selected),
                         "sample_rows": rows[:12],
                         "row_count": len(rows),
                         "image_count": sum(len(page["images"]) for page in selected),
                         "merged_cells": selected[0].get("merged_cells", [])[:40]})
    return profiles


def validate_plan(plan, pages, skills):
    sheets = {page["sheet"] for page in pages}
    if {item.sheet for item in plan.sheets} != sheets or len(plan.sheets) != len(sheets):
        raise ValueError("Excel解析规划必须逐一覆盖实际工作表且不能重复")
    for item in plan.sheets:
        if item.skill not in skills or item.skill == COMMON:
            if item.skill != "create":
                raise ValueError("Excel解析Skill不存在")
        cells = {cell["cell"]: cell for page in pages if page["sheet"] == item.sheet
                 for row in page["rows"] for cell in row["cells"]}
        if item.columns and item.header_row is None:
            raise ValueError("列映射必须有实际表头行")
        for address in set(item.columns.values()):
            fields = {field for field, cell in item.columns.items() if cell == address}
            if len(fields) > 1 and not fields <= {"material", "process"}:
                raise ValueError("除明确的材质工艺合并列外，不能把同一列作为不同字段")
        for address in item.columns.values():
            if not re.fullmatch(r"[A-Z]{1,2}[1-9][0-9]{0,3}", address) or address not in cells:
                raise ValueError("Skill列映射引用了不存在的单元格")
            if coordinate_to_tuple(address)[0] != item.header_row or cells[address].get("formula"):
                raise ValueError("Skill列映射不是实际表头单元格")
    return plan


def structural_profile(plan, pages):
    """Generation receives geometry and semantic fields, not customer cell values."""
    rows = [row for page in pages if page["sheet"] == plan.sheet for row in page["rows"]]
    return {"layout": plan.layout, "purpose": plan.purpose, "semantic_columns": plan.columns,
            "header_row": plan.header_row, "row_count": len(rows),
            "populated_columns": sorted({re.sub(r"\d", "", cell["cell"])
                                         for row in rows for cell in row["cells"]}),
            "images_present": any(page["images"] for page in pages if page["sheet"] == plan.sheet)}


def save_generated(spec, structure):
    with CREATE_LOCK:
        return _save_generated(spec, structure)


def _save_generated(spec, structure):
    # An opaque deterministic name prevents arbitrary paths and duplicate layout files.
    name = generated_name(structure)
    root = generated_root().resolve()
    directory = (root / name).resolve()
    if directory.parent != root:
        raise ValueError("Skill保存位置无效")
    path = directory / "SKILL.md"
    if path.exists():
        return read_skill(path, "model")
    if len(list(root.glob("excel-generated-*/SKILL.md"))) >= MAX_GENERATED:
        raise ValueError("自动创建Skill已达到32个，请整理现有Skill后继续")
    directory.mkdir(parents=True, exist_ok=True)
    metadata = SkillMetadata(name=name, description=spec.description)
    text = "---\n" + json.dumps(metadata.model_dump(), ensure_ascii=False) + "\n---\n\n" + spec.guidance + "\n"
    temp = directory / (uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return read_skill(path, "model")


def generated_name(structure):
    reusable = {"layout": structure["layout"], "purpose": structure["purpose"],
                "semantic_fields": sorted(structure["semantic_columns"]),
                "column_count": len(structure["populated_columns"]), "images_present": structure["images_present"]}
    identity = hashlib.sha256(json.dumps(reusable, sort_keys=True).encode()).hexdigest()[:16]
    return "excel-generated-" + identity


def apply_plan(source, plan):
    sheet_plan = next(item for item in plan.sheets if item.sheet == source["sheet"])
    return {**source, "analysis_plan": sheet_plan.model_dump(mode="json")}
