"""Tests for LangChain-native provider fallback and CLI resilience."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest
from langchain_core.runnables import RunnableLambda

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter, LLMProvider
from app.domain.models import GuardrailDecision, IntentType


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
