from decimal import Decimal
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, BeforeValidator, model_validator
from pydantic_core import PydanticCustomError
from .measurement_units import EngineeringUnit
from .process_taxonomy import ProcessCode
from .pricing_contract import MAX_PARAMETER_BLOCKS, MAX_PARAMETER_FIELDS, MAX_COSTS

Positive = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=2, allow_inf_nan=False)]
Target = Literal["item", "letter_group", "component"]
PARAMETER_FIELDS = {
    "item": {"quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
             "billing_length_mm", "material", "process", "language", "pricing_category",
             "character_count", "area_m2", "power_count", "pricing_family", "labor_hours", "text_content"},
    "letter_group": {"width_mm", "height_mm", "billing_length_mm", "count", "text_content"},
    "component": {"width_mm", "height_mm", "depth_mm", "billing_length_mm", "thickness_mm", "material",
                  "quantity_per_item", "area_m2", "labor_hours", "geometry", "area_basis"},
}
Strategy = Literal["auto", "letters", "area", "length", "item", "composite", "labor"]
DocumentRole = Literal["requirements", "drawing", "price_reference", "unknown"]
Region = tuple[
    Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)],
    Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)]
]


def reject_boolean_count(value):
    if isinstance(value, bool):
        raise ValueError("成品数量不能是布尔值")
    return value


PieceCount = Annotated[int, BeforeValidator(reject_boolean_count), Field(gt=0, le=1000000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1)
    text: str = Field(max_length=1500)
    sheet: str = Field(default="", max_length=100)
    cell: str = Field(default="", max_length=50,
        description="One actual Excel cell address, such as C13; use separate evidence entries for multiple cells.")
    source_range: str = Field(default="", max_length=50,
        description="Original same-row range, retained by the source adapter when splitting a range into cells.")
    source: Literal["image", "text"] = "text"
    region: tuple[
        Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)],
        Annotated[float, Field(ge=0, le=1)], Annotated[float, Field(ge=0, le=1)]
    ] | None = None


class QuantityEvidence(Evidence):
    quantity: PieceCount | None = None
    raw_quantity: str = Field(default="", max_length=100)
    validation_status: Literal["unchecked", "verified", "rejected"] = "unchecked"
    validation_note: str = Field(default="", max_length=500)


class SourceMeasurement(StrictModel):
    value: Annotated[Decimal, Field(gt=0, max_digits=15, decimal_places=6, allow_inf_nan=False)]
    unit: EngineeringUnit
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


class DimensionEvidence(Evidence):
    field: Literal["width_mm", "height_mm", "thickness_mm", "depth_mm"]
    value: Positive | None = Field(description="Single annotation value; null means unreadable, not a known dimension.")
    unit: Literal["mm", "cm", "m"] = "mm"
    source: Literal["image", "text"] = "image"
    target: Target = "item"
    target_id: str | None = Field(default=None, max_length=50)
    target_index: int | None = Field(default=None, ge=0, le=49,
        description="Must be null for target=item; otherwise the zero-based index in letter_groups or components.")
    role: Literal["overall", "character", "plate_thickness", "shell_depth", "spacing", "local", "unknown"] = "overall"

    @model_validator(mode="after")
    def target_contract(self):
        if (self.target == "item" and (self.target_index is not None or self.target_id is not None)
                or self.target != "item" and self.target_index is None and not self.target_id):
            raise PydanticCustomError("dimension_target_index",
                "字组/部件尺寸必须指定target_index，整件尺寸不能指定索引")
        return self


class MaterialComponent(StrictModel):
    object_id: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,50}$")
    name: str = Field(min_length=1, max_length=100)
    material: str = Field(default="", max_length=100)
    thickness_mm: Positive | None = None
    processes: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=10)
    width_mm: Positive | None = None
    height_mm: Positive | None = None
    depth_mm: Positive | None = None
    billing_length_mm: Positive | None = None
    area_m2: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6, allow_inf_nan=False)] | None = None
    labor_hours: Positive | None = None
    quantity_per_item: int | None = Field(default=None, gt=0, le=10000)
    geometry: Literal["flat_rectangle", "shaped", "shell", "unknown"] = "unknown"
    area_basis: Literal["unknown", "rectangle", "net", "developed"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    uncertainties: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)


class ProductionRequirement(StrictModel):
    target: Literal["item", "component"] = "item"
    target_id: str | None = Field(default=None, min_length=1, max_length=50)
    process_code: ProcessCode
    evidence: list[Evidence] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def scope_contract(self):
        if (self.target == "item") != (self.target_id is None):
            raise ValueError("整件工序不填target_id，部件工序必须指定实际对象编号")
        return self


class LetterGroup(StrictModel):
    object_id: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,50}$")
    label: str = Field(default="", max_length=100)
    count: int = Field(gt=0, le=10000)
    width_mm: Positive | None = None
    height_mm: Positive | None = None
    billing_length_mm: Positive | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    text_content: str = Field(default="", max_length=1000)
    count_basis: Literal["characters", "repeated", "unknown"] = "unknown"

    @model_validator(mode="after")
    def character_count_contract(self):
        if self.count_basis == "characters":
            count = len("".join(self.text_content.split()))
            if not count or self.count != count:
                raise ValueError("字组count必须等于text_content的非空白字符数，不能把单词数当字母数")
        return self


class ExtractedLine(StrictModel):
    source_item: str = Field(default="", max_length=100)
    source_code: str = Field(default="", max_length=100)
    document_role: DocumentRole = "unknown"
    measurement_version: Literal[2, 3] = 2
    depth_mm: Positive | None = None
    product: str = Field(default="", max_length=150)
    pricing_category: str = Field(default="", max_length=150)
    category_basis: str = Field(default="", max_length=1500)
    text_content: str = Field(default="", max_length=1000)
    material: str = Field(default="", max_length=100)
    thickness_mm: Positive | None = None
    width_mm: Positive | None = None
    height_mm: Positive | None = None
    billing_length_mm: Positive | None = None
    quantity: PieceCount | None = None
    quantity_unit: str = Field(default="", max_length=20)
    quantity_evidence: list[QuantityEvidence] = Field(default_factory=list, max_length=20)
    source_measurements: list[SourceMeasurement] = Field(default_factory=list, max_length=10)
    dimension_evidence: list[DimensionEvidence] = Field(default_factory=list, max_length=20)
    components: list[MaterialComponent] = Field(default_factory=list, max_length=20)
    production_requirements: list[ProductionRequirement] = Field(default_factory=list, max_length=50)
    language: Literal["zh", "en", "all", "unknown"] = "unknown"
    process: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=4000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    text_evidence: list[Annotated[str, Field(min_length=1, max_length=1500)]] = Field(default_factory=list, max_length=20)
    uncertainties: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)
    pricing_family: Literal["letter", "material", "unknown"] = "unknown"
    type_note: str = Field(default="", max_length=255)
    difficulty: Literal["A", "B", "C", "unknown"] = "unknown"
    difficulty_note: str = Field(default="", max_length=500)
    character_count: int | None = Field(default=None, gt=0, le=10000)
    letter_groups: list[LetterGroup] = Field(default_factory=list, max_length=50)
    area_m2: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6, allow_inf_nan=False)] | None = None
    faces: Literal["single", "double", "unknown"] = "unknown"
    lighting: Literal["yes", "no", "unknown"] = "unknown"
    installation_scene: Literal["rooftop", "other", "unknown"] = "unknown"
    labor_hours: Positive | None = None


    @model_validator(mode="after")
    def stable_objects(self):
        for kind in ("components", "letter_groups"):
            objects = getattr(self, kind)
            for index, obj in enumerate(objects):
                if not obj.object_id:
                    identity = [kind, index, getattr(obj, "name", getattr(obj, "label", "")),
                                [(e.file_id, e.page, e.sheet, e.cell) for e in obj.evidence]]
                    obj.object_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
            if len({obj.object_id for obj in objects}) != len(objects):
                raise ValueError("同一项目的部件或字组object_id不能重复")
        for evidence in self.dimension_evidence:
            if evidence.target == "item":
                continue
            objects = self.components if evidence.target == "component" else self.letter_groups
            if evidence.target_id:
                index = next((i for i, obj in enumerate(objects) if obj.object_id == evidence.target_id), None)
                if index is None or evidence.target_index is not None and evidence.target_index != index:
                    raise ValueError("尺寸证据对象编号或索引已变化，须核对原对象")
                evidence.target_index = index
            elif evidence.target_index is not None and evidence.target_index < len(objects):
                evidence.target_id = objects[evidence.target_index].object_id
        component_ids = {obj.object_id for obj in self.components}
        if any(req.target == "component" and req.target_id not in component_ids
               for req in self.production_requirements):
            raise ValueError("工艺要求必须绑定当前项目的实际部件")
        return self


class SourceCoverage(StrictModel):
    file_id: int
    page: int
    sheet: str = ""
    row: int
    status: Literal["extracted", "header", "non_product", "unresolved"]
    reason: str = ""
    region: Region | None = None
    kind: Literal["row", "page", "region", "item", "view", "note", "unreadable", "empty"] = "row"
    label: str = Field(default="", max_length=1500)
    source_item: str = Field(default="", max_length=100)
    document_role: DocumentRole = "unknown"
    source_code: str = Field(default="", max_length=100)
    verification: Literal["native", "visual", "ai", "unverified"] = "unverified"
    expected_items: int | None = Field(default=None, ge=0, le=10000)
    extracted_items: int | None = Field(default=None, ge=0, le=10000)


class SourceLink(StrictModel):
    file_ids: list[int] = Field(min_length=1, max_length=6)
    product: str = Field(default="", max_length=150)
    source_code: str = Field(default="", max_length=100)
    status: Literal["merged", "unresolved", "reference"]
    reason: str = Field(max_length=500)


class ExtractionResult(StrictModel):
    summary: str = Field(default="", max_length=4000)
    lines: list[ExtractedLine] = Field(max_length=10000)
    questions: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30)
    source_coverage: list[SourceCoverage] = Field(default_factory=list, max_length=50000)
    source_links: list[SourceLink] = Field(default_factory=list, max_length=10000)
    source_agent: dict | None = None


class ExtraCharge(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    amount: Money
    basis: Literal["per_piece", "total"] = "total"
    reason: str = Field(min_length=2, max_length=500)
    kind: Literal["other", "material", "process", "finished", "power", "bracket", "outsourcing",
                  "logistics", "transport", "installation", "invoice", "packing", "tax", "profit"] = "other"


class EstimateAssumption(StrictModel):
    field: Literal["quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                   "billing_length_mm", "material", "process", "language", "pricing_category",
                   "character_count", "area_m2", "power_count", "pricing_family",
                   "count", "quantity_per_item", "labor_hours", "geometry", "area_basis", "text_content"]
    value: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=2, max_length=1500)
    target: Target = "item"
    target_id: str | None = Field(default=None, max_length=50)
    target_index: int | None = Field(default=None, ge=0, le=49)
    mode: Literal["fill", "correct"] = "fill"
    previous_value: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    basis: Literal["source", "derived", "assumption"] = "assumption"

    @model_validator(mode="after")
    def target_contract(self):
        if (self.target == "item" and (self.target_index is not None or self.target_id is not None)
                or self.target != "item" and self.target_index is None and not self.target_id):
            raise ValueError("字组/部件补全必须指定target_index，整件补全不能指定索引")
        if self.field not in PARAMETER_FIELDS[self.target]:
            raise ValueError("补全字段不属于指定对象")
        if self.mode == "correct" and (not self.evidence or self.previous_value is None):
            raise ValueError("纠正识别值必须提供previous_value和原文evidence，不能伪装成普通补全")
        return self


class EstimateCost(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    rate: Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=6, allow_inf_nan=False)]
    rate_unit: Literal["unknown", "cm", "m", "m2", "m3", "kg", "piece", "set", "fixed", "ratio", "hour"] = "unknown"
    expected_amount: Money | None = None
    measurement_note: str = Field(default="", max_length=1500)
    quantity_formula: str = Field(default="=1", min_length=2, max_length=1000)
    calculation_script: str = Field(default="", max_length=6000,
        description="Bounded Python arithmetic: assignments, comparisons, conditional expressions, assert, min/max/abs/ceil/floor. Assign measurement to result, not money. Inputs use live measurement variables; no imports/loops/I/O.")
    basis: Literal["per_piece", "total"] = "per_piece"
    source: Literal["agent_estimate", "catalog", "historical"] = "agent_estimate"
    reference_id: int | None = Field(default=None, gt=0)
    reference_revision: int | None = Field(default=None, gt=0)
    reference_part: Literal["material_price", "process_1_price", "process_2_price"] | None = None
    reason: str = Field(min_length=2, max_length=1500)
    kind: Literal["other", "material", "process", "finished", "power", "bracket", "outsourcing",
                  "logistics", "transport", "installation", "invoice", "packing", "tax", "profit"] = "other"
    process_codes: list[ProcessCode] = Field(default_factory=list, max_length=20)
    outsourcing_type: Literal["other", "adhesive", "cut_adhesive", "3m"] = "other"
    minimum_area_scope: Literal["unconfirmed", "per_piece", "order"] = "unconfirmed"
    target: Target = "item"
    target_id: str | None = Field(default=None, max_length=50)
    measurement: Literal["auto", "item_area", "letter_area", "letter_length", "item_length",
                         "component_area", "component_count", "item_count", "labor_hours",
                         "power_units", "subtotal_ratio", "fixed", "script", "source_quantity"] = "auto"
    source_measurement_index: int | None = Field(default=None, ge=0, le=9)
    target_index: int | None = Field(default=None, ge=0, le=49)
    included_kinds: list[Literal["material", "process", "power", "bracket", "outsourcing"]] = Field(
        default_factory=list, max_length=5)
    covers_component_ids: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=50)
    adjusts_cost_index: int | None = Field(default=None, ge=0, le=29)
    rate_basis_amount: Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=6, allow_inf_nan=False)] | None = None
    rate_basis_unit: Literal["cm", "m", "m2", "m3", "kg", "piece", "set", "fixed", "ratio", "hour"] | None = None

    @model_validator(mode="after")
    def target_contract(self):
        if self.source_measurement_index is not None and self.measurement != "source_quantity":
            raise ValueError("source_measurement_index仅用于原表工程量计价")
        if self.reference_part and (self.source != "catalog" or self.kind != (
                "material" if self.reference_part == "material_price" else "process")):
            raise ValueError("成本分项须引用报价库，并分别标记为材料或加工类别")
        if bool(self.calculation_script.strip()) != (self.measurement == "script"):
            raise ValueError("临时脚本须使用measurement=script，且脚本不能为空")
        if self.calculation_script:
            from .calculation_scripts import parse_script
            _, required = parse_script(self.calculation_script)
            if not required:
                raise ValueError("工程量脚本须引用当前项目参数，不能硬编码结果")
            if not self.measurement_note.strip() or self.rate_unit == "unknown":
                raise ValueError("脚本须注明工程量推导依据和输出单位rate_unit")
            if self.basis == "per_piece" and required & {"quantity", "order_subtotal", "fixed_subtotal"}:
                raise ValueError("每件脚本不能包含订购数量或整单小计")
        if (self.target == "item" and (self.target_index is not None or self.target_id is not None)
                or self.target != "item" and self.target_index is None and not self.target_id):
            raise ValueError("字组/部件费用必须指定target_index，整件费用不能指定索引")
        if (self.rate_basis_amount is None) != (self.rate_basis_unit is None):
            raise ValueError("单价依据金额和单位必须同时提供")
        if len(set(self.included_kinds)) != len(self.included_kinds):
            raise ValueError("已含费用类别不能重复")
        if len(set(self.covers_component_ids)) != len(self.covers_component_ids):
            raise ValueError("成品费用覆盖的部件编号不能重复")
        return self


class EstimatePlan(StrictModel):
    assumptions: list[EstimateAssumption] = Field(
        default_factory=list, max_length=MAX_PARAMETER_BLOCKS * MAX_PARAMETER_FIELDS)
    costs: list[EstimateCost] = Field(default_factory=list, max_length=MAX_COSTS)
    scope: str = Field(min_length=2, max_length=3000)
    contract_version: Literal[1, 2, 3] = 1
    pricing_strategy: Strategy = "auto"
    proposed_components: list[MaterialComponent] = Field(default_factory=list, max_length=50)
    proposed_letter_groups: list[LetterGroup] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def proposed_objects_contract(self):
        objects = [*self.proposed_components, *self.proposed_letter_groups]
        if objects and self.contract_version < 3:
            raise ValueError("新增待审核对象需使用新计价契约")
        if any(not obj.object_id or not obj.evidence for obj in objects):
            raise ValueError("新增待审核对象必须有明确object_id及原始证据")
        if len({obj.object_id for obj in objects}) != len(objects):
            raise ValueError("新增待审核对象编号不能重复")
        return self


class RequirementLine(ExtractedLine):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    confirmed: bool = False
    rate_evidence_required: bool = False
    billing_length_mm: Positive | None = None
    selected_price_id: int | None = Field(default=None, gt=0)
    selected_price_revision: int | None = Field(default=None, gt=0)
    price_review_note: str = Field(default="", max_length=2000)
    extras: list[ExtraCharge] = Field(default_factory=list, max_length=20)
    manual_unit_price: Money | None = None
    manual_price_note: str = Field(default="", max_length=2000)
    estimate: EstimatePlan | None = None
    business_rules_version: Literal["legacy", "2026-09-23.v1"] = "legacy"
    length_rounding: Literal["exact", "ceil_cm", "nearest_cm"] = "exact"
    power_mode: Literal["manual", "per_letter_ceiling"] = "manual"
    power_count: int | None = Field(default=None, ge=0, le=1000000)
    power_adjustment: int = Field(default=0, ge=-10000, le=10000)
    power_note: str = Field(default="", max_length=1500)


class RequirementsUpdate(StrictModel):
    revision: int = Field(gt=0)
    lines: list[RequirementLine] = Field(max_length=10000)
    reason: str = Field(min_length=2, max_length=2000)


class RunInput(StrictModel):
    revision: int = Field(gt=0)
    supplementary_text: str = Field(default="", max_length=8000)
    allow_external_processing: bool = False
    replace_existing: bool = False
    intent: Literal["auto", "resume", "extract"] = "auto"


class QuoteInput(StrictModel):
    revision: int = Field(gt=0)
    terms: str = Field(min_length=2, max_length=4000)
    rounding: Literal["none", "yuan", "ten", "hundred"] = "none"


class PricingPolicyInput(StrictModel):
    revision: int = Field(gt=0)
    reason: str = Field(min_length=2, max_length=2000)


class ApprovalInput(StrictModel):
    note: str = Field(min_length=2, max_length=2000)
    confirmed_commercial_terms: bool = False
    confirmed_estimates: bool = False
    confirmed_source_review: bool = False
    terms: str | None = Field(default=None, min_length=2, max_length=4000)


class ModelKeyInput(StrictModel):
    api_key: str = Field(min_length=10, max_length=500)
