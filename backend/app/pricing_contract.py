"""Versioned boundaries shared by proposal compilation, recovery and API projections."""

ENGINE_VERSION = "pricing.2026-10-16.v1"
WORKSPACE_VERSION = "quote-workspace.v1"
MAX_PARAMETER_BLOCKS = 20
MAX_PARAMETER_FIELDS = 20
MAX_COSTS = 30
MAX_PROPOSED_OBJECTS = 20
WORKFLOW_STATES = (
    "pending", "processing", "priced", "priced_review", "customer_input",
    "administrator_pricing", "source_review", "system_error",
)
STATE_LABELS = {
    "pending": "待计价", "processing": "计价中", "priced": "已计价",
    "priced_review": "已计价待审核", "customer_input": "待客户补充",
    "administrator_pricing": "待管理员核价", "source_review": "待系统核图",
    "system_error": "系统待处理",
}
