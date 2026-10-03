from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, BeforeValidator, model_validator

Positive = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=2, allow_inf_nan=False)]
Target = Literal["item", "letter_group", "component"]
Strategy = Literal["auto", "letters", "area", "length", "item", "composite", "labor"]


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
    cell: str = Field(default="", max_length=50)
    source: Literal["image", "text"] = "text"


class QuantityEvidence(Evidence):
    quantity: PieceCount | None = None
    raw_quantity: str = Field(default="", max_length=100)
    validation_status: Literal["unchecked", "verified", "rejected"] = "unchecked"
    validation_note: str = Field(default="", max_length=500)


class SourceMeasurement(StrictModel):
    value: Annotated[Decimal, Field(gt=0, max_digits=15, decimal_places=6, allow_inf_nan=False)]
    unit: Literal["m", "m2"]
    evidence: list[Evidence] = Field(min_length=1, max_length=3)


class DimensionEvidence(Evidence):
    field: Literal["width_mm", "height_mm", "thickness_mm", "depth_mm"]
    value: Positive | None = Field(description="Single annotation value; null means unreadable, not a known dimension.")
    unit: Literal["mm", "cm", "m"] = "mm"
    source: Literal["image", "text"] = "image"
    target: Target = "item"
    target_index: int | None = Field(default=None, ge=0, le=49)
    role: Literal["overall", "character", "plate_thickness", "shell_depth", "spacing", "local", "unknown"] = "overall"

    @model_validator(mode="after")
    def target_contract(self):
        if (self.target == "item") != (self.target_index is None):
            raise ValueError("字组/部件尺寸必须指定target_index，整件尺寸不能指定索引")
        return self


class MaterialComponent(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    material: str = Field(default="", max_length=100)
    thickness_mm: Positive | None = None
    processes: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=10)
    width_mm: Positive | None = None
    height_mm: Positive | None = None
    billing_length_mm: Positive | None = None
    area_m2: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=6, allow_inf_nan=False)] | None = None
    labor_hours: Positive | None = None
    quantity_per_item: int | None = Field(default=None, gt=0, le=10000)
    geometry: Literal["flat_rectangle", "shaped", "shell", "unknown"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    uncertainties: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)


class LetterGroup(StrictModel):
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
    quantity_evidence: list[QuantityEvidence] = Field(default_factory=list, max_length=20)
    source_measurements: list[SourceMeasurement] = Field(default_factory=list, max_length=10)
    dimension_evidence: list[DimensionEvidence] = Field(default_factory=list, max_length=20)
    components: list[MaterialComponent] = Field(default_factory=list, max_length=20)
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


class ExtractionResult(StrictModel):
    summary: str = Field(default="", max_length=4000)
    lines: list[ExtractedLine] = Field(max_length=100)
    questions: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30)


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
                   "count", "quantity_per_item", "labor_hours", "geometry", "text_content"]
    value: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=2, max_length=1500)
    target: Target = "item"
    target_index: int | None = Field(default=None, ge=0, le=49)
    mode: Literal["fill", "correct"] = "fill"
    previous_value: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def target_contract(self):
        if (self.target == "item") != (self.target_index is None):
            raise ValueError("字组/部件补全必须指定target_index，整件补全不能指定索引")
        fields = {
            "item": {"quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                     "billing_length_mm", "material", "process", "language", "pricing_category",
                     "character_count", "area_m2", "power_count", "pricing_family", "labor_hours", "text_content"},
            "letter_group": {"width_mm", "height_mm", "billing_length_mm", "count", "text_content"},
            "component": {"width_mm", "height_mm", "billing_length_mm", "thickness_mm", "material",
                          "quantity_per_item", "area_m2", "labor_hours", "geometry"},
        }
        if self.field not in fields[self.target]:
            raise ValueError("补全字段不属于指定对象")
        if self.mode == "correct" and (not self.evidence or self.previous_value is None):
            raise ValueError("纠正识别值必须提供previous_value和原文evidence，不能伪装成普通补全")
        return self


class EstimateCost(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    rate: Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=6, allow_inf_nan=False)]
    rate_unit: Literal["unknown", "cm", "m", "m2", "piece", "set", "fixed", "ratio", "hour"] = "unknown"
    expected_amount: Money | None = None
    measurement_note: str = Field(default="", max_length=1500)
    quantity_formula: str = Field(default="=1", min_length=2, max_length=1000)
    basis: Literal["per_piece", "total"] = "per_piece"
    source: Literal["agent_estimate", "catalog", "historical"] = "agent_estimate"
    reference_id: int | None = Field(default=None, gt=0)
    reference_revision: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=2, max_length=1500)
    kind: Literal["other", "material", "process", "finished", "power", "bracket", "outsourcing",
                  "logistics", "transport", "installation", "invoice", "packing", "tax", "profit"] = "other"
    outsourcing_type: Literal["other", "adhesive", "cut_adhesive", "3m"] = "other"
    minimum_area_scope: Literal["unconfirmed", "per_piece", "order"] = "unconfirmed"
    target: Target = "item"
    target_index: int | None = Field(default=None, ge=0, le=49)
    included_kinds: list[Literal["material", "process", "power", "bracket", "outsourcing"]] = Field(
        default_factory=list, max_length=5)
    adjusts_cost_index: int | None = Field(default=None, ge=0, le=29)
    rate_basis_amount: Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=6, allow_inf_nan=False)] | None = None
    rate_basis_unit: Literal["cm", "m", "m2", "piece", "set", "fixed", "ratio", "hour"] | None = None

    @model_validator(mode="after")
    def target_contract(self):
        if (self.target == "item") != (self.target_index is None):
            raise ValueError("字组/部件费用必须指定target_index，整件费用不能指定索引")
        if (self.rate_basis_amount is None) != (self.rate_basis_unit is None):
            raise ValueError("单价依据金额和单位必须同时提供")
        if len(set(self.included_kinds)) != len(self.included_kinds):
            raise ValueError("已含费用类别不能重复")
        return self


class EstimatePlan(StrictModel):
    assumptions: list[EstimateAssumption] = Field(default_factory=list, max_length=20)
    costs: list[EstimateCost] = Field(default_factory=list, max_length=30)
    scope: str = Field(min_length=2, max_length=3000)
    contract_version: Literal[1, 2] = 1
    pricing_strategy: Strategy = "auto"


class RequirementLine(ExtractedLine):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    confirmed: bool = False
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
    lines: list[RequirementLine] = Field(max_length=100)
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
    terms: str | None = Field(default=None, min_length=2, max_length=4000)


class ModelKeyInput(StrictModel):
    api_key: str = Field(min_length=10, max_length=500)
