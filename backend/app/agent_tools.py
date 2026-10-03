"""Tool definitions and schemas for the agent loop."""
from typing import Literal
from pydantic import Field
from .requirements_schemas import StrictModel, EstimatePlan


class SearchInput(StrictModel):
    query: str = Field(default="", max_length=150)
    material: str = Field(default="", max_length=100)
    status: Literal["active", "draft", "all"] = "active"
    offset: int = Field(default=0, ge=0, le=10000)


class SemanticSearchInput(SearchInput):
    query: str = Field(min_length=1, max_length=1000)


class PriceInput(StrictModel):
    line_id: str = Field(max_length=50)
    price_id: int = Field(gt=0)
    revision: int = Field(gt=0)


class SelectInput(PriceInput):
    reason: str = Field(min_length=2, max_length=1000)


class AskInput(StrictModel):
    question: str = Field(min_length=2, max_length=2000)


class DeferInput(StrictModel):
    line_id: str = Field(min_length=1, max_length=50)
    cause: Literal["customer_input", "catalog_gap", "execution_error"]
    reason: str = Field(min_length=2, max_length=2000)
    question: str = Field(default="", max_length=2000)
    missing_fields: list[str] = Field(default_factory=list, max_length=20)


class EmptyInput(StrictModel):
    pass


class ComponentInput(StrictModel):
    line_id: str = Field(max_length=50)


class CaseSearchInput(StrictModel):
    query: str = Field(default="", max_length=150)
    offset: int = Field(default=0, ge=0, le=10000)


class CaseInput(StrictModel):
    case_id: int = Field(gt=0)


class PendingInput(StrictModel):
    reason: str = Field(min_length=2, max_length=2000)


class CompleteEstimateInput(StrictModel):
    line_id: str = Field(max_length=50)
    estimate: EstimatePlan


TOOL_SPECS = {
    "read_requirement": (ComponentInput, "读取需求行完整原始依据及已保存的估算方案，用于自主复核或调整；读取不会修改数据。"),
    "complete_estimate": (CompleteEstimateInput, "按用户需求补全暂定参数及成品费用方案，保留原始需求，逐项列明估算依据供审核。程序试算并保存可用方案；禁止伪称已确认或已批准。"),
    "defer_requirement": (DeferInput, "关键条件无法合理暂估时，记录具体缺项、不能暂估的原因和需客户回答的问题，暂挂本项并继续其他项目。cause区分客户信息缺失、无价格依据、程序执行问题；不生成金额，不伪称报价完成。"),
    "search_pricing_cases": (CaseSearchInput, "检索历史人工报价的组合公式、原工程量、工艺变更和来源，仅参考，不当作通用单价。"),
    "evaluate_pricing_case": (CaseInput, "程序复算历史人工报价公式，空白费用不当作零；返回未确认参数和方案变更，不套用当前需求。"),
    "analyze_components": (ComponentInput, "查询各部件材料建议单价及来源；尺寸、用量和唯一适用单价明确时返回每件成品对应的部件成本参考。不等于成品报价，不能纳入合计。"),
    "finish_pending_quote": (PendingInput, "仅在用户明确禁止假设或无法形成合理补全方案时保存未完成清单。通常应先complete_estimate补全后finish_quote生成完整待审核稿。"),
    "search_prices": (SearchInput, "查询报价库。无结果时换关键词、放宽材质、翻页或查 draft 状态。空关键词可浏览产品。"),
    "search_prices_semantic": (SemanticSearchInput, "用硅基流动Qwen向量检索需求语义相近的报价候选，可输入完整需求或工艺描述；可选材质/状态/翻页。相似度不是适用性或审批，不自动选最高项；由你核对原始条件及候选差异，再判断引用、调整或估价。关键词检索仍可独立使用，工具顺序由你决定。"),
    "evaluate_price": (PriceInput, "返回候选原始条件和标准价试算。标准价不匹配不代表不能报价，适用性和调整方案由你判断并写入complete_estimate。"),
    "select_price": (SelectInput, "关联已查询且通过后端校验的单价；不可更改需求、单价或规则。"),
    "calculate_quote": (EmptyInput, "调用程序计算全部需求并返回尚待解决的阻塞项，不保存草稿。"),
    "finish_quote": (EmptyInput, "全部需求可计价时保存待审核报价草稿并结束。不能批准或发送报价。"),
    "ask_user": (AskInput, "关键条件无法合理暂估时询问具体问题。后台模式仅暂挂当前项并继续其他项目；指定项目及原因请使用defer_requirement。普通缺项可提出有依据的暂估供审核。"),
}

TOOLS = [{"type": "function", "function": {"name": name, "description": description,
          "parameters": schema.model_json_schema()}} for name, (schema, description) in TOOL_SPECS.items()]
