"""Small, order-scoped tools for an AI that reads and reconciles originals."""
from typing import Literal

from pydantic import Field, model_validator

from .file_workspace import ArtifactInput, FileScriptInput, WorkspaceInput
from .requirements_schemas import ExtractedLine, Region, StrictModel


class ReadingPlan(StrictModel):
    plan: str = Field(min_length=2, max_length=4000)


class ReadSource(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6000, ge=100, le=12000)
    row_count: int = Field(default=12, ge=1, le=50)
    include_image: bool = True
    purpose: Literal["read", "review"] = "read"


class InspectRegion(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1)
    region: Region = (0, 0, 1, 1)
    image_index: int | None = Field(default=None, ge=0,
        description="Excel内嵌图片必填，采用pricing_packet.visual_sources或read_source返回的image_index；PDF不填")
    purpose: Literal["read", "review"] = "read"

    @model_validator(mode="after")
    def rectangle(self):
        if self.region[0] >= self.region[2] or self.region[1] >= self.region[3]:
            raise ValueError("区域须有正的宽度和高度")
        return self


class SearchSources(StrictModel):
    query: str = Field(min_length=1, max_length=150)
    file_id: int = Field(gt=0)
    start_page: int = Field(default=1, ge=1)
    page_count: int = Field(default=20, ge=1, le=30)


class Observation(StrictModel):
    observation_id: str = Field(default="", max_length=50)
    line: ExtractedLine
    reason: str = Field(min_length=2, max_length=1500)


class SourceNote(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1)
    role: Literal["requirements", "drawing", "catalog", "cover", "price_reference", "note", "unknown"]
    summary: str = Field(min_length=2, max_length=1200)
    next_action: str = Field(default="", max_length=500)


class SourceQuery(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=15, ge=1, le=30)
    file_id: int | None = Field(default=None, gt=0)


class SourceListQuery(SourceQuery):
    status: Literal["unread", "unreviewed", "all"] = "unread"


class SaveSourceNotes(StrictModel):
    pages: list[SourceNote] = Field(min_length=1, max_length=10)


class SaveObservations(StrictModel):
    observations: list[Observation] = Field(min_length=1, max_length=30)
    source_notes: list[SourceNote] = Field(default_factory=list, max_length=10)


class ObservationQuery(StrictModel):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=15, ge=1, le=30)
    view: Literal["summary", "full"] = "summary"
    query: str = Field(default="", max_length=150)
    observation_ids: list[str] = Field(default_factory=list, max_length=30)
    include_archived: bool = False
    previous_candidates: bool = False


class ReconcileObservations(StrictModel):
    action: Literal["exclude", "merge", "restore"]
    observation_ids: list[str] = Field(min_length=1, max_length=30)
    reason: str = Field(min_length=2, max_length=1500)
    merged_line: ExtractedLine | None = None


class PageReview(StrictModel):
    file_id: int = Field(gt=0)
    page: int = Field(ge=1)
    role: Literal["requirements", "drawing", "catalog", "cover", "price_reference", "note", "unknown"]
    status: Literal["reviewed", "unresolved"]
    basis: Literal["initial_read", "reread"] = "initial_read"
    reason: str = Field(min_length=2, max_length=1500)
    questions: list[str] = Field(default_factory=list, max_length=10)


class RecordReviews(StrictModel):
    pages: list[PageReview] = Field(min_length=1, max_length=30)


class BeginReview(StrictModel):
    summary: str = Field(min_length=2, max_length=2000)


class FinishReview(BeginReview):
    questions: list[str] = Field(default_factory=list, max_length=30)


SPECS = {
    "list_sources": (SourceListQuery, "默认仅列未完整读取页面，status=unreviewed列未记录结论页面，all查看全部；next_offset续查。当前上下文已有下一页时直接读取，不反复查询目录。"),
    "set_reading_plan": (ReadingPlan, "保存你对附件关系的判断、阅读顺序和核对计划；可随发现调整。"),
    "read_source": (ReadSource, "分段读取原件。PDF默认附整页原图；Excel默认12行，offset为rows的0起始位置，按next_offset续读；其他文件offset为字符位置。读到产品先分批save_observations；非产品或待查内容用record_source_notes保存判断。purpose=review专门复核。"),
    "inspect_source_region": (InspectRegion, "直接查看原始区域图片，不调用另一个提取器。region为整页归一化坐标；Excel须指定read_source返回的image_index。可放大、换区域、purpose=review重读。"),
    "search_sources": (SearchSources, "在指定附件的有限页范围搜索文字以定位清单或图纸。返回命中片段，搜索不等于已读完页面。扫描图无命中不代表无内容。"),
    "list_observations": (ObservationQuery, "默认分页返回项目、数量、尺寸、来源位置和疑点摘要，每页15项。仅修订具体疑点时按observation_ids并用view=full取完整依据；不逐项查询已明确项目。可查归档或旧候选，旧候选不能照抄为事实。"),
    "save_observations": (SaveObservations, "边读边保存，每批1-3项；只提交已知字段，省略空字符串/空列表及未知的可选字段，不复制全部默认值。首次省略observation_id；纠正提交完整line和理由。可同时source_notes保存页面结论及剩余工作，供后续跨文件补充。"),
    "record_source_notes": (SaveSourceNotes, "保存实际读过页面的用途、结论、疑点和下一步；封面/目录也应保存笔记，避免压缩后重读。覆盖结论用record_source_reviews批量记录，笔记不自动排除项目。"),
    "list_source_notes": (SourceQuery, "分页查阅已持久化的页面笔记，支持file_id筛选。需要原文时再针对性read_source，不为回忆用途重读整份文件。"),
    "reconcile_observations": (ReconcileObservations, "你判断目录/说明/价格参考不是订购项目时exclude；同一项目重复视图或跨文件对应时merge并提供完整merged_line；发现误排除时restore。保留前后内容和理由，数量不自动相加。"),
    "begin_source_review": (BeginReview, "可选：集中处理仍有疑点的页面，保留已有结论和当前原件。不要求全部项目逐项比对或重读所有页面。"),
    "record_source_reviews": (RecordReviews, "按页批量保存角色和漏项/重复结论，可在初读时直接记录，默认basis=initial_read沿用已读原件、已保存项目及笔记，不必再次读取。仅确需整页重读时basis=reread并purpose=review读取。未看清区域或未解决归属用unresolved。"),
    "finish_source_review": (FinishReview, "保存页面结论后直接交付计价，无需先begin_source_review。未读、未记录结论或未解决页面如实保留；不要为交付再次逐项检查全部项目。"),
    "list_file_workspace": (WorkspaceInput, "查看隔离脚本输入、原文件和现有处理记录。"),
    "run_file_script": (FileScriptInput, "在隔离容器读取原文件进行搜索、表格解析或辅助计算，输出/work/result.json中的observations/checks。仅返回观察，不自动采用或强校验业务结论；你仍须比对原件并保存判断。"),
    "read_workspace_artifact": (ArtifactInput, "读取本订单脚本输出或脚本，结合原件判断或修正失败脚本。"),
}


def compact_schema(value):
    if isinstance(value, list):
        return [compact_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {key: compact_schema(item) for key, item in value.items() if key not in ("title", "default")}


def tools(*, recovering=False):
    result = []
    for name, (schema, description) in SPECS.items():
        parameters = compact_schema(schema.model_json_schema())
        if name == "save_observations":
            parameters["properties"]["observations"]["maxItems"] = 1 if recovering else 3
            if recovering:
                parameters["properties"]["source_notes"]["maxItems"] = 1
                description += " 当前为格式恢复：本轮只调用一个工具，只保存一个项目。"
        if name == "reconcile_observations":
            # The complete line contract is already advertised by save_observations.
            parameters.pop("$defs", None)
            parameters["properties"]["merged_line"] = {
                "type": "object", "description": "合并后的完整line，字段与save_observations的line相同。"}
        result.append({"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters}})
    return result
