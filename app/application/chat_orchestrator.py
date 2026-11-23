"""Core async orchestration service connecting Guardrails, Golden Bucket, SQL Self-Correction, PII Masking, and Synthesis."""

from __future__ import annotations

import hashlib
import logging
import time
from uuid import uuid4

from app.application.ports import (
    AnalyticsWarehousePort,
    CachePort,
    ConfirmationStorePort,
    GoldenKnowledgePort,
    LLMAgentPort,
    ObservabilityPort,
    PersonaStorePort,
    UserPreferencePort,
)
from app.application.report_service import ReportService
from app.domain.exceptions import QueryExecutionError, SQLSafetyError
from app.domain.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    IntentType,
    QueryExecutionResult,
    SavedReport,
    UserContext,
)
from app.domain.pii_masker import PIIMasker
from app.domain.sql_policy import SQLPolicyEngine

logger = logging.getLogger(__name__)


class ChatOrchestratorService:
    """Implements ChatUseCasePort using Ports & Adapters."""

    def __init__(
        self,
        llm_agent: LLMAgentPort,
        warehouse: AnalyticsWarehousePort,
        golden_kb: GoldenKnowledgePort,
        report_service: ReportService,
        confirmation_store: ConfirmationStorePort,
        preference_store: UserPreferencePort,
        persona_store: PersonaStorePort,
        cache: CachePort,
        observability: ObservabilityPort,
        pii_masker: PIIMasker,
        sql_policy: SQLPolicyEngine,
        max_sql_retries: int = 3,
        cache_ttl_seconds: int = 300,
        chat_history_limit: int = 10,
        golden_trio_top_k: int = 3,
    ) -> None:
        self._llm = llm_agent
        self._warehouse = warehouse
        self._golden_kb = golden_kb
        self._reports = report_service
        self._confirmations = confirmation_store
        self._preferences = preference_store
        self._persona = persona_store
        self._cache = cache
        self._obs = observability
        self._masker = pii_masker
        self._sql_policy = sql_policy
        self._max_sql_retries = max_sql_retries
        self._cache_ttl = cache_ttl_seconds
        self._chat_history_limit = chat_history_limit
        self._golden_trio_top_k = golden_trio_top_k

    async def confirm_action(self, request: ConfirmationRequest, user: UserContext) -> ChatResponse:
        trace_id = f"tr_{uuid4().hex[:12]}"
        self._obs.start_trace(
            trace_id, user.user_id, request.session_id, f"CONFIRM:{request.token}"
        )

        pending = await self._confirmations.pop_pending(request.token, user.user_id)
        if pending is None:
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply="Confirmation token is invalid, expired, or belongs to another user. No reports were deleted.",
            )

        if not request.approved:
            self._obs.increment_metric("destructive_confirmations_cancelled")
            reply = f"Cancelled. Your {len(pending.target_report_ids)} report(s) remain untouched."
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply=reply,
            )

        deleted_count = await self._reports.execute_confirmed_deletion(pending, user)
        titles_str = ", ".join(f"'{t}'" for t in pending.target_report_titles)
        reply = f"Confirmed and executed: deleted {deleted_count} report(s) ({titles_str})."
        await self._cache.append_chat_message(
            request.session_id, ChatMessage(role="assistant", content=reply)
        )
        return ChatResponse(
            session_id=request.session_id,
            trace_id=trace_id,
            reply=reply,
        )

    async def handle_message(self, request: ChatRequest, user: UserContext) -> ChatResponse:
        t_start = time.perf_counter()
        trace_id = f"tr_{uuid4().hex[:12]}"
        self._obs.start_trace(trace_id, user.user_id, request.session_id, request.message)
        self._obs.increment_metric("chat_requests_total")

        history = await self._cache.get_chat_history(
            request.session_id, limit=self._chat_history_limit
        )
        await self._cache.append_chat_message(
            request.session_id, ChatMessage(role="user", content=request.message)
        )

        # 1. Safety Guardrail & Intent Classification
        t0 = time.perf_counter()
        decision = await self._llm.classify_and_guard(request.message, history)
        self._obs.record_span(
            trace_id,
            "guardrail_classification",
            (time.perf_counter() - t0) * 1000,
            {"intent": decision.intent.value, "is_safe": decision.is_safe},
        )

        if not decision.is_safe or decision.intent in {
            IntentType.BLOCKED_OFF_TOPIC,
            IntentType.BLOCKED_INJECTION,
        }:
            self._obs.increment_metric("guardrail_blocks_total")
            refusal = (
                decision.rejection_reason
                or "I am an executive retail data analysis assistant and can only answer questions related to retail sales, customers, products, and saved reports."
            )
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=refusal)
            )
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply=refusal,
                observability={"trace_id": trace_id, "blocked": True},
            )

        # 2. User-Level Continuous Learning: update preferences if detected
        prefs = await self._preferences.get_preferences(user.user_id)
        prefs_changed = False
        if decision.detected_format_preference is not None:
            prefs.preferred_format = decision.detected_format_preference
            prefs_changed = True
        if decision.detected_depth_preference is not None:
            prefs.preferred_depth = decision.detected_depth_preference
            prefs_changed = True
        if prefs_changed:
            prefs = await self._preferences.upsert_preferences(prefs)
            self._obs.increment_metric("user_preference_updates_total")

        # 3. High-Stakes Destructive Operation Flow (Saved Reports Deletion)
        if decision.intent == IntentType.DELETE_REPORTS:
            pending = await self._reports.prepare_deletion(
                user=user,
                session_id=request.session_id,
                scope=decision.delete_scope or "keyword",
                keyword=decision.delete_keyword,
            )
            if pending is None:
                reply = "No matching saved reports owned by your account were found to delete."
                await self._cache.append_chat_message(
                    request.session_id, ChatMessage(role="assistant", content=reply)
                )
                return ChatResponse(session_id=request.session_id, trace_id=trace_id, reply=reply)

            titles_bullets = "\n".join(f"- {t}" for t in pending.target_report_titles)
            reply = (
                f"**Confirmation Required (Destructive Action)**\n"
                f"{pending.description}\n\n"
                f"Matched reports:\n{titles_bullets}\n\n"
                f"Please confirm using token `{pending.token}` (or approve in the prompt) before I permanently delete them."
            )
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply=reply,
                pending_confirmation=pending,
                observability={"trace_id": trace_id, "awaiting_confirmation": True},
            )

        # 4. List Saved Reports Shortcut
        if decision.intent == IntentType.LIST_REPORTS:
            reports = await self._reports.list_user_reports(user)
            if not reports:
                reply = "You currently have no saved reports."
            else:
                lines = [
                    f"- **{r.title}** (`{r.report_id[:8]}...`, {r.created_at.strftime('%Y-%m-%d %H:%M UTC')})"
                    for r in reports
                ]
                reply = "Here are your saved reports:\n" + "\n".join(lines)
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(session_id=request.session_id, trace_id=trace_id, reply=reply)

        # 5. Hybrid Intelligence: retrieve Golden Trios + active CEO Persona + BigQuery Schema
        t_ret = time.perf_counter()
        golden_trios = await self._golden_kb.search_similar_trios(
            request.message, top_k=self._golden_trio_top_k
        )
        persona = await self._persona.get_active_persona()
        schema_summary = await self._warehouse.get_all_schemas_summary()
        self._obs.record_span(
            trace_id,
            "hybrid_context_retrieval",
            (time.perf_counter() - t_ret) * 1000,
            {"golden_trios_matched": len(golden_trios)},
        )

        # 6. SQL Generation + Safety Validation + Self-Correction Retry Loop
        query_result: QueryExecutionResult | None = None
        last_error: str | None = None
        last_sql: str | None = None
        pii_redactions_total = 0

        for attempt in range(1, self._max_sql_retries + 1):
            t_sql = time.perf_counter()
            sql_plan = await self._llm.generate_sql_plan(
                user_message=request.message,
                schema_summary=schema_summary,
                golden_trios=golden_trios,
                user_context=user,
                history=history,
                previous_error=last_error,
                previous_sql=last_sql,
            )
            self._obs.record_span(
                trace_id,
                f"sql_generation_attempt_{attempt}",
                (time.perf_counter() - t_sql) * 1000,
                {"requires_sql": sql_plan.requires_sql},
            )

            if not sql_plan.requires_sql:
                break

            validation = self._sql_policy.validate_and_secure(sql_plan.sql_query, user)
            if not validation.is_valid:
                last_error = f"SQL Policy Violation: {'; '.join(validation.violations)}"
                last_sql = sql_plan.sql_query
                self._obs.increment_metric("sql_self_correction_attempts")
                logger.warning("Attempt %d SQL policy rejected: %s", attempt, last_error)
                if attempt == self._max_sql_retries:
                    raise SQLSafetyError(last_error)
                continue

            secured_sql = validation.sanitized_sql
            last_sql = secured_sql

            # Check Redis query cache first
            sql_hash = hashlib.sha256(secured_sql.encode("utf-8")).hexdigest()[:16]
            cache_key = f"bq_cache:{sql_hash}"
            cached_payload = await self._cache.get_json(cache_key)
            if cached_payload is not None:
                query_result = QueryExecutionResult(
                    **cached_payload, from_cache=True, attempts=attempt
                )
                self._obs.increment_metric("bq_cache_hits")
                break

            try:
                t_bq = time.perf_counter()
                raw_result = await self._warehouse.execute_query(secured_sql)
                self._obs.record_span(
                    trace_id,
                    f"bigquery_execution_attempt_{attempt}",
                    (time.perf_counter() - t_bq) * 1000,
                    {"row_count": raw_result.row_count},
                )

                # Self-correct on empty result set (e.g., overly strict filter or wrong state/category casing)
                if raw_result.row_count == 0 and attempt < self._max_sql_retries:
                    last_error = (
                        "Query executed cleanly but returned 0 rows. Verify filter literals "
                        "(e.g., case sensitivity, date ranges, or status values) and broaden filters if appropriate."
                    )
                    self._obs.increment_metric("sql_self_correction_attempts")
                    continue

                # Mandatory PII masking before caching or passing to LLM synthesis
                masked_rows, row_redactions = self._masker.mask_rows(raw_result.rows)
                pii_redactions_total += row_redactions
                query_result = QueryExecutionResult(
                    sql_executed=secured_sql,
                    rows=masked_rows,
                    row_count=raw_result.row_count,
                    columns=raw_result.columns,
                    bytes_processed=raw_result.bytes_processed,
                    from_cache=False,
                    attempts=attempt,
                )
                await self._cache.set_json(
                    cache_key,
                    query_result.model_dump(exclude={"from_cache", "attempts"}),
                    ttl_seconds=self._cache_ttl,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                self._obs.increment_metric("sql_self_correction_attempts")
                logger.warning("Attempt %d BigQuery error: %s", attempt, last_error)
                if attempt == self._max_sql_retries:
                    raise QueryExecutionError(
                        f"Unable to execute a valid analytical query after {self._max_sql_retries} attempts: {last_error}",
                        last_sql=last_sql,
                    ) from exc

        # 7. Executive Synthesis + Output PII Masking
        t_syn = time.perf_counter()
        synthesis = await self._llm.synthesize_response(
            user_message=request.message,
            query_result=query_result,
            golden_trios=golden_trios,
            persona=persona,
            preferences=prefs,
            history=history,
        )
        self._obs.record_span(
            trace_id,
            "executive_synthesis",
            (time.perf_counter() - t_syn) * 1000,
        )

        combined_md = f"{synthesis.executive_summary}\n\n{synthesis.detailed_analysis}"
        if synthesis.action_items:
            actions_md = "\n".join(f"- {item}" for item in synthesis.action_items)
            combined_md += f"\n\n### Action Items\n{actions_md}"

        masked_reply, text_redactions = self._masker.mask_text(combined_md)
        pii_redactions_total += text_redactions
        if pii_redactions_total > 0:
            self._obs.increment_metric("pii_redactions_total", pii_redactions_total)

        masked_actions = [self._masker.mask_text(a)[0] for a in synthesis.action_items]

        # 8. Optional Report Persistence
        saved_report_id: str | None = None
        if request.save_report or decision.should_save_report:
            report_title = synthesis.report_title or f"Retail Analysis: {request.message[:60]}"
            saved = await self._reports.save_report(
                SavedReport(
                    owner_user_id=user.user_id,
                    session_id=request.session_id,
                    title=report_title,
                    summary=self._masker.mask_text(synthesis.executive_summary)[0],
                    content_markdown=masked_reply,
                    action_items=masked_actions,
                    sql_used=query_result.sql_executed if query_result else None,
                )
            )
            saved_report_id = saved.report_id

        await self._cache.append_chat_message(
            request.session_id, ChatMessage(role="assistant", content=masked_reply)
        )

        total_ms = round((time.perf_counter() - t_start) * 1000, 2)
        return ChatResponse(
            session_id=request.session_id,
            trace_id=trace_id,
            reply=masked_reply,
            action_items=masked_actions,
            sql_used=query_result.sql_executed if query_result else None,
            data_preview=(query_result.rows[:10] if query_result else []),
            row_count=query_result.row_count if query_result else 0,
            saved_report_id=saved_report_id,
            observability={
                "trace_id": trace_id,
                "total_latency_ms": total_ms,
                "sql_attempts": query_result.attempts if query_result else 0,
                "from_cache": query_result.from_cache if query_result else False,
                "pii_redactions": pii_redactions_total,
                "golden_trios_used": len(golden_trios),
            },
        )
