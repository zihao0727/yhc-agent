from functools import lru_cache
from pathlib import Path

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
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_timeout: int = 120
    max_upload_bytes: int = 20 * 1024 * 1024
    max_job_pages: int = 20
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_prefix: str = "firefly:agent"
    agent_background: bool = True
    agent_retry_seconds: int = 30
    agent_max_retries: int = Field(default=5, ge=1)
    agent_max_total_tokens: int = Field(default=1000000, ge=1)
    agent_max_cycles: int = Field(default=100, ge=1)
    agent_max_idle_rounds: int = Field(default=30, ge=1)
    agent_slice_rounds: int = Field(default=6, ge=1)
    agent_slice_seconds: int = Field(default=90, ge=1)


@lru_cache
def settings() -> Settings:
    return Settings()
