"""Dependency Injection composition root wiring Ports to Adapters."""

from __future__ import annotations

from app.adapters.outbound.bigquery_adapter import AsyncBigQueryAdapter
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter
from app.adapters.outbound.persistence_adapter import DatabaseManager, PostgresReportRepository
from app.adapters.outbound.redis_and_telemetry import (
    AsyncRedisAdapter,
    StructuredObservabilityAdapter,
)
from app.application.chat_orchestrator import ChatOrchestratorService
from app.application.report_service import ReportService
from app.domain.pii_masker import PIIMasker
from app.domain.post_sql_guard import PostSQLDataGuard
from app.domain.privacy_policy import PrivacyPolicy
from app.domain.semantic_glossary import SemanticGlossary
from app.domain.sql_policy import SQLPolicyEngine
from app.infrastructure.config import (
    Settings,
    load_business_glossary,
    resolve_local_user_context,
)


class ApplicationContainer:
    """Holds instantiated adapters and application services for FastAPI lifespan."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # HTTP prototype requests use this server-controlled identity. The standalone CLI resolves
        # its selected profile with the same function before entering the application core.
        self.prototype_user = resolve_local_user_context(settings.prototype_user_profile)
        self.db_manager = DatabaseManager(settings.database_url)
        self.redis_adapter = AsyncRedisAdapter(
            redis_url=settings.redis_url,
            confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
            chat_history_ttl_seconds=settings.chat_history_ttl_seconds,
        )
        self.privacy_policy = PrivacyPolicy(
            demographic_min_group_size=settings.demographic_min_group_size
        )
        self.pii_masker = PIIMasker(salt=settings.pii_salt, policy=self.privacy_policy)
        self.post_sql_guard = PostSQLDataGuard(policy=self.privacy_policy)
        self.observability = StructuredObservabilityAdapter(
            langfuse_public_key=settings.langfuse_public_key,
            langfuse_secret_key=settings.langfuse_secret_key,
            langfuse_host=settings.langfuse_host,
            trace_log_path=settings.trace_log_path,
            pii_masker=self.pii_masker,
        )

        # Only saved reports require prototype persistence.
        self.report_repo = PostgresReportRepository(self.db_manager.session_factory)

        # Domain Policies & Semantic Glossary
        merged_glossary = {
            **load_business_glossary(),
            **settings.custom_metric_glossary,
        }
        self.glossary = SemanticGlossary(
            custom_definitions=merged_glossary,
            enabled=settings.enable_semantic_glossary,
            clarification_messages=settings.metric_clarification_messages,
        )
        self.sql_policy = SQLPolicyEngine(
            dataset_id=settings.bq_dataset_id,
            default_limit=settings.default_row_limit,
            demographic_min_group_size=settings.demographic_min_group_size,
            privacy_policy=self.privacy_policy,
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
            dataset_id=settings.bq_dataset_id,
            demographic_min_group_size=settings.demographic_min_group_size,
            pii_masker=self.pii_masker,
            glossary=self.glossary,
        )

        # Application Services
        self.report_service = ReportService(
            repo=self.report_repo,
            confirmation_store=self.redis_adapter,
            observability=self.observability,
            confirmation_ttl_seconds=settings.confirmation_ttl_seconds,
        )
        self.chat_orchestrator = ChatOrchestratorService(
            llm_agent=self.llm_agent,
            warehouse=self.warehouse,
            report_service=self.report_service,
            confirmation_store=self.redis_adapter,
            cache=self.redis_adapter,
            observability=self.observability,
            pii_masker=self.pii_masker,
            sql_policy=self.sql_policy,
            max_sql_retries=settings.max_sql_retries,
            max_llm_calls_per_turn=settings.max_llm_calls_per_turn,
            max_turn_cost_usd=settings.max_turn_cost_usd,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            chat_history_limit=settings.chat_history_limit,
            default_persona_tone=settings.default_persona_tone,
            glossary=self.glossary,
            post_sql_guard=self.post_sql_guard,
        )

    async def startup(self) -> None:
        await self.db_manager.init_schema()

    async def shutdown(self) -> None:
        await self.redis_adapter.close()
        await self.db_manager.close()
