"""Pydantic v2 BaseSettings configuration loaded from .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(
        default="OpsFleet Retail Data Analysis Assistant", validation_alias="APP_NAME"
    )
    app_env: str = Field(default="production", validation_alias="APP_ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")

    # LLM Provider (Primary & Fallback: Gemini, OpenRouter, OpenAI)
    llm_provider: str = Field(default="gemini", validation_alias="LLM_PROVIDER")
    google_api_key: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", validation_alias="GEMINI_MODEL")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="google/gemma-4-31b-it:free", validation_alias="OPENROUTER_MODEL"
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", validation_alias="OPENROUTER_BASE_URL"
    )
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", validation_alias="OPENAI_MODEL")
    llm_temperature: float = Field(default=0.1, validation_alias="LLM_TEMPERATURE")
    llm_timeout_seconds: int = Field(default=15, validation_alias="LLM_TIMEOUT_SECONDS")

    # Circuit Breaker & Resilience
    circuit_breaker_failure_threshold: int = Field(
        default=2, validation_alias="CIRCUIT_BREAKER_FAILURE_THRESHOLD"
    )
    circuit_breaker_recovery_timeout_seconds: int = Field(
        default=60, validation_alias="CIRCUIT_BREAKER_RECOVERY_TIMEOUT_SECONDS"
    )

    # BigQuery
    gcp_project_id: str = Field(default="", validation_alias="GCP_PROJECT_ID")
    bq_dataset_id: str = Field(
        default="bigquery-public-data.thelook_ecommerce", validation_alias="BQ_DATASET_ID"
    )
    bq_max_bytes_billed: int = Field(default=1_073_741_824, validation_alias="BQ_MAX_BYTES_BILLED")
    bq_query_timeout_seconds: int = Field(default=30, validation_alias="BQ_QUERY_TIMEOUT_SECONDS")
    bigquery_mock_mode: bool = Field(default=True, validation_alias="BIGQUERY_MOCK_MODE")

    # Database & Redis
    database_url: str = Field(
        default="sqlite+aiosqlite:///./opsfleet_local.db", validation_alias="DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    confirmation_ttl_seconds: int = Field(default=600, validation_alias="CONFIRMATION_TTL_SECONDS")
    cache_ttl_seconds: int = Field(default=300, validation_alias="CACHE_TTL_SECONDS")

    # Resilience & Persona
    max_sql_retries: int = Field(default=3, validation_alias="MAX_SQL_RETRIES")
    default_row_limit: int = Field(default=200, validation_alias="DEFAULT_ROW_LIMIT")
    default_persona_tone: str = Field(
        default=(
            "Executive, concise, data-driven, and action-oriented. "
            "Highlight key drivers, anomalies, and concrete next-quarter action items."
        ),
        validation_alias="DEFAULT_PERSONA_TONE",
    )
    chat_history_limit: int = Field(default=10, validation_alias="CHAT_HISTORY_LIMIT")
    chat_history_ttl_seconds: int = Field(
        default=86400, validation_alias="CHAT_HISTORY_TTL_SECONDS"
    )
    golden_trio_top_k: int = Field(default=3, validation_alias="GOLDEN_TRIO_TOP_K")

    # Data Protection & PII
    pii_salt: str = Field(default="opsfleet-pii-salt-2026", validation_alias="PII_SALT")

    # Observability
    langfuse_public_key: str = Field(default="", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", validation_alias="LANGFUSE_HOST"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
