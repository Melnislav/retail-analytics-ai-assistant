"""Tests for Circuit Breaker, LLM failover with OpenRouter, and CLI client resilience."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter, LLMProvider
from app.domain.circuit_breaker import CircuitBreaker, CircuitState
from app.domain.models import (
    GuardrailDecision,
    IntentType,
    SQLPlan,
)


def test_circuit_breaker_lifecycle() -> None:
    cb = CircuitBreaker(name="test_provider", failure_threshold=2, recovery_timeout_seconds=0.1)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True

    # 1. First failure -> below threshold, remains CLOSED
    cb.record_failure(RuntimeError("Transient error"))
    assert cb.failure_count == 1
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute() is True

    # 2. Second failure -> meets threshold, trips to OPEN
    cb.record_failure(RuntimeError("503 Service Unavailable"))
    assert cb.failure_count == 2
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False

    # 3. Before recovery timeout, execution blocked
    time.sleep(0.02)
    assert cb.can_execute() is False

    # 4. After recovery timeout, transitions to HALF_OPEN to allow canary probe
    time.sleep(0.12)
    assert cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # 5. Success in HALF_OPEN resets circuit back to CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0
    assert cb.can_execute() is True


def test_circuit_breaker_trips_immediately_on_half_open_failure() -> None:
    cb = CircuitBreaker(name="test_canary", failure_threshold=3, recovery_timeout_seconds=0.05)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.06)
    assert cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    # Failure during canary probe immediately re-opens circuit
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.can_execute() is False


@pytest.mark.asyncio
async def test_langchain_adapter_failover_to_openrouter() -> None:
    adapter = LangChainRetailAgentAdapter(
        provider="gemini",
        google_api_key="mock_key",
        openrouter_api_key="mock_key",
        timeout_seconds=2,
        circuit_breaker_failure_threshold=2,
        circuit_breaker_recovery_timeout=60.0,
    )

    # Mock primary provider (gemini) to fail with 503
    mock_gemini_model = MagicMock()
    mock_gemini_structured = MagicMock()
    mock_gemini_structured.ainvoke = AsyncMock(
        side_effect=Exception("503 This model is currently experiencing high demand")
    )
    mock_gemini_model.with_structured_output.return_value = mock_gemini_structured

    # Mock fallback provider (openrouter) to succeed
    expected_decision = GuardrailDecision(
        intent=IntentType.DATA_ANALYSIS,
        is_safe=True,
    )
    mock_openrouter_model = MagicMock()
    mock_openrouter_structured = MagicMock()
    mock_openrouter_structured.ainvoke = AsyncMock(return_value=expected_decision)
    mock_openrouter_model.with_structured_output.return_value = mock_openrouter_structured

    gemini_cb = CircuitBreaker("gemini", failure_threshold=2, recovery_timeout_seconds=60.0)
    openrouter_cb = CircuitBreaker("openrouter", failure_threshold=2, recovery_timeout_seconds=60.0)

    adapter._providers = [
        LLMProvider(name="gemini", model=mock_gemini_model, circuit_breaker=gemini_cb),
        LLMProvider(name="openrouter", model=mock_openrouter_model, circuit_breaker=openrouter_cb),
    ]

    # Call classify_and_guard -> gemini fails, openrouter succeeds
    decision = await adapter.classify_and_guard("How many items were sold last month?", [])
    assert decision.intent == IntentType.DATA_ANALYSIS
    assert decision.is_safe is True
    assert gemini_cb.failure_count == 1
    assert gemini_cb.state == CircuitState.CLOSED
    assert openrouter_cb.failure_count == 0

    # Second call -> gemini fails again, trips circuit to OPEN
    decision2 = await adapter.classify_and_guard("Show revenue by category", [])
    assert decision2.intent == IntentType.DATA_ANALYSIS
    assert gemini_cb.failure_count == 2
    assert gemini_cb.state == CircuitState.OPEN

    # Reset mock call counts to observe circuit breaker bypass
    mock_gemini_structured.ainvoke.reset_mock()
    mock_openrouter_structured.ainvoke.reset_mock()

    # Third call -> gemini circuit is OPEN, so gemini is skipped completely!
    decision3 = await adapter.classify_and_guard("What is the top state by customer spend?", [])
    assert decision3.intent == IntentType.DATA_ANALYSIS
    mock_gemini_structured.ainvoke.assert_not_called()
    mock_openrouter_structured.ainvoke.assert_called_once()


@pytest.mark.asyncio
async def test_langchain_adapter_timeout_triggers_failover() -> None:
    adapter = LangChainRetailAgentAdapter(
        provider="gemini",
        google_api_key="mock_key",
        openrouter_api_key="mock_key",
        timeout_seconds=1,  # 1 second hard deadline
        circuit_breaker_failure_threshold=1,
    )

    # Primary model hangs (exceeding 1s timeout)
    async def hanging_call(*args, **kwargs):
        await asyncio.sleep(5)
        return SQLPlan(reasoning="Hanging", sql_query="SELECT 1", requires_sql=True)

    mock_gemini_model = MagicMock()
    mock_gemini_structured = MagicMock()
    mock_gemini_structured.ainvoke = hanging_call
    mock_gemini_model.with_structured_output.return_value = mock_gemini_structured

    # Fallback model responds quickly
    expected_plan = SQLPlan(
        reasoning="Fast OpenRouter response",
        sql_query="SELECT count(*) FROM orders",
        requires_sql=True,
    )
    mock_openrouter_model = MagicMock()
    mock_openrouter_structured = MagicMock()
    mock_openrouter_structured.ainvoke = AsyncMock(return_value=expected_plan)
    mock_openrouter_model.with_structured_output.return_value = mock_openrouter_structured

    gemini_cb = CircuitBreaker("gemini", failure_threshold=1, recovery_timeout_seconds=60.0)
    openrouter_cb = CircuitBreaker("openrouter", failure_threshold=2, recovery_timeout_seconds=60.0)

    adapter._providers = [
        LLMProvider(name="gemini", model=mock_gemini_model, circuit_breaker=gemini_cb),
        LLMProvider(name="openrouter", model=mock_openrouter_model, circuit_breaker=openrouter_cb),
    ]

    t0 = time.monotonic()
    plan = await adapter.generate_sql_plan(
        user_message="Orders count",
        schema_summary="orders",
        golden_trios=[],
        user_context=MagicMock(),
        history=[],
    )
    elapsed = time.monotonic() - t0

    # Must complete in ~1.x seconds (1s timeout for gemini + instant openrouter)
    assert elapsed < 3.0
    assert plan.reasoning == "Fast OpenRouter response"
    assert gemini_cb.state == CircuitState.OPEN


@pytest.mark.asyncio
async def test_langchain_adapter_all_failed_uses_heuristic() -> None:
    adapter = LangChainRetailAgentAdapter(
        provider="gemini",
        google_api_key="mock_key",
        timeout_seconds=1,
    )

    # Provider that fails
    mock_model = MagicMock()
    mock_structured = MagicMock()
    mock_structured.ainvoke = AsyncMock(side_effect=RuntimeError("Total outage"))
    mock_model.with_structured_output.return_value = mock_structured

    cb = CircuitBreaker("gemini", failure_threshold=1, recovery_timeout_seconds=60.0)
    adapter._providers = [LLMProvider(name="gemini", model=mock_model, circuit_breaker=cb)]

    # classify_and_guard should fallback to heuristic
    decision = await adapter.classify_and_guard("Tell me a joke", [])
    assert decision.intent == IntentType.BLOCKED_OFF_TOPIC
    assert decision.is_safe is False


@pytest.mark.asyncio
async def test_cli_client_handles_timeout_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = RetailAssistantCLI(base_url="http://test-server:8000")

    # Simulate user sending one message then exiting
    inputs = iter(["how many items were sold?", "/exit"])

    def fake_ask(*args, **kwargs):
        return next(inputs)

    monkeypatch.setattr("rich.prompt.Prompt.ask", fake_ask)

    # Simulate httpx.AsyncClient.post raising TimeoutException
    async def fake_post(*args, **kwargs):
        raise httpx.ReadTimeout("Read timed out")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # Running CLI should not raise unhandled httpx.ReadTimeout exception
    try:
        await asyncio.wait_for(cli.run(), timeout=5.0)
    except httpx.TimeoutException:
        pytest.fail("CLI crashed with unhandled httpx.TimeoutException")
