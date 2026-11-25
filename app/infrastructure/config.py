"""Pydantic v2 BaseSettings configuration loaded from .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.models import UserContext, UserRole


def load_user_rbac_profiles(
    config_path: str | Path = "configs/users_rbac.yaml",
) -> dict[str, dict[str, Any]]:
    """Load local user RBAC and brand mapping profiles.

    Falls back to sensible built-in defaults if the file does not exist.
    """
    path = Path(config_path)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "users" in data and isinstance(data["users"], dict):
                    return data["users"]
        except Exception:
            pass

    return {
        "ceo": {
            "user_id": "ceo_carol",
            "display_name": "Carol (Executive CEO)",
            "role": "ceo",
            "allowed_brands": [],
        },
        "brand_manager_alice": {
            "user_id": "mgr_alice",
            "display_name": "Alice (Brand Manager - Calvin Klein)",
            "role": "brand_manager",
            "allowed_brands": ["Calvin Klein"],
        },
        "brand_manager_bob": {
            "user_id": "mgr_bob",
            "display_name": "Bob (Multi-Brand Manager)",
            "role": "brand_manager",
            "allowed_brands": ["Diesel", "Tommy Hilfiger"],
        },
    }


def resolve_local_user_context(
    profile_key: str,
    config_path: str | Path = "configs/users_rbac.yaml",
) -> UserContext:
    """Resolve a prototype identity exclusively from the local RBAC configuration."""
    profile = load_user_rbac_profiles(config_path).get(profile_key)
    if not isinstance(profile, dict):
        raise ValueError(f"Unknown local RBAC profile: {profile_key}")

    try:
        return UserContext(
            user_id=str(profile["user_id"]),
            display_name=str(profile.get("display_name", "Executive User")),
            role=UserRole(str(profile["role"]).lower()),
            allowed_brands=[str(brand) for brand in profile.get("allowed_brands", [])],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid local RBAC profile: {profile_key}") from exc


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="Retail Data Analysis Assistant", validation_alias="APP_NAME")
    app_env: str = Field(default="production", validation_alias="APP_ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    api_host: str = Field(default="0.0.0.0", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")

    # LLM Provider (Primary & Fallback: Gemini, OpenRouter, OpenAI)
    llm_provider: str = Field(default="gemini", validation_alias="LLM_PROVIDER")
    google_api_key: str = Field(default="", validation_alias="GOOGLE_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")
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
        default="sqlite+aiosqlite:///./retail_local.db", validation_alias="DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", validation_alias="REDIS_URL")
    confirmation_ttl_seconds: int = Field(default=600, validation_alias="CONFIRMATION_TTL_SECONDS")
    cache_ttl_seconds: int = Field(default=300, validation_alias="CACHE_TTL_SECONDS")

    # Resilience & Persona
    max_sql_retries: int = Field(default=3, validation_alias="MAX_SQL_RETRIES")
    max_llm_calls_per_turn: int = Field(default=5, ge=1, validation_alias="MAX_LLM_CALLS_PER_TURN")
    max_turn_cost_usd: float = Field(default=1.0, gt=0.0, validation_alias="MAX_TURN_COST_USD")
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
    prototype_user_profile: str = Field(
        default="brand_manager_alice", validation_alias="PROTOTYPE_USER_PROFILE"
    )
    # Data Protection & PII
    pii_salt: str = Field(default="retail-pii-salt-2026", validation_alias="PII_SALT")
    demographic_min_group_size: int = Field(
        default=5,
        ge=2,
        validation_alias="DEMOGRAPHIC_MIN_GROUP_SIZE",
    )

    # Observability
    langfuse_public_key: str = Field(default="", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", validation_alias="LANGFUSE_HOST"
    )

    # Semantic Glossary & Retail Metric Defaults
    enable_semantic_glossary: bool = Field(
        default=True, validation_alias="ENABLE_SEMANTIC_GLOSSARY"
    )
    custom_metric_glossary: dict[str, str] = Field(
        default_factory=dict, validation_alias="CUSTOM_METRIC_GLOSSARY"
    )
    metric_clarification_messages: dict[str, str] = Field(
        default_factory=dict, validation_alias="METRIC_CLARIFICATION_MESSAGES"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
