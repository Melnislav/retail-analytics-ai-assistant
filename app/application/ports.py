"""Hexagonal Architecture Ports (Inbound Use Cases & Outbound Infrastructure Contracts)."""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.models import (
    AnalystSynthesis,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    GoldenTrio,
    GuardrailDecision,
    PendingConfirmation,
    PersonaConfig,
    QueryExecutionResult,
    SavedReport,
    SQLPlan,
    UserContext,
    UserPreferenceProfile,
)

# ==============================================================================
# INBOUND PORTS (Driving Adapters -> Application Core)
# ==============================================================================


class ChatUseCasePort(Protocol):
    async def handle_message(self, request: ChatRequest, user: UserContext) -> ChatResponse: ...

    async def confirm_action(
        self, request: ConfirmationRequest, user: UserContext
    ) -> ChatResponse: ...


class ReportManagementPort(Protocol):
    async def list_user_reports(self, user: UserContext) -> list[SavedReport]: ...

    async def save_report(self, report: SavedReport) -> SavedReport: ...

    async def prepare_deletion(
        self,
        user: UserContext,
        session_id: str,
        scope: str,
        keyword: str | None = None,
    ) -> PendingConfirmation | None: ...

    async def execute_confirmed_deletion(
        self,
        confirmation: PendingConfirmation,
        user: UserContext,
    ) -> int: ...


class PersonaManagementPort(Protocol):
    async def get_persona(self) -> PersonaConfig: ...

    async def update_persona(self, tone_instructions: str, updated_by: str) -> PersonaConfig: ...


# ==============================================================================
# OUTBOUND PORTS (Application Core -> Driven Infrastructure Adapters)
# ==============================================================================


class AnalyticsWarehousePort(Protocol):
    async def execute_query(self, sql_query: str) -> QueryExecutionResult: ...

    async def get_table_schema(self, table_name: str) -> list[dict[str, Any]]: ...

    async def get_all_schemas_summary(self) -> str: ...


class LLMAgentPort(Protocol):
    async def classify_and_guard(
        self,
        user_message: str,
        history: list[ChatMessage],
    ) -> GuardrailDecision: ...

    async def generate_sql_plan(
        self,
        user_message: str,
        schema_summary: str,
        golden_trios: list[GoldenTrio],
        user_context: UserContext,
        history: list[ChatMessage],
        previous_error: str | None = None,
        previous_sql: str | None = None,
    ) -> SQLPlan: ...

    async def synthesize_response(
        self,
        user_message: str,
        query_result: QueryExecutionResult | None,
        golden_trios: list[GoldenTrio],
        persona: PersonaConfig,
        preferences: UserPreferenceProfile,
        history: list[ChatMessage],
    ) -> AnalystSynthesis: ...


class GoldenKnowledgePort(Protocol):
    async def search_similar_trios(self, query: str, top_k: int = 3) -> list[GoldenTrio]: ...

    async def add_trio(self, trio: GoldenTrio) -> GoldenTrio: ...

    async def seed_defaults_if_empty(self) -> None: ...


class ReportRepositoryPort(Protocol):
    async def create(self, report: SavedReport) -> SavedReport: ...

    async def list_by_owner(self, owner_user_id: str) -> list[SavedReport]: ...

    async def find_by_session(self, owner_user_id: str, session_id: str) -> list[SavedReport]: ...

    async def find_by_keyword(self, owner_user_id: str, keyword: str) -> list[SavedReport]: ...

    async def delete_by_ids(self, owner_user_id: str, report_ids: list[str]) -> int: ...


class ConfirmationStorePort(Protocol):
    async def save_pending(self, confirmation: PendingConfirmation) -> None: ...

    async def pop_pending(self, token: str, user_id: str) -> PendingConfirmation | None: ...


class UserPreferencePort(Protocol):
    async def get_preferences(self, user_id: str) -> UserPreferenceProfile: ...

    async def upsert_preferences(self, profile: UserPreferenceProfile) -> UserPreferenceProfile: ...


class PersonaStorePort(Protocol):
    async def get_active_persona(self) -> PersonaConfig: ...

    async def save_persona(self, persona: PersonaConfig) -> PersonaConfig: ...


class CachePort(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl_seconds: int = 300) -> None: ...

    async def append_chat_message(self, session_id: str, msg: ChatMessage) -> None: ...

    async def get_chat_history(self, session_id: str, limit: int = 12) -> list[ChatMessage]: ...


class ObservabilityPort(Protocol):
    def start_trace(self, trace_id: str, user_id: str, session_id: str, input_text: str) -> Any: ...

    def record_span(
        self,
        trace_id: str,
        name: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    def increment_metric(self, metric_name: str, value: int = 1) -> None: ...

    def get_metrics_snapshot(self) -> dict[str, Any]: ...
