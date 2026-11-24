"""Dependency Injection composition root wiring Ports to Adapters."""

from __future__ import annotations

from app.adapters.outbound.bigquery_adapter import AsyncBigQueryAdapter
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter
from app.adapters.outbound.persistence_adapter import (
    DatabaseManager,
    PostgresGoldenKnowledgeRepository,
    PostgresPersonaRepository,
    PostgresReportRepository,
    PostgresUserPreferenceRepository,
)
from app.adapters.outbound.redis_and_telemetry import (
    AsyncRedisAdapter,
    StructuredObservabilityAdapter,
)
from app.application.chat_orchestrator import ChatOrchestratorService
from app.application.report_service import ReportService
from app.domain.pii_masker import PIIMasker
from app.domain.sql_policy import SQLPolicyEngine
from app.infrastructure.config import Settings


class ApplicationContainer:
    """Holds instantiated adapters and application services for FastAPI lifespan."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_manager = DatabaseManager(settings.database_url)
        self.redis_adapter = AsyncRedisAdapter(
            redis_url=settings.redis_url,
            confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
            chat_history_ttl_seconds=settings.chat_history_ttl_seconds,
        )
        self.observability = StructuredObservabilityAdapter(
            langfuse_public_key=settings.langfuse_public_key,
            langfuse_secret_key=settings.langfuse_secret_key,
            langfuse_host=settings.langfuse_host,
        )

        # Repositories
        self.report_repo = PostgresReportRepository(self.db_manager.session_factory)
        self.golden_kb = PostgresGoldenKnowledgeRepository(
            self.db_manager.session_factory,
            dataset_id=settings.bq_dataset_id,
        )
        self.preference_store = PostgresUserPreferenceRepository(self.db_manager.session_factory)
        self.persona_store = PostgresPersonaRepository(
            self.db_manager.session_factory,
            default_tone=settings.default_persona_tone,
        )

        # Domain Policies
        self.pii_masker = PIIMasker(salt=settings.pii_salt)
        self.sql_policy = SQLPolicyEngine(
            dataset_id=settings.bq_dataset_id,
            default_limit=settings.default_row_limit,
        )

        # Warehouse & LLM Adapters
        self.warehouse = AsyncBigQueryAdapter(
            project_id=settings.gcp_project_id or None,
            dataset_id=settings.bq_dataset_id,
            max_bytes_billed=settings.bq_max_bytes_billed,
            query_timeout_seconds=settings.bq_query_timeout_seconds,
            mock_mode=settings.bigquery_mock_mode,
        )
        self.llm_agent = LangChainRetailAgentAdapter(
            provider=settings.llm_provider,
            google_api_key=settings.google_api_key,
            gemini_model=settings.gemini_model,
            openrouter_api_key=settings.openrouter_api_key,
            openrouter_model=settings.openrouter_model,
            openrouter_base_url=settings.openrouter_base_url,
            openai_api_key=settings.openai_api_key,
            openai_model=settings.openai_model,
            temperature=settings.llm_temperature,
            timeout_seconds=settings.llm_timeout_seconds,
            circuit_breaker_failure_threshold=settings.circuit_breaker_failure_threshold,
            circuit_breaker_recovery_timeout=float(
                settings.circuit_breaker_recovery_timeout_seconds
            ),
            dataset_id=settings.bq_dataset_id,
            pii_masker=self.pii_masker,
        )

        # Application Services
        self.report_service = ReportService(
            repo=self.report_repo,
            confirmation_store=self.redis_adapter,
            observability=self.observability,
        )
        self.chat_orchestrator = ChatOrchestratorService(
            llm_agent=self.llm_agent,
            warehouse=self.warehouse,
            golden_kb=self.golden_kb,
            report_service=self.report_service,
            confirmation_store=self.redis_adapter,
            preference_store=self.preference_store,
            persona_store=self.persona_store,
            cache=self.redis_adapter,
            observability=self.observability,
            pii_masker=self.pii_masker,
            sql_policy=self.sql_policy,
            max_sql_retries=settings.max_sql_retries,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            chat_history_limit=settings.chat_history_limit,
            golden_trio_top_k=settings.golden_trio_top_k,
        )

    async def startup(self) -> None:
        await self.db_manager.init_schema()
        await self.golden_kb.seed_defaults_if_empty()

    async def shutdown(self) -> None:
        await self.redis_adapter.close()
        await self.db_manager.close()
