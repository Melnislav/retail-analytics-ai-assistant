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
from app.domain.budget import TurnBudget, turn_budget_scope
from app.domain.exceptions import (
    ConfirmationValidationError,
    QueryBudgetExceededError,
    QueryExecutionError,
    SQLSafetyError,
    TurnBudgetExceededError,
)
from app.domain.input_safety import InputSafetyPolicy
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
    TraceRecord,
    UserContext,
    UserPreferenceProfile,
)
from app.domain.pii_masker import PIIMasker
from app.domain.post_sql_guard import PostSQLDataGuard
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
    soft_retries: int
    syntax_retries: int
    empty_retries: int
    budget: TurnBudget
    trace_meta: dict[str, Any]
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
        max_llm_calls_per_turn: int = 5,
        max_turn_cost_usd: float = 1.0,
        cache_ttl_seconds: int = 300,
        chat_history_limit: int = 10,
        default_persona_tone: str = PersonaConfig().tone_instructions,
        glossary: SemanticGlossary | None = None,
        input_safety: InputSafetyPolicy | None = None,
        post_sql_guard: PostSQLDataGuard | None = None,
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
        self._max_llm_calls_per_turn = max_llm_calls_per_turn
        self._max_turn_cost_usd = max_turn_cost_usd
        self._cache_ttl = cache_ttl_seconds
        self._chat_history_limit = chat_history_limit
        self._glossary = glossary or SemanticGlossary()
        self._input_safety = input_safety or InputSafetyPolicy()
        self._post_sql_guard = post_sql_guard or PostSQLDataGuard()
        self._analysis_graph = self._build_analysis_graph()

    def _build_analysis_graph(self) -> CompiledStateGraph:
        workflow = StateGraph(AnalysisWorkflowState)

        async def generate_sql_node(state: AnalysisWorkflowState) -> dict[str, Any]:
            t_sql = time.perf_counter()
            attempt = state.get("attempt", 1)
            trace_id = state.get("trace_id", "")
            budget = state.get("budget")
            if budget is not None:
                budget.check_can_call()
            plan = await self._llm.generate_sql_plan(
                user_message=state["user_message"],
                schema_summary=state["schema_summary"],
                user_context=state["user_context"],
                history=state["history"],
                previous_error=state.get("last_error"),
                previous_sql=state.get("last_sql"),
            )
            if budget is not None:
                budget.record_call()
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

            applied_defs = list(plan.applied_metric_definitions) if plan else []
            attempt = state.get("attempt", 1)
            soft_retries = state.get("soft_retries", 0)
            syntax_retries = state.get("syntax_retries", 0)
            empty_retries = state.get("empty_retries", 0)
            trace_id = state.get("trace_id", "")
            user = state["user_context"]
            max_additional_attempts = max(0, self._max_sql_retries - 1)

            validation = self._sql_policy.validate_and_secure(plan.sql_query, user)
            trace_meta = state.get("trace_meta")
            if trace_meta is not None:
                trace_meta["generated_sql"] = (
                    validation.sanitized_sql if validation.is_valid else plan.sql_query
                )
                trace_meta["ast_valid"] = validation.is_valid
            if not validation.is_valid:
                error_msg = f"SQL Policy Violation: {'; '.join(validation.violations)}"
                if validation.severity == "hard":
                    self._obs.increment_metric("sql_hard_policy_blocks_total")
                    logger.warning(
                        "Attempt %d hard SQL policy block (0 retries): %s", attempt, error_msg
                    )
                    return {
                        "last_error": error_msg,
                        "last_sql": plan.sql_query,
                        "fatal_error": SQLSafetyError(error_msg),
                    }

                soft_limit = min(1, max_additional_attempts)
                if soft_retries >= soft_limit:
                    logger.warning(
                        "Soft SQL formatting limit reached (%d): %s", soft_retries, error_msg
                    )
                    return {
                        "last_error": error_msg,
                        "last_sql": plan.sql_query,
                        "fatal_error": SQLSafetyError(error_msg),
                    }
                self._obs.increment_metric("sql_self_correction_attempts")
                logger.warning(
                    "Attempt %d soft SQL policy repair (%d/%d): %s",
                    attempt,
                    soft_retries + 1,
                    soft_limit,
                    error_msg,
                )
                return {
                    "last_error": error_msg,
                    "last_sql": plan.sql_query,
                    "attempt": attempt + 1,
                    "soft_retries": soft_retries + 1,
                }

            secured_sql = validation.sanitized_sql
            sql_hash = hashlib.sha256(secured_sql.encode("utf-8")).hexdigest()[:16]
            cache_key = f"bq_cache:{sql_hash}"
            cached_payload = await self._cache.get_json(cache_key)
            if cached_payload is not None:
                cached_result = QueryExecutionResult(
                    **cached_payload, from_cache=True, attempts=attempt
                )
                if applied_defs and not cached_result.applied_metric_definitions:
                    cached_result = cached_result.model_copy(
                        update={"applied_metric_definitions": applied_defs}
                    )
                guarded_rows, guard_redactions = self._apply_post_sql_guard(cached_result.rows)
                masked_rows, row_redactions = self._masker.mask_rows(guarded_rows)
                if trace_meta is not None:
                    trace_meta["bq_bytes_billed"] = cached_result.bytes_processed
                self._obs.increment_metric("bq_cache_hits")
                return {
                    "query_result": cached_result.model_copy(update={"rows": masked_rows}),
                    "last_sql": secured_sql,
                    "redactions_count": (
                        state.get("redactions_count", 0) + guard_redactions + row_redactions
                    ),
                }

            try:
                t_bq = time.perf_counter()
                raw_result = await self._warehouse.execute_query(secured_sql)
                if trace_meta is not None:
                    trace_meta["bq_bytes_billed"] = raw_result.bytes_processed
                if trace_id:
                    self._obs.record_span(
                        trace_id,
                        f"bigquery_execution_attempt_{attempt}",
                        (time.perf_counter() - t_bq) * 1000,
                        {"row_count": raw_result.row_count},
                    )

                empty_limit = min(1, max_additional_attempts)
                if raw_result.row_count == 0 and empty_retries < empty_limit:
                    err_msg = (
                        "Query executed cleanly but returned 0 rows. Verify filter literals "
                        "(e.g., case sensitivity, date ranges, or status values) and broaden filters if appropriate."
                    )
                    self._obs.increment_metric("sql_self_correction_attempts")
                    return {
                        "last_error": err_msg,
                        "last_sql": secured_sql,
                        "attempt": attempt + 1,
                        "empty_retries": empty_retries + 1,
                    }

                guarded_rows, guard_redactions = self._apply_post_sql_guard(raw_result.rows)
                masked_rows, row_redactions = self._masker.mask_rows(guarded_rows)
                result = QueryExecutionResult(
                    sql_executed=secured_sql,
                    rows=masked_rows,
                    row_count=raw_result.row_count,
                    columns=raw_result.columns,
                    bytes_processed=raw_result.bytes_processed,
                    from_cache=False,
                    attempts=attempt,
                    applied_metric_definitions=applied_defs,
                )
                if result.row_count > 0:
                    await self._cache.set_json(
                        cache_key,
                        result.model_dump(exclude={"from_cache", "attempts"}),
                        ttl_seconds=self._cache_ttl,
                    )
                return {
                    "query_result": result,
                    "last_sql": secured_sql,
                    "redactions_count": (
                        state.get("redactions_count", 0) + guard_redactions + row_redactions
                    ),
                }
            except QueryBudgetExceededError as exc:
                self._obs.increment_metric("bq_budget_blocks_total")
                logger.warning("Query byte budget exceeded (0 retries): %s", exc.message)
                return {
                    "last_error": exc.message,
                    "last_sql": secured_sql,
                    "fatal_error": exc,
                }
            except Exception as exc:
                err_msg = str(exc)
                syntax_limit = min(2, max_additional_attempts)
                logger.warning("Attempt %d BigQuery error: %s", attempt, err_msg)
                if syntax_retries >= syntax_limit:
                    return {
                        "last_error": err_msg,
                        "last_sql": secured_sql,
                        "fatal_error": QueryExecutionError(
                            f"Unable to execute a valid analytical query after {attempt} attempt(s): {err_msg}",
                            last_sql=secured_sql,
                        ),
                    }
                self._obs.increment_metric("sql_self_correction_attempts")
                return {
                    "last_error": err_msg,
                    "last_sql": secured_sql,
                    "attempt": attempt + 1,
                    "syntax_retries": syntax_retries + 1,
                }

        async def synthesize_node(state: AnalysisWorkflowState) -> dict[str, Any]:
            t_syn = time.perf_counter()
            trace_id = state.get("trace_id", "")
            plan = state.get("sql_plan")
            if plan and plan.response_message:
                synthesis = AnalystSynthesis(
                    executive_summary=plan.response_message,
                    detailed_analysis="",
                )
            else:
                budget = state.get("budget")
                if budget is not None:
                    budget.check_can_call()
                synthesis = await self._llm.synthesize_response(
                    user_message=state["user_message"],
                    query_result=state.get("query_result"),
                    persona=self._persona,
                    preferences=state["preferences"],
                    history=state["history"],
                )
                if budget is not None:
                    budget.record_call()
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
            if state.get("last_error"):
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

        try:
            deleted_count = await self._reports.execute_confirmed_deletion(pending, user)
        except ConfirmationValidationError as exc:
            self._obs.increment_metric("destructive_confirmations_rejected")
            reply = self._mask_text(exc.message)
            await self._cache.append_chat_message(
                request.session_id, ChatMessage(role="assistant", content=reply)
            )
            return ChatResponse(
                session_id=request.session_id,
                trace_id=trace_id,
                reply=reply,
            )

        short_ids = ", ".join(f"`{rid[:8]}`" for rid in pending.target_report_ids)
        titles_str = ", ".join(f"'{t}'" for t in pending.target_report_titles)
        reply = self._mask_text(
            f"Confirmed and executed: deleted {deleted_count} report(s) [{short_ids}] ({titles_str})."
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
        safe_message = self._mask_text(request.message)
        self._obs.start_trace(trace_id, user.user_id, request.session_id, safe_message)
        self._obs.increment_metric("chat_requests_total")

        raw_history = await self._cache.get_chat_history(
            request.session_id, limit=self._chat_history_limit
        )
        history = [
            message.model_copy(update={"content": self._mask_text(message.content)})
            for message in raw_history
        ]
        await self._cache.append_chat_message(
            request.session_id, ChatMessage(role="user", content=safe_message)
        )

        budget = TurnBudget(
            max_llm_calls=self._max_llm_calls_per_turn,
            max_cost_usd=self._max_turn_cost_usd,
        )
        trace_meta: dict[str, Any] = {
            "intent_classified": "unknown",
            "generated_sql": None,
            "ast_valid": None,
            "bq_bytes_billed": 0,
            "error_type": None,
            "final_reply": "",
        }

        try:
            t_pre = time.perf_counter()
            pre_guard = self._input_safety.inspect(request.message)
            self._obs.record_span(
                trace_id,
                "pre_guard",
                (time.perf_counter() - t_pre) * 1000,
                {"is_safe": pre_guard.is_safe},
            )
            if not pre_guard.is_safe:
                self._obs.increment_metric("deterministic_guardrail_blocks_total")
                trace_meta["intent_classified"] = "blocked_pre_guard"
                trace_meta["error_type"] = "PreGuardBlocked"
                refusal = self._mask_text(
                    pre_guard.rejection_reason or "Request blocked by security policy."
                )
                trace_meta["final_reply"] = refusal
                await self._cache.append_chat_message(
                    request.session_id, ChatMessage(role="assistant", content=refusal)
                )
                return ChatResponse(
                    session_id=request.session_id,
                    trace_id=trace_id,
                    reply=refusal,
                    observability={"trace_id": trace_id, "blocked": True, "pre_guard": True},
                )

            try:
                with turn_budget_scope(budget):
                    # 1. Safety Guardrail & Intent Classification
                    budget.check_can_call()
                    t0 = time.perf_counter()
                    decision = await self._llm.classify_and_guard(safe_message, history)
                    budget.record_call()
                    trace_meta["intent_classified"] = decision.intent.value
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
                        trace_meta["error_type"] = decision.intent.value
                        refusal = self._mask_text(
                            decision.rejection_reason
                            or "I am an executive retail data analysis assistant and can only answer questions related to retail sales, customers, products, and saved reports."
                        )
                        trace_meta["final_reply"] = refusal
                        await self._cache.append_chat_message(
                            request.session_id, ChatMessage(role="assistant", content=refusal)
                        )
                        return ChatResponse(
                            session_id=request.session_id,
                            trace_id=trace_id,
                            reply=refusal,
                            observability={
                                "trace_id": trace_id,
                                "blocked": True,
                                **budget.to_dict(),
                            },
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
                            trace_meta["final_reply"] = reply
                            await self._cache.append_chat_message(
                                request.session_id, ChatMessage(role="assistant", content=reply)
                            )
                            return ChatResponse(
                                session_id=request.session_id,
                                trace_id=trace_id,
                                reply=reply,
                                observability={"trace_id": trace_id, **budget.to_dict()},
                            )

                        safe_pending = pending.model_copy(
                            update={
                                "description": self._mask_text(pending.description),
                                "target_report_titles": [
                                    self._mask_text(title) for title in pending.target_report_titles
                                ],
                            }
                        )
                        bullet_lines: list[str] = []
                        for idx, title in enumerate(safe_pending.target_report_titles):
                            rid = (
                                safe_pending.target_report_ids[idx][:8]
                                if idx < len(safe_pending.target_report_ids)
                                else "unknown"
                            )
                            created = (
                                safe_pending.target_report_created_at[idx]
                                if idx < len(safe_pending.target_report_created_at)
                                else ""
                            )
                            created_suffix = f" ({created})" if created else ""
                            bullet_lines.append(f"- `{rid}` — **{title}**{created_suffix}")
                        titles_bullets = "\n".join(bullet_lines)
                        hash_preview = (
                            safe_pending.payload_hash[:12] if safe_pending.payload_hash else "n/a"
                        )
                        reply = (
                            f"**Confirmation Required (Destructive Action)**\n"
                            f"{safe_pending.description}\n\n"
                            f"Matched reports:\n{titles_bullets}\n\n"
                            f"Snapshot hash: `{hash_preview}`\n"
                            f"Please confirm using token `{safe_pending.token}` (or approve in the prompt) before I permanently delete them."
                        )
                        trace_meta["final_reply"] = reply
                        await self._cache.append_chat_message(
                            request.session_id, ChatMessage(role="assistant", content=reply)
                        )
                        return ChatResponse(
                            session_id=request.session_id,
                            trace_id=trace_id,
                            reply=reply,
                            pending_confirmation=safe_pending,
                            observability={
                                "trace_id": trace_id,
                                "awaiting_confirmation": True,
                                **budget.to_dict(),
                            },
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
                            reply = self._mask_text(
                                "Here are your saved reports:\n" + "\n".join(lines)
                            )
                        trace_meta["final_reply"] = reply
                        await self._cache.append_chat_message(
                            request.session_id, ChatMessage(role="assistant", content=reply)
                        )
                        return ChatResponse(
                            session_id=request.session_id,
                            trace_id=trace_id,
                            reply=reply,
                            observability={"trace_id": trace_id, **budget.to_dict()},
                        )

                    # Golden knowledge retrieval and dynamic persona storage are design-only in this prototype.
                    t_ret = time.perf_counter()
                    schema_summary = await self._warehouse.get_all_schemas_summary()
                    self._obs.record_span(
                        trace_id,
                        "schema_retrieval",
                        (time.perf_counter() - t_ret) * 1000,
                    )

                    # 6. LangGraph Workflow (SQL Generation + Safety Validation + Differentiated Retry + Executive Synthesis)
                    workflow_state = await self._analysis_graph.ainvoke(
                        {
                            "user_message": safe_message,
                            "user_context": user,
                            "history": history,
                            "schema_summary": schema_summary,
                            "preferences": prefs,
                            "trace_id": trace_id,
                            "attempt": 1,
                            "soft_retries": 0,
                            "syntax_retries": 0,
                            "empty_retries": 0,
                            "budget": budget,
                            "trace_meta": trace_meta,
                        }
                    )

                    fatal = workflow_state.get("fatal_error")
                    if isinstance(fatal, QueryBudgetExceededError):
                        trace_meta["error_type"] = type(fatal).__name__
                        budget_reply = self._mask_text(
                            f"{fatal.message} Please narrow the date range or add specific category/brand filters."
                        )
                        trace_meta["final_reply"] = budget_reply
                        await self._cache.append_chat_message(
                            request.session_id, ChatMessage(role="assistant", content=budget_reply)
                        )
                        return ChatResponse(
                            session_id=request.session_id,
                            trace_id=trace_id,
                            reply=budget_reply,
                            sql_used=(
                                self._mask_text(workflow_state["last_sql"])
                                if workflow_state.get("last_sql")
                                else None
                            ),
                            observability={
                                "trace_id": trace_id,
                                "bq_budget_exceeded": True,
                                **budget.to_dict(),
                            },
                        )
                    if fatal is not None:
                        raise fatal
            except TurnBudgetExceededError as exc:
                trace_meta["error_type"] = type(exc).__name__
                self._obs.increment_metric("turn_budget_exceeded_total")
                budget_reply = self._mask_text(
                    f"Turn budget reached ({exc.message}). Please narrow your question and retry."
                )
                trace_meta["final_reply"] = budget_reply
                await self._cache.append_chat_message(
                    request.session_id, ChatMessage(role="assistant", content=budget_reply)
                )
                return ChatResponse(
                    session_id=request.session_id,
                    trace_id=trace_id,
                    reply=budget_reply,
                    observability={
                        "trace_id": trace_id,
                        "budget_exceeded": True,
                        **budget.to_dict(),
                    },
                )

            query_result = workflow_state.get("query_result")
            synthesis = workflow_state.get("synthesis")
            if synthesis is None:
                raise QueryExecutionError("Analysis workflow completed without synthesis output.")

            pii_redactions_total = workflow_state.get("redactions_count", 0)

            plan = workflow_state.get("sql_plan")
            has_clarification = bool(plan and plan.response_message)
            used_metrics_list = [] if has_clarification else list(synthesis.used_metric_definitions)
            if (
                not has_clarification
                and not used_metrics_list
                and query_result
                and query_result.applied_metric_definitions
            ):
                used_metrics_list = list(query_result.applied_metric_definitions)
            if not has_clarification and not used_metrics_list:
                detected = self._glossary.detect_metrics(
                    safe_message + " " + (query_result.sql_executed if query_result else "")
                )
                used_metrics_list = [
                    f"{m.name}: {m.description} (Formula: {m.sql_formula})" for m in detected
                ]
            combined_md = f"{synthesis.executive_summary}\n\n{synthesis.detailed_analysis}"
            if used_metrics_list and "Metric Definitions Applied" not in combined_md:
                detected_metrics = self._glossary.detect_metrics(
                    safe_message + " " + (query_result.sql_executed if query_result else "")
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
            safe_executed_sql = self._mask_text(query_result.sql_executed) if query_result else None
            if safe_executed_sql is not None:
                trace_meta["generated_sql"] = safe_executed_sql
            trace_meta["final_reply"] = masked_reply

            # 8. Optional Report Persistence
            saved_report_id: str | None = None
            if request.save_report or decision.should_save_report:
                report_title = self._mask_text(
                    synthesis.report_title or f"Retail Analysis: {safe_message[:60]}"
                )
                saved = await self._reports.save_report(
                    SavedReport(
                        owner_user_id=user.user_id,
                        session_id=request.session_id,
                        question=safe_message,
                        title=report_title,
                        summary=self._masker.mask_text(synthesis.executive_summary)[0],
                        content_markdown=masked_reply,
                        action_items=masked_actions,
                        sql_used=safe_executed_sql,
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
                sql_used=safe_executed_sql,
                data_preview=(query_result.rows[:10] if query_result else []),
                row_count=query_result.row_count if query_result else 0,
                saved_report_id=saved_report_id,
                used_metric_definitions=used_metrics_list,
                observability={
                    "trace_id": trace_id,
                    "total_latency_ms": total_ms,
                    "sql_attempts": query_result.attempts if query_result else 0,
                    "from_cache": query_result.from_cache if query_result else False,
                    "bytes_processed": query_result.bytes_processed if query_result else 0,
                    "pii_redactions": pii_redactions_total,
                    **budget.to_dict(),
                },
            )
        except Exception as exc:
            trace_meta["error_type"] = type(exc).__name__
            raise
        finally:
            total_ms = round((time.perf_counter() - t_start) * 1000, 2)
            raw_sql = trace_meta.get("generated_sql")
            self._obs.finish_trace(
                TraceRecord(
                    trace_id=trace_id,
                    user_id=user.user_id,
                    session_id=request.session_id,
                    intent_classified=str(trace_meta.get("intent_classified", "unknown")),
                    generated_sql=(self._mask_text(str(raw_sql)) if raw_sql is not None else None),
                    ast_valid=trace_meta.get("ast_valid"),
                    bq_bytes_billed=int(trace_meta.get("bq_bytes_billed") or 0),
                    llm_calls_count=budget.llm_calls,
                    input_tokens=budget.input_tokens,
                    output_tokens=budget.output_tokens,
                    total_cost_usd=round(budget.total_cost_usd, 6),
                    latency_ms=total_ms,
                    error_type=trace_meta.get("error_type"),
                    pii_redacted_prompt=safe_message,
                    pii_redacted_response=str(trace_meta.get("final_reply") or ""),
                    fallback_used=budget.fallback_used,
                )
            )

    def _apply_post_sql_guard(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Enforce demographic k-anonymity and non-linkage before caching or masking."""
        guard_result = self._post_sql_guard.enforce(rows)
        if guard_result.masked_groups_count > 0:
            self._obs.increment_metric("k_anonymity_masks_total", guard_result.masked_groups_count)
        if guard_result.stripped_demographic_cells > 0:
            self._obs.increment_metric(
                "demographic_link_redactions_total", guard_result.stripped_demographic_cells
            )
        return (
            guard_result.rows,
            guard_result.masked_groups_count + guard_result.stripped_demographic_cells,
        )

    def _mask_text(self, text: str) -> str:
        """Return text safe for persistence in reports or exposure to a user."""
        return self._masker.mask_text(text)[0]
