from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    database_url: str = "postgresql+psycopg://firefly_pricing_app:CHANGE_ME@127.0.0.1:5432/firefly_pricing"
    admin_token: str = ""
    allowed_origins: list[str] = ["http://127.0.0.1:5173", "http://localhost:5173"]
    storage_dir: Path = Path(__file__).resolve().parents[2] / ".runtime" / "customer-files"
    file_workspace_enabled: bool = True
    file_workspace_image: str = "firefly-file-worker:1"
    file_workspace_timeout: int = Field(default=45, ge=2, le=120)
    file_workspace_memory_mb: int = Field(default=512, ge=128, le=2048)
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout: int = 120
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_embedding_model: str = "Qwen/Qwen3-Embedding-4B"
    siliconflow_timeout: int = Field(default=60, ge=1, le=180)
    max_upload_bytes: int = 20 * 1024 * 1024
    max_pdf_upload_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    max_job_upload_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    max_job_pages: int = Field(default=500, ge=1, le=5000)
    max_requirement_lines: int = Field(default=10000, ge=100, le=10000)
    pdf_render_batch_pages: int = Field(default=4, ge=1, le=20)
    pdf_region_rows: int = Field(default=2, ge=1, le=8)
    pdf_region_columns: int = Field(default=1, ge=1, le=4)
    pdf_max_region_depth: int = Field(default=3, ge=0, le=6)
    visual_region_audit: bool = True
    image_tile_pixels: int = Field(default=1600, ge=800, le=2400)
    extraction_slice_batches: int = Field(default=8, ge=1, le=100)
    excel_native_first: bool = True
    extraction_recovery_rounds: int = Field(default=8, ge=1, le=20)
    source_agent_enabled: bool = True
    source_agent_max_tokens: int = Field(default=3000000, ge=0)
    source_agent_max_turns: int = Field(default=1600, ge=10, le=5000)
    source_agent_context_chars: int = Field(default=48000, ge=12000, le=120000)
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "firefly:agent"
    agent_background: bool = True
    agent_worker_mode: Literal["embedded", "external", "disabled"] = "embedded"
    agent_dependency_check_seconds: int = Field(default=30, ge=5, le=600)
    agent_worker_stall_seconds: int = Field(default=600, ge=30)
    agent_pricing_concurrency: int = Field(default=4, ge=1, le=16)
    agent_retry_seconds: int = 30
    agent_max_retries: int = Field(default=5, ge=1)
    agent_max_total_tokens: int = Field(default=0, ge=0)  # Zero disables the cumulative token cap.
    agent_max_cycles: int = Field(default=100, ge=1)  # Legacy setting; scheduling no longer pauses work.
    agent_max_idle_rounds: int = Field(default=30, ge=1)
    agent_stall_rounds: int = Field(default=12, ge=6, le=200)
    agent_line_max_tokens: int = Field(default=0, ge=0)  # Zero disables the per-line token cap.
    agent_compact_plans: bool = True
    agent_response_max_tokens: int = Field(default=10000, ge=1000, le=16000)
    agent_line_max_seconds: int = Field(default=600, ge=30)
    agent_repair_attempts: int = Field(default=3, ge=1, le=10)
    agent_runtime_recovery: bool = True
    agent_runtime_repair_attempts: int = Field(default=3, ge=1, le=10)
    estimate_contract_version: int = Field(default=3, ge=2, le=3)
    agent_slice_rounds: int = Field(default=6, ge=1)
    agent_slice_seconds: int = Field(default=90, ge=1)
    quote_anomaly_ratio: float = Field(default=4, gt=1, le=1000)


@lru_cache
def settings() -> Settings:
    return Settings()
