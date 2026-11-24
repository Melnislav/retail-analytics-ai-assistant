"""Pure Pydantic v2 domain models, DTOs, and structured LLM output schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserRole(StrEnum):
    EXECUTIVE = "executive"
    BRAND_MANAGER = "brand_manager"
    CEO = "ceo"
    ADMIN = "admin"


class OutputFormatPreference(StrEnum):
    AUTO = "auto"
    TABLES = "tables"
    BULLETS = "bullets"
    NARRATIVE = "narrative"


class AnalysisDepthPreference(StrEnum):
    CONCISE = "concise"
    STANDARD = "standard"
    DEEP_DIVE = "deep_dive"


class UserContext(BaseModel):
    """Authenticated executive/manager identity and brand-level RBAC scope."""

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(..., min_length=1, description="Unique user identifier")
    display_name: str = Field(default="Executive User")
    role: UserRole = Field(default=UserRole.EXECUTIVE)
    allowed_brands: list[str] = Field(
        default_factory=list,
        description=(
            "Authorized product brands. Only CEO/ADMIN may leave this empty; "
            "all other roles fail closed."
        ),
    )


class UserPreferenceProfile(BaseModel):
    """User-level continuous learning preferences."""

    user_id: str
    preferred_format: OutputFormatPreference = OutputFormatPreference.AUTO
    preferred_depth: AnalysisDepthPreference = AnalysisDepthPreference.STANDARD
    prefers_charts_or_visuals: bool = False
    custom_notes: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class PersonaConfig(BaseModel):
    """Dynamic CEO-managed assistant tone & reporting instructions."""

    persona_id: str = "global_executive_persona"
    tone_instructions: str = (
        "Executive, concise, data-driven, and action-oriented. "
        "Highlight key drivers, anomalies, and concrete next-quarter action items."
    )
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utc_now)


class IntentType(StrEnum):
    DATA_ANALYSIS = "data_analysis"
    SCHEMA_DISCOVERY = "schema_discovery"
    SAVE_REPORT = "save_report"
    LIST_REPORTS = "list_reports"
    DELETE_REPORTS = "delete_reports"
    UPDATE_PREFERENCES = "update_preferences"
    GENERAL_FOLLOWUP = "general_followup"
    BLOCKED_OFF_TOPIC = "blocked_off_topic"
    BLOCKED_INJECTION = "blocked_injection"


class GuardrailDecision(BaseModel):
    """Structured LLM classification for safety, intent routing, and destructive ops."""

    intent: IntentType
    is_safe: bool = True
    rejection_reason: str | None = None
    delete_scope: str | None = Field(
        default=None,
        description="For DELETE_REPORTS: 'conversation' or 'keyword'",
    )
    delete_keyword: str | None = Field(
        default=None,
        description="Entity/client keyword mentioned for report deletion, e.g., 'Client X'",
    )
    should_save_report: bool = Field(
        default=False,
        description="True when user explicitly asks to create/save a report.",
    )
    detected_format_preference: OutputFormatPreference | None = None
    detected_depth_preference: AnalysisDepthPreference | None = None


class SQLPlan(BaseModel):
    """Structured SQL generation output from the LLM agent."""

    reasoning: str = Field(..., description="Analytical logic referencing the available schema")
    sql_query: str = Field(..., description="Valid BigQuery StandardSQL SELECT query")
    requires_sql: bool = Field(
        default=True,
        description="False if question can be answered directly from metadata/history without querying BQ.",
    )
    target_tables: list[str] = Field(default_factory=list)


class SQLValidationResult(BaseModel):
    """Result of deterministic SQL safety and RBAC policy enforcement."""

    is_valid: bool
    sanitized_sql: str = ""
    violations: list[str] = Field(default_factory=list)
    applied_rbac_filters: list[str] = Field(default_factory=list)


class QueryExecutionResult(BaseModel):
    """Normalized execution output from BigQuery."""

    sql_executed: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    columns: list[str] = Field(default_factory=list)
    bytes_processed: int = 0
    from_cache: bool = False
    attempts: int = 1


class AnalystSynthesis(BaseModel):
    """Structured executive answer and optional report artifact."""

    executive_summary: str
    detailed_analysis: str
    action_items: list[str] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list)
    report_title: str | None = None
    used_metric_definitions: list[str] = Field(
        default_factory=list,
        description="Explicit retail metric definitions applied during this analysis turn.",
    )


class SavedReport(BaseModel):
    """Persisted executive report owned by a specific user."""

    report_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    session_id: str
    title: str
    summary: str
    content_markdown: str
    action_items: list[str] = Field(default_factory=list)
    sql_used: str | None = None
    metric_definitions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ConfirmationAction(StrEnum):
    DELETE_REPORTS = "delete_reports"


class PendingConfirmation(BaseModel):
    """High-stakes destructive action awaiting explicit user confirmation."""

    token: str = Field(default_factory=lambda: f"confirm_{uuid4().hex[:12]}")
    user_id: str
    session_id: str
    action: ConfirmationAction = ConfirmationAction.DELETE_REPORTS
    description: str
    target_report_ids: list[str]
    target_report_titles: list[str]
    created_at: datetime = Field(default_factory=utc_now)


class ChatMessage(BaseModel):
    """Single conversation turn stored in session history."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """Inbound chat request payload."""

    session_id: str = Field(default_factory=lambda: f"sess_{uuid4().hex[:10]}")
    message: str = Field(..., min_length=1, max_length=4000)
    save_report: bool = Field(
        default=False,
        description="Optional explicit flag to persist generated report to Saved Reports library.",
    )


class ConfirmationRequest(BaseModel):
    """Inbound payload to approve or cancel a high-stakes destructive action."""

    session_id: str
    token: str
    approved: bool


class ChatResponse(BaseModel):
    """Outbound chat response payload (strictly PII-masked)."""

    session_id: str
    trace_id: str
    reply: str
    action_items: list[str] = Field(default_factory=list)
    sql_used: str | None = None
    data_preview: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    saved_report_id: str | None = None
    pending_confirmation: PendingConfirmation | None = None
    observability: dict[str, Any] = Field(default_factory=dict)
    used_metric_definitions: list[str] = Field(
        default_factory=list,
        description="Explicit retail metric definitions applied during this analysis turn.",
    )
