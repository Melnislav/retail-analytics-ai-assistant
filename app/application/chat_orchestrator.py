"""Core prototype orchestration for guardrails, SQL, PII masking, and reports."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.application.ports import (
    AnalyticsWarehousePort,
    CachePort,
    ConfirmationStorePort,
    LLMAgentPort,
    ObservabilityPort,
)
from app.application.report_service import ReportService
from app.domain.exceptions import QueryExecutionError, SQLSafetyError
from app.domain.models import (
    AnalystSynthesis,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    IntentType,
    PersonaConfig,
    QueryExecutionResult,
    SavedReport,
    SQLPlan,
    UserContext,
    UserPreferenceProfile,
)
from app.domain.pii_masker import PIIMasker
from app.domain.semantic_glossary import SemanticGlossary
from app.domain.sql_policy import SQLPolicyEngine

logger = logging.getLogger(__name__)


class AnalysisWorkflowState(TypedDict, total=False):
    user_message: str
    user_context: UserContext
    history: list[ChatMessage]
    schema_summary: str
    preferences: UserPreferenceProfile
    trace_id: str
    attempt: int
    sql_plan: SQLPlan | None
    query_result: QueryExecutionResult | None
    synthesis: AnalystSynthesis | None
    last_error: str | None
    last_sql: str | None
    fatal_error: Exception | None
    redactions_count: int


class ChatOrchestratorService:
    """Implements ChatUseCasePort using Ports & Adapters."""

    def __init__(
        self,
        llm_agent: LLMAgentPort,
        warehouse: AnalyticsWarehousePort,
        report_service: ReportService,
        confirmation_store: ConfirmationStorePort,
        cache: CachePort,
        observability: ObservabilityPort,
        pii_masker: PIIMasker,
        sql_policy: SQLPolicyEngine,
        max_sql_retries: int = 3,
        cache_ttl_seconds: int = 300,
        chat_history_limit: int = 10,
        default_persona_tone: str = PersonaConfig().tone_instructions,
        glossary: SemanticGlossary | None = None,
    ) -> None:
        self._llm = llm_agent
        self._warehouse = warehouse
        self._reports = report_service
        self._confirmations = confirmation_store
        self._persona = PersonaConfig(tone_instructions=default_persona_tone)
        self._cache = cache
        self._obs = observability
        self._masker = pii_masker
        self._sql_policy = sql_policy
        self._max_sql_retries = max_sql_retries
        self._cache_ttl = cache_ttl_seconds
        self._chat_history_limit = chat_history_limit
        self._glossary = glossary or SemanticGlossary()
        self._analysis_graph = self._build_analysis_graph()

    def _build_analysis_graph(self) -> CompiledStateGraph:
        workflow = StateGraph(AnalysisWorkflowState)

        async def generate_sql_node(state: AnalysisWorkflowState) -> dict[str, Any]:
            t_sql = time.perf_counter()
            attempt = state.get("attempt", 1)
            trace_id = state.get("trace_id", "")
            plan = await self._llm.generate_sql_plan(
                user_message=state["user_message"],
                schema_summary=state["schema_summary"],
                user_context=state["user_context"],
                history=state["history"],
                previous_error=state.get("last_error"),
                previous_sql=state.get("last_sql"),
            )
            if trace_id:
                self._obs.record_span(
                    trace_id,
                    f"sql_generation_attempt_{attempt}",
                    (time.perf_counter() - t_sql) * 1000,
                    {"requires_sql": plan.requires_sql},
                )
            return {"sql_plan": plan}

        async def execute_query_node(state: AnalysisWorkflowState) -> dict[str, Any]:
            plan = state.get("sql_plan")
            if plan is None or not plan.requires_sql:
                return {"query_result": None}

            attempt = state.get("attempt", 1)
            trace_id = state.get("trace_id", "")
            user = state["user_context"]

            validation = self._sql_policy.validate_and_secure(plan.sql_query, user)
            if not validation.is_valid:
                error_msg = f"SQL Policy Violation: {'; '.join(validation.violations)}"
                self._obs.increment_metric("sql_self_correction_attempts")
                logger.warning("Attempt %d SQL policy rejected: %s", attempt, error_msg)
                policy_fatal: Exception | None = (
                    SQLSafetyError(error_msg) if attempt >= self._max_sql_retries else None
                )
                return {
                    "last_error": error_msg,
                    "last_sql": plan.sql_query,
                    "attempt": attempt + 1,
                    "fatal_error": policy_fatal,
                }

            secured_sql = validation.sanitized_sql
            sql_hash = hashlib.sha256(secured_sql.encode("utf-8")).hexdigest()[:16]
            cache_key = f"bq_cache:{sql_hash}"
            cached_payload = await self._cache.get_json(cache_key)
            if cached_payload is not None:
                cached_result = QueryExecutionResult(
                    **cached_payload, from_cache=True, attempts=attempt
                )
                masked_rows, row_redactions = self._masker.mask_rows(cached_result.rows)
                self._obs.increment_metric("bq_cache_hits")
                return {
                    "query_result": cached_result.model_copy(update={"rows": masked_rows}),
                    "last_sql": secured_sql,
                    "redactions_count": state.get("redactions_count", 0) + row_redactions,
                }

            try:
                t_bq = time.perf_counter()
                raw_result = await self._warehouse.execute_query(secured_sql)
                if trace_id:
                    self._obs.record_span(
                        trace_id,
                        f"bigquery_execution_attempt_{attempt}",
                        (time.perf_counter() - t_bq) * 1000,
                        {"row_count": raw_result.row_count},
                    )

                if raw_result.row_count == 0 and attempt < self._max_sql_retries:
                    err_msg = (
                        "Query executed cleanly but returned 0 rows. Verify filter literals "
                        "(e.g., case sensitivity, date ranges, or status values) and broaden filters if appropriate."
                    )
                    self._obs.increment_metric("sql_self_correction_attempts")
                    return {
                        "last_error": err_msg,
                        "last_sql": secured_sql,
                        "attempt": attempt + 1,
                    }

                masked_rows, row_redactions = self._masker.mask_rows(raw_result.rows)
                result = QueryExecutionResult(
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
                    result.model_dump(exclude={"from_cache", "attempts"}),
                    ttl_seconds=self._cache_ttl,
                )
                return {
                    "query_result": result,
                    "last_sql": secured_sql,
                    "redactions_count": state.get("redactions_count", 0) + row_redactions,
                }
            except Exception as exc:
                err_msg = str(exc)
                self._obs.increment_metric("sql_self_correction_attempts")
                logger.warning("Attempt %d BigQuery error: %s", attempt, err_msg)
                exec_fatal: Exception | None = (
                    QueryExecutionError(
                        f"Unable to execute a valid analytical query after {self._max_sql_retries} attempts: {err_msg}",
                        last_sql=secured_sql,
                    )
                    if attempt >= self._max_sql_retries
                    else None
                )
                return {
                    "last_error": err_msg,
                    "last_sql": secured_sql,
                    "attempt": attempt + 1,
                    "fatal_error": exec_fatal,
                }

        async def synthesize_node(state: AnalysisWorkflowState) -> dict[str, Any]:
            t_syn = time.perf_counter()
            trace_id = state.get("trace_id", "")
            synthesis = await self._llm.synthesize_response(
                user_message=state["user_message"],
                query_result=state.get("query_result"),
                persona=self._persona,
                preferences=state["preferences"],
                history=state["history"],
            )
            if trace_id:
                self._obs.record_span(
                    trace_id,
                    "executive_synthesis",
                    (time.perf_counter() - t_syn) * 1000,
                    {"has_action_items": len(synthesis.action_items) > 0},
                )
            return {"synthesis": synthesis}

        def route_after_execution(state: AnalysisWorkflowState) -> str:
            if state.get("fatal_error") is not None:
                return END
            sql_plan = state.get("sql_plan")
            if state.get("query_result") is not None or (sql_plan and not sql_plan.requires_sql):
                return "synthesize"
            if state.get("last_error") and state.get("attempt", 1) <= self._max_sql_retries:
                return "generate_sql"
            return END

        workflow.add_node("generate_sql", generate_sql_node)
        workflow.add_node("execute_query", execute_query_node)
        workflow.add_node("synthesize", synthesize_node)

        workflow.add_edge(START, "generate_sql")
        workflow.add_edge("generate_sql", "execute_query")
        workflow.add_conditional_edges(
            "execute_query",
            route_after_execution,
            {
                "generate_sql": "generate_sql",
                "synthesize": "synthesize",
                END: END,
            },
        )
        workflow.add_edge("synthesize", END)

        return workflow.compile()

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
        reply = self._mask_text(
            f"Confirmed and executed: deleted {deleted_count} report(s) ({titles_str})."
        )
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
            refusal = self._mask_text(
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

        # Preferences apply to this response only; persistence is design-only for the prototype.
        prefs = UserPreferenceProfile(user_id=user.user_id)
        if decision.detected_format_preference is not None:
            prefs.preferred_format = decision.detected_format_preference
        if decision.detected_depth_preference is not None:
            prefs.preferred_depth = decision.detected_depth_preference

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

            safe_pending = pending.model_copy(
                update={
                    "description": self._mask_text(pending.description),
                    "target_report_titles": [
                        self._mask_text(title) for title in pending.target_report_titles
                    ],
                }
            )
            titles_bullets = "\n".join(f"- {t}" for t in safe_pending.target_report_titles)
            reply = (
                f"**Confirmation Required (Destructive Action)**\n"
                f"{safe_pending.description}\n\n"
                f"Matched reports:\n{titles_bullets}\n\n"
                f"Please confirm using token `{safe_pending.token}` (or approve in the prompt) before I permanently delete them."
            )
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply=reply,
                pending_confirmation=safe_pending,
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
                reply = self._mask_text("Here are your saved reports:\n" + "\n".join(lines))
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(session_id=request.session_id, trace_id=trace_id, reply=reply)

        # Golden knowledge retrieval and dynamic persona storage are design-only in this prototype.
        t_ret = time.perf_counter()
        schema_summary = await self._warehouse.get_all_schemas_summary()
        self._obs.record_span(
            trace_id,
            "schema_retrieval",
            (time.perf_counter() - t_ret) * 1000,
        )

        # 6. LangGraph Workflow (SQL Generation + Safety Validation + Self-Correction Retry + Executive Synthesis)
        workflow_state = await self._analysis_graph.ainvoke(
            {
                "user_message": request.message,
                "user_context": user,
                "history": history,
                "schema_summary": schema_summary,
                "preferences": prefs,
                "trace_id": trace_id,
                "attempt": 1,
            }
        )

        fatal = workflow_state.get("fatal_error")
        if fatal is not None:
            raise fatal

        query_result = workflow_state.get("query_result")
        synthesis = workflow_state.get("synthesis")
        if synthesis is None:
            raise QueryExecutionError("Analysis workflow completed without synthesis output.")

        pii_redactions_total = workflow_state.get("redactions_count", 0)

        used_metrics_list = list(synthesis.used_metric_definitions)
        if not used_metrics_list:
            detected = self._glossary.detect_metrics(
                request.message + " " + (query_result.sql_executed if query_result else "")
            )
            used_metrics_list = [
                f"{m.name}: {m.description} (Formula: {m.sql_formula})" for m in detected
            ]

        combined_md = f"{synthesis.executive_summary}\n\n{synthesis.detailed_analysis}"
        if used_metrics_list and "Metric Definitions Applied" not in combined_md:
            detected_metrics = self._glossary.detect_metrics(
                request.message + " " + (query_result.sql_executed if query_result else "")
            )
            defs_block = self._glossary.format_response_definitions(detected_metrics or None)
            if defs_block:
                combined_md += f"\n\n{defs_block}"

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
            report_title = self._mask_text(
                synthesis.report_title or f"Retail Analysis: {request.message[:60]}"
            )
            safe_sql = self._mask_text(query_result.sql_executed) if query_result else None
            saved = await self._reports.save_report(
                SavedReport(
                    owner_user_id=user.user_id,
                    session_id=request.session_id,
                    title=report_title,
                    summary=self._masker.mask_text(synthesis.executive_summary)[0],
                    content_markdown=masked_reply,
                    action_items=masked_actions,
                    sql_used=safe_sql,
                    metric_definitions=used_metrics_list,
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
            sql_used=self._mask_text(query_result.sql_executed) if query_result else None,
            data_preview=(query_result.rows[:10] if query_result else []),
            row_count=query_result.row_count if query_result else 0,
            saved_report_id=saved_report_id,
            used_metric_definitions=used_metrics_list,
            observability={
                "trace_id": trace_id,
                "total_latency_ms": total_ms,
                "sql_attempts": query_result.attempts if query_result else 0,
                "from_cache": query_result.from_cache if query_result else False,
                "pii_redactions": pii_redactions_total,
            },
        )

    def _mask_text(self, text: str) -> str:
        """Return text safe for persistence in reports or exposure to a user."""
        return self._masker.mask_text(text)[0]
