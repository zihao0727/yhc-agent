from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Positive = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=2, allow_inf_nan=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1, le=20)
    text: str = Field(max_length=1500)


class QuantityEvidence(Evidence):
    quantity: int = Field(gt=0, le=1000000)


class DimensionEvidence(Evidence):
    field: Literal["width_mm", "height_mm", "thickness_mm", "depth_mm"]
    value: Positive
    unit: Literal["mm", "cm", "m"] = "mm"
    source: Literal["image", "text"] = "image"


class MaterialComponent(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    material: str = Field(default="", max_length=100)
    thickness_mm: Positive | None = None
    processes: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=10)
    width_mm: Positive | None = None
    height_mm: Positive | None = None
    quantity_per_item: int | None = Field(default=None, gt=0, le=10000)
    geometry: Literal["flat_rectangle", "shaped", "shell", "unknown"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)
    uncertainties: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)


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
    quantity: int | None = Field(default=None, gt=0, le=1000000)
    quantity_evidence: list[QuantityEvidence] = Field(default_factory=list, max_length=20)
    dimension_evidence: list[DimensionEvidence] = Field(default_factory=list, max_length=20)
    components: list[MaterialComponent] = Field(default_factory=list, max_length=20)
    language: Literal["zh", "en", "all", "unknown"] = "unknown"
    process: str = Field(default="", max_length=255)
    notes: str = Field(default="", max_length=4000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    text_evidence: list[Annotated[str, Field(min_length=1, max_length=1500)]] = Field(default_factory=list, max_length=20)
    uncertainties: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=20)


class ExtractionResult(StrictModel):
    summary: str = Field(default="", max_length=4000)
    lines: list[ExtractedLine] = Field(max_length=100)
    questions: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list, max_length=30)


class ExtraCharge(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    amount: Money
    basis: Literal["per_piece", "total"] = "total"
    reason: str = Field(min_length=2, max_length=500)


class EstimateAssumption(StrictModel):
    field: Literal["quantity", "width_mm", "height_mm", "thickness_mm", "depth_mm",
                   "billing_length_mm", "material", "process", "language", "pricing_category"]
    value: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=2, max_length=1500)


class EstimateCost(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    rate: Annotated[Decimal, Field(ge=0, max_digits=15, decimal_places=6, allow_inf_nan=False)]
    quantity_formula: str = Field(default="=1", min_length=2, max_length=1000)
    basis: Literal["per_piece", "total"] = "per_piece"
    source: Literal["agent_estimate", "catalog", "historical"] = "agent_estimate"
    reference_id: int | None = Field(default=None, gt=0)
    reference_revision: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=2, max_length=1500)


class EstimatePlan(StrictModel):
    assumptions: list[EstimateAssumption] = Field(default_factory=list, max_length=20)
    costs: list[EstimateCost] = Field(default_factory=list, max_length=30)
    scope: str = Field(min_length=2, max_length=3000)


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


class ApprovalInput(StrictModel):
    note: str = Field(min_length=2, max_length=2000)
    confirmed_commercial_terms: bool = False
    confirmed_estimates: bool = False
    terms: str | None = Field(default=None, min_length=2, max_length=4000)


class ModelKeyInput(StrictModel):
    api_key: str = Field(min_length=10, max_length=500)
