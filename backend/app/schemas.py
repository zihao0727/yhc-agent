from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Status = Literal["draft", "active", "inactive"]
Unit = Literal["cm", "m", "m2", "piece", "set", "unknown"]
PriceKind = Literal["standard", "suggested", "starting", "reference"]


class PriceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category: str = Field(min_length=1, max_length=100)
    product: str = Field(min_length=1, max_length=150)
    material: str = Field(default="", max_length=100)
    thickness_mm: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=3)
    spec: str = Field(default="", max_length=255)
    language: Literal["zh", "en", "all", "unknown"] = "all"
    process: str = Field(default="", max_length=255)
    quality: str = Field(default="", max_length=100)
    unit: Unit = "unknown"
    amount: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=6)
    price_kind: PriceKind = "standard"
    status: Status = "active"
    notes: str = Field(default="", max_length=10000)
    review_reason: str = Field(min_length=2, max_length=2000)
    revision: int | None = Field(default=None, ge=1)

    @field_validator("amount", "thickness_mm")
    @classmethod
    def finite(cls, value):
        if value is not None and not value.is_finite():
            raise ValueError("数值必须有限")
        return value


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    product_id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=20000)
    rule_type: Literal["reference", "surcharge", "constraint", "formula", "case"] = "reference"
    status: Status = "draft"
    reason: str = Field(min_length=2, max_length=2000)
    revision: int | None = Field(default=None, ge=1)


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=2000)
