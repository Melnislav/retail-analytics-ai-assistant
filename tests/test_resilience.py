"""Tests for LangChain-native provider fallback and CLI resilience."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.adapters.outbound.bigquery_adapter import BigQueryRunner
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter, LLMProvider
from app.domain.budget import TurnBudget, turn_budget_scope
from app.domain.exceptions import (
    ExternalServiceUnavailableError,
    QueryBudgetExceededError,
    QueryExecutionError,
    SQLSafetyError,
    TurnBudgetExceededError,
)
from app.domain.models import (
    AnalystSynthesis,
    ChatMessage,
    ChatRequest,
    GuardrailDecision,
    IntentType,
    QueryExecutionResult,
    SQLPlan,
    UserContext,
    UserRole,
)
from app.domain.sql_policy import SQLPolicyEngine
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer


@pytest.mark.asyncio
async def test_langchain_native_fallback_uses_second_provider() -> None:
    adapter = LangChainRetailAgentAdapter()
    calls: list[str] = []

    async def fail_primary(_messages: object) -> GuardrailDecision:
        calls.append("primary")
        raise RuntimeError("primary unavailable")

    expected = GuardrailDecision(intent=IntentType.DATA_ANALYSIS, is_safe=True)

    async def use_fallback(_messages: object) -> GuardrailDecision:
        calls.append("fallback")
        return expected

    primary = MagicMock()
    primary.with_structured_output.return_value = RunnableLambda(fail_primary)
    fallback = MagicMock()
    fallback.with_structured_output.return_value = RunnableLambda(use_fallback)
    adapter._providers = [
        LLMProvider(name="primary", model=primary),
        LLMProvider(name="fallback", model=fallback),
    ]

    decision = await adapter.classify_and_guard("Show revenue by brand", [])

    assert decision == expected
    assert calls == ["primary", "fallback"]


@pytest.mark.asyncio
async def test_langchain_native_fallback_marks_budget_fallback_used() -> None:
    adapter = LangChainRetailAgentAdapter()

    async def fail_primary(_messages: object) -> GuardrailDecision:
        raise RuntimeError("primary unavailable")

    expected = GuardrailDecision(intent=IntentType.DATA_ANALYSIS, is_safe=True)

    async def use_fallback(_messages: object) -> GuardrailDecision:
        return expected

    primary = MagicMock()
    primary.with_structured_output.return_value = RunnableLambda(fail_primary)
    fallback = MagicMock()
    fallback.with_structured_output.return_value = RunnableLambda(use_fallback)
    adapter._providers = [
        LLMProvider(name="primary", model=primary),
        LLMProvider(name="fallback", model=fallback),
    ]

    budget = TurnBudget(max_llm_calls=5, max_cost_usd=1.0)
    with turn_budget_scope(budget):
        decision = await adapter.classify_and_guard("Show revenue by brand", [])

    assert decision == expected
    assert budget.fallback_used is True

    # Primary succeeding must NOT set fallback_used
    async def ok_primary(_messages: object) -> GuardrailDecision:
        return expected

    primary_ok = MagicMock()
    primary_ok.with_structured_output.return_value = RunnableLambda(ok_primary)
    adapter._providers = [
        LLMProvider(name="primary", model=primary_ok),
        LLMProvider(name="fallback", model=fallback),
    ]
    clean_budget = TurnBudget(max_llm_calls=5, max_cost_usd=1.0)
    with turn_budget_scope(clean_budget):
        await adapter.classify_and_guard("Show revenue by brand", [])
    assert clean_budget.fallback_used is False


@pytest.mark.asyncio
async def test_langchain_all_failed_uses_heuristic() -> None:
    adapter = LangChainRetailAgentAdapter()

    async def fail(_messages: object) -> GuardrailDecision:
        raise RuntimeError("total outage")

    model = MagicMock()
    model.with_structured_output.return_value = RunnableLambda(fail)
    adapter._providers = [LLMProvider(name="primary", model=model)]

    decision = await adapter.classify_and_guard("Tell me a joke", [])

    assert decision.intent == IntentType.BLOCKED_OFF_TOPIC
    assert decision.is_safe is False


@pytest.mark.asyncio
async def test_langchain_all_failed_fails_closed_for_non_retail_question() -> None:
    adapter = LangChainRetailAgentAdapter()

    async def fail(_messages: object) -> GuardrailDecision:
        raise RuntimeError("total outage")

    model = MagicMock()
    model.with_structured_output.return_value = RunnableLambda(fail)
    adapter._providers = [LLMProvider(name="primary", model=model)]

    decision = await adapter.classify_and_guard("What is the capital of France?", [])

    assert decision.intent == IntentType.BLOCKED_OFF_TOPIC
    assert decision.is_safe is False
    assert decision.rejection_reason is not None


@pytest.mark.asyncio
async def test_langchain_all_failed_allows_retail_question() -> None:
    adapter = LangChainRetailAgentAdapter()

    async def fail(_messages: object) -> GuardrailDecision:
        raise RuntimeError("total outage")

    model = MagicMock()
    model.with_structured_output.return_value = RunnableLambda(fail)
    adapter._providers = [LLMProvider(name="primary", model=model)]

    decision = await adapter.classify_and_guard("What was revenue by brand last quarter?", [])

    assert decision.intent == IntentType.DATA_ANALYSIS
    assert decision.is_safe is True


@pytest.mark.asyncio
async def test_langchain_all_failed_allows_followup_with_history() -> None:
    adapter = LangChainRetailAgentAdapter()

    async def fail(_messages: object) -> GuardrailDecision:
        raise RuntimeError("total outage")

    model = MagicMock()
    model.with_structured_output.return_value = RunnableLambda(fail)
    adapter._providers = [LLMProvider(name="primary", model=model)]
    history = [ChatMessage(role="assistant", content="Revenue by brand is trending up.")]

    decision = await adapter.classify_and_guard("and by region?", history)

    assert decision.intent == IntentType.DATA_ANALYSIS
    assert decision.is_safe is True


@pytest.mark.asyncio
async def test_cli_client_handles_timeout_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = RetailAssistantCLI(base_url="http://test-server:8000")
    inputs = iter(["how many items were sold?", "/exit"])

    def fake_ask(*_args: object, **_kwargs: object) -> str:
        return next(inputs)

    async def fake_post(*_args: object, **_kwargs: object) -> None:
        raise httpx.ReadTimeout("Read timed out")

    monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    try:
        await asyncio.wait_for(cli.run(), timeout=5.0)
    except httpx.TimeoutException:
        pytest.fail("CLI crashed with unhandled httpx.TimeoutException")


def test_default_gemini_model_is_2_5_flash(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert Settings().gemini_model == "gemini-2.5-flash"
    import inspect

    sig = inspect.signature(LangChainRetailAgentAdapter.__init__)
    assert sig.parameters["gemini_model"].default == "gemini-2.5-flash"


def test_sql_policy_classifies_hard_and_soft_violations() -> None:
    policy = SQLPolicyEngine(demographic_min_group_size=5)
    ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)

    hard_dml = policy.validate_and_secure("DROP TABLE orders", ceo)
    hard_pii = policy.validate_and_secure("SELECT email FROM users", ceo)
    hard_link = policy.validate_and_secure(
        "SELECT u.id, u.state FROM users u GROUP BY u.id, u.state HAVING COUNT(DISTINCT u.id) >= 5",
        ceo,
    )
    soft_wildcard = policy.validate_and_secure("SELECT * FROM products", ceo)
    soft_demographics = policy.validate_and_secure(
        "SELECT u.state, COUNT(DISTINCT u.id) AS cnt FROM users u GROUP BY u.state",
        ceo,
    )

    assert not hard_dml.is_valid and hard_dml.severity == "hard"
    assert not hard_pii.is_valid and hard_pii.severity == "hard"
    assert not hard_link.is_valid and hard_link.severity == "hard"
    assert not soft_wildcard.is_valid and soft_wildcard.severity == "soft"
    assert not soft_demographics.is_valid and soft_demographics.severity == "soft"


def _resilience_test_settings(tmp_path: Path, db_name: str, **overrides: object) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/{db_name}",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
        **overrides,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_differentiated_sql_retries_and_empty_result_synthesis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)

    # 1. Hard violation -> 0 retries (fail fast on 1st SQL attempt)
    hard_container = ApplicationContainer(_resilience_test_settings(tmp_path, "hard.db"))
    await hard_container.startup()
    hard_calls = 0

    async def hard_sql_plan(**_kwargs: object) -> SQLPlan:
        nonlocal hard_calls
        hard_calls += 1
        return SQLPlan(reasoning="bad", sql_query="SELECT email FROM users", requires_sql=True)

    monkeypatch.setattr(hard_container.chat_orchestrator._llm, "generate_sql_plan", hard_sql_plan)
    try:
        with pytest.raises(SQLSafetyError):
            await hard_container.chat_orchestrator.handle_message(
                ChatRequest(session_id="s_hard", message="Show revenue by category"),
                ceo,
            )
        assert hard_calls == 1
    finally:
        await hard_container.shutdown()

    # 2. Soft violation -> max 1 repair (2 total calls)
    soft_container = ApplicationContainer(_resilience_test_settings(tmp_path, "soft.db"))
    await soft_container.startup()
    soft_calls = 0

    async def soft_sql_plan(**_kwargs: object) -> SQLPlan:
        nonlocal soft_calls
        soft_calls += 1
        return SQLPlan(reasoning="wildcard", sql_query="SELECT * FROM products", requires_sql=True)

    monkeypatch.setattr(soft_container.chat_orchestrator._llm, "generate_sql_plan", soft_sql_plan)
    try:
        with pytest.raises(SQLSafetyError):
            await soft_container.chat_orchestrator.handle_message(
                ChatRequest(session_id="s_soft", message="Show revenue by category"),
                ceo,
            )
        assert soft_calls == 2
    finally:
        await soft_container.shutdown()

    # 3. BigQuery syntax error -> max 2 repairs (3 total calls)
    syn_container = ApplicationContainer(_resilience_test_settings(tmp_path, "syntax.db"))
    await syn_container.startup()
    syntax_calls = 0

    async def syntax_sql_plan(**_kwargs: object) -> SQLPlan:
        nonlocal syntax_calls
        syntax_calls += 1
        return SQLPlan(
            reasoning="broken sql",
            sql_query="SELECT category FROM products",
            requires_sql=True,
        )

    async def fail_bq(_sql: str) -> QueryExecutionResult:
        raise ValueError("Syntax error near SELECT")

    monkeypatch.setattr(syn_container.chat_orchestrator._llm, "generate_sql_plan", syntax_sql_plan)
    monkeypatch.setattr(syn_container.chat_orchestrator._warehouse, "execute_query", fail_bq)
    try:
        with pytest.raises(QueryExecutionError):
            await syn_container.chat_orchestrator.handle_message(
                ChatRequest(session_id="s_syn", message="Show revenue by category"),
                ceo,
            )
        assert syntax_calls == 3
    finally:
        await syn_container.shutdown()

    # 4. Empty result set (0 rows) -> max 1 repair (2 calls) + honest explanation (not capability overview)
    empty_container = ApplicationContainer(_resilience_test_settings(tmp_path, "empty.db"))
    await empty_container.startup()
    empty_calls = 0

    async def empty_sql_plan(**_kwargs: object) -> SQLPlan:
        nonlocal empty_calls
        empty_calls += 1
        return SQLPlan(
            reasoning="valid sql",
            sql_query="SELECT category FROM products WHERE category = 'Nonexistent'",
            requires_sql=True,
        )

    async def empty_bq(sql: str) -> QueryExecutionResult:
        return QueryExecutionResult(sql_executed=sql, rows=[], row_count=0, columns=["category"])

    monkeypatch.setattr(empty_container.chat_orchestrator._llm, "generate_sql_plan", empty_sql_plan)
    monkeypatch.setattr(empty_container.chat_orchestrator._warehouse, "execute_query", empty_bq)
    try:
        resp = await empty_container.chat_orchestrator.handle_message(
            ChatRequest(session_id="s_empty", message="Show revenue for Nonexistent category"),
            ceo,
        )
        assert empty_calls == 2
        assert resp.row_count == 0
        assert "no matching records" in resp.reply.lower()
        assert "available dataset" not in resp.reply.lower()
    finally:
        await empty_container.shutdown()


@pytest.mark.asyncio
async def test_bigquery_dry_run_enforces_byte_budget_with_zero_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    query_configs: list[object] = []

    def fake_query(_sql: str, job_config: object = None) -> MagicMock:
        query_configs.append(job_config)
        job = MagicMock()
        job.total_bytes_processed = 5_000_000
        return job

    fake_client.query.side_effect = fake_query
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda project=None: fake_client)

    runner = BigQueryRunner(project_id="test-proj", max_bytes_billed=1_000_000)
    with pytest.raises(QueryBudgetExceededError, match="exceeds the configured byte limit"):
        runner.execute_query("SELECT id FROM products")
    assert len(query_configs) == 1
    assert getattr(query_configs[0], "dry_run", False) is True

    # In orchestrator: QueryBudgetExceededError stops immediately (0 retries) with friendly reply.
    container = ApplicationContainer(_resilience_test_settings(tmp_path, "bq_budget.db"))
    await container.startup()
    sql_calls = 0

    async def counting_sql_plan(**_kwargs: object) -> SQLPlan:
        nonlocal sql_calls
        sql_calls += 1
        return SQLPlan(
            reasoning="expensive query",
            sql_query="SELECT category FROM products",
            requires_sql=True,
        )

    async def budget_fail_bq(_sql: str) -> QueryExecutionResult:
        raise QueryBudgetExceededError("Query scan estimate (5,000,000 bytes) exceeds limit.")

    monkeypatch.setattr(container.chat_orchestrator._llm, "generate_sql_plan", counting_sql_plan)
    monkeypatch.setattr(container.chat_orchestrator._warehouse, "execute_query", budget_fail_bq)
    try:
        ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)
        resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="s_bq_budget", message="Show revenue by category"),
            ceo,
        )
        assert sql_calls == 1
        assert resp.observability["bq_budget_exceeded"] is True
        assert "narrow the date range" in resp.reply.lower()
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_turn_budget_guard_and_usage_metadata_tracking(tmp_path: Path) -> None:
    budget = TurnBudget(max_llm_calls=5, max_cost_usd=1.0)
    with pytest.raises(TurnBudgetExceededError):
        for _ in range(6):
            budget.record_call()

    # Adapter unpacks include_raw=True usage_metadata into active TurnBudget.
    adapter = LangChainRetailAgentAdapter(gemini_model="gemini-2.5-flash")
    raw_msg = AIMessage(
        content="ok",
        usage_metadata={
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "total_tokens": 2_000_000,
        },
        response_metadata={"model_name": "gemini-2.5-flash"},
    )
    parsed_syn = AnalystSynthesis(executive_summary="Done", detailed_analysis="Details")

    async def fake_structured(_msgs: object) -> dict[str, object]:
        return {"raw": raw_msg, "parsed": parsed_syn, "parsing_error": None}

    mock_model = MagicMock()
    mock_model.with_structured_output.return_value = RunnableLambda(fake_structured)
    adapter._providers = [
        LLMProvider(name="gemini", model=mock_model, model_name="gemini-2.5-flash")
    ]

    tracked_budget = TurnBudget(max_llm_calls=5, max_cost_usd=1.0)
    with turn_budget_scope(tracked_budget):
        res = await adapter.synthesize_response(
            user_message="Show revenue",
            query_result=None,
            persona=SimpleNamespace(tone_instructions="Concise"),  # type: ignore[arg-type]
            preferences=SimpleNamespace(
                preferred_format=SimpleNamespace(value="auto"),
                preferred_depth=SimpleNamespace(value="standard"),
            ),  # type: ignore[arg-type]
            history=[],
        )
    assert res.executive_summary == "Done"
    assert tracked_budget.input_tokens == 1_000_000
    assert tracked_budget.output_tokens == 1_000_000
    assert tracked_budget.total_cost_usd == pytest.approx(0.75)

    # Orchestrator rejects gracefully if max_llm_calls_per_turn is exhausted.
    container = ApplicationContainer(
        _resilience_test_settings(tmp_path, "turn_budget.db", max_llm_calls_per_turn=2)
    )
    await container.startup()
    try:
        ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)
        resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="s_turn_budget", message="Show revenue by category"),
            ceo,
        )
        assert resp.observability["budget_exceeded"] is True
        assert "budget reached" in resp.reply.lower()
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_standalone_cli_handles_domain_and_unexpected_errors_gracefully(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    container = ApplicationContainer(_resilience_test_settings(tmp_path, "cli_err.db"))
    await container.startup()
    cli = RetailAssistantCLI(user_key="brand_manager_alice", standalone=True)
    cli.container = container

    async def raise_safety(_req: object, _user: object) -> object:
        raise SQLSafetyError("Blocked by AST policy")

    async def raise_query_err(_req: object, _user: object) -> object:
        raise QueryExecutionError("Syntax error after 3 attempts")

    async def raise_unexpected(_req: object, _user: object) -> object:
        raise RuntimeError("boom")

    try:
        monkeypatch.setattr(container.chat_orchestrator, "handle_message", raise_safety)
        assert await cli._send_chat(None, "test", False) is None
        assert "Security / Policy Refusal" in capsys.readouterr().out

        monkeypatch.setattr(container.chat_orchestrator, "handle_message", raise_query_err)
        assert await cli._send_chat(None, "test", False) is None
        assert "Query Execution Error" in capsys.readouterr().out

        monkeypatch.setattr(container.chat_orchestrator, "confirm_action", raise_unexpected)
        assert await cli._confirm_action(None, "confirm_abc123", True) is None
        assert "Unexpected Error" in capsys.readouterr().out
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_bigquery_infrastructure_errors_fail_fast_without_sql_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ServiceUnavailable(Exception):
        pass

    ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)

    for idx, exc in enumerate(
        (
            TimeoutError("BigQuery timed out"),
            ServiceUnavailable("503 backend unavailable"),
        )
    ):
        container = ApplicationContainer(_resilience_test_settings(tmp_path, f"infra_{idx}.db"))
        await container.startup()
        sql_calls = 0

        async def counting_sql_plan(**_kwargs: object) -> SQLPlan:
            nonlocal sql_calls
            sql_calls += 1
            return SQLPlan(
                reasoning="valid query",
                sql_query="SELECT category FROM products",
                requires_sql=True,
            )

        async def infra_fail_bq(_sql: str, err: Exception = exc) -> QueryExecutionResult:
            raise err

        monkeypatch.setattr(
            container.chat_orchestrator._llm, "generate_sql_plan", counting_sql_plan
        )
        monkeypatch.setattr(container.chat_orchestrator._warehouse, "execute_query", infra_fail_bq)
        try:
            with pytest.raises(ExternalServiceUnavailableError):
                await container.chat_orchestrator.handle_message(
                    ChatRequest(session_id="s_infra", message="Show revenue by category"),
                    ceo,
                )
            assert sql_calls == 1
            counters = container.observability.get_metrics_snapshot()["counters"]
            assert counters["warehouse_unavailable_total"] == 1
        finally:
            await container.shutdown()


@pytest.mark.asyncio
async def test_redis_adapter_trips_cooldown_after_connection_failure() -> None:
    from app.adapters.outbound.redis_and_telemetry import AsyncRedisAdapter

    adapter = AsyncRedisAdapter(
        redis_url="redis://localhost:6379/0",
        failure_cooldown_seconds=60.0,
    )
    fake_redis = MagicMock()
    calls = 0

    async def fail_get(_key: str) -> None:
        nonlocal calls
        calls += 1
        raise ConnectionRefusedError("Redis unreachable")

    fake_redis.get = fail_get
    adapter._redis = fake_redis

    # First call hits fake_redis once, fails, and trips cooldown
    assert await adapter.get_json("k1") is None
    assert calls == 1

    # Subsequent calls during cooldown bypass Redis entirely and use in-memory store
    await adapter.set_json("k2", {"ok": True}, ttl_seconds=60)
    assert await adapter.get_json("k2") == {"ok": True}
    assert calls == 1
