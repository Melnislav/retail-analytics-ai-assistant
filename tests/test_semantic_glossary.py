"""Unit and integration tests for the Retail Semantic Glossary (Recommendation #1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda

from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter, LLMProvider
from app.domain.models import ChatRequest, SQLPlan, UserContext, UserRole
from app.domain.semantic_glossary import DEFAULT_METRICS, MetricDefinition, SemanticGlossary
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer


def test_default_metric_definitions() -> None:
    expected_metrics = {
        "net_revenue",
        "gmv",
        "churn_rate",
        "gross_margin",
        "repeat_customer_rate",
        "aov",
    }
    assert expected_metrics.issubset(set(DEFAULT_METRICS.keys()))

    for key, metric in DEFAULT_METRICS.items():
        assert metric.key == key
        assert metric.name
        assert metric.description
        assert metric.sql_formula
        assert metric.business_rule
        assert metric.override_hint
        assert len(metric.keywords) > 0


def test_semantic_glossary_custom_override() -> None:
    custom_overrides = {
        "churn_rate": "Customer churn defined strictly as no orders placed in 60 days.",
        "custom_kpi": MetricDefinition(
            key="custom_kpi",
            name="Custom KPI",
            description="Custom executive ratio.",
            sql_formula="SAFE_DIVIDE(metric_a, metric_b)",
            business_rule="Evaluated monthly.",
            override_hint="Override in prompt.",
            keywords=["custom kpi"],
        ),
    }
    glossary = SemanticGlossary(custom_definitions=custom_overrides)

    churn = glossary.get_metric("churn_rate")
    assert churn is not None
    assert "60 days" in churn.description

    custom = glossary.get_metric("custom_kpi")
    assert custom is not None
    assert custom.name == "Custom KPI"


def test_semantic_glossary_prompt_formatting() -> None:
    glossary = SemanticGlossary()
    prompt_section = glossary.format_system_prompt_section()

    assert "BUSINESS METRIC DEFINITIONS (Lightweight Semantic Glossary):" in prompt_section
    assert "Net Revenue" in prompt_section
    assert "Customer Churn Rate" in prompt_section
    assert "Gross Margin" in prompt_section
    assert "METRIC OVERRIDE RULE:" in prompt_section
    assert "ALWAYS prioritize the user's custom definition" in prompt_section


def test_semantic_glossary_detection_and_response_formatting() -> None:
    glossary = SemanticGlossary()

    detected_churn = glossary.detect_metrics("What is our monthly churn and return rate?")
    assert any(m.key == "churn_rate" for m in detected_churn)

    detected_margin = glossary.detect_metrics("Analyze product gross margin and profit")
    assert any(m.key == "gross_margin" for m in detected_margin)

    detected_revenue = glossary.detect_metrics("Show me sales and revenue by department")
    assert any(m.key == "net_revenue" for m in detected_revenue)

    formatted = glossary.format_response_definitions(detected_churn)
    assert "### Metric Definitions Applied" in formatted
    assert "Customer Churn Rate" in formatted
    assert "You can override any metric definition directly in your prompt" in formatted


@pytest.mark.asyncio
async def test_langchain_adapter_injects_glossary_in_sql_prompt() -> None:
    captured_messages: list[BaseMessage] = []

    async def capture_messages(messages: list[BaseMessage]) -> SQLPlan:
        captured_messages.extend(messages)
        return SQLPlan(
            reasoning="Capture test",
            sql_query="SELECT 1",
            requires_sql=True,
            target_tables=["orders"],
        )

    structured_model = RunnableLambda(capture_messages)
    mock_model = MagicMock()
    mock_model.with_structured_output.return_value = structured_model

    adapter = LangChainRetailAgentAdapter()
    adapter._providers = [LLMProvider(name="test", model=mock_model)]

    user_ctx = UserContext(user_id="exec_1", role=UserRole.EXECUTIVE, allowed_brands=[])
    await adapter.generate_sql_plan(
        user_message="Analyze customer churn across states",
        schema_summary="orders, users",
        user_context=user_ctx,
        history=[],
    )

    prompt_text = "\n".join(str(m.content) for m in captured_messages)
    assert "BUSINESS METRIC DEFINITIONS (Lightweight Semantic Glossary):" in prompt_text
    assert "Net Revenue" in prompt_text
    assert "Customer Churn Rate" in prompt_text
    assert "METRIC OVERRIDE RULE:" in prompt_text


def test_langchain_adapter_heuristic_generates_glossary_sql_and_synthesis() -> None:
    adapter = LangChainRetailAgentAdapter()

    # Heuristic SQL for margin
    plan_margin = adapter._heuristic_sql_plan("Show gross margin by product category")
    assert "gross_margin_pct" in plan_margin.sql_query
    assert "net_revenue_usd" in plan_margin.sql_query

    # Heuristic SQL for GMV
    plan_gmv = adapter._heuristic_sql_plan("What is our GMV across departments?")
    assert "gmv_usd" in plan_gmv.sql_query
    assert "status != 'Cancelled'" in plan_gmv.sql_query

    # Heuristic SQL for churn
    plan_churn = adapter._heuristic_sql_plan("Analyze customer churn trend")
    assert "churn_rate_pct" in plan_churn.sql_query


@pytest.mark.asyncio
async def test_end_to_end_chat_declares_metric_definitions(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_glossary.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
    )
    container = ApplicationContainer(test_settings)
    await container.startup()

    try:
        user = UserContext(
            user_id="ceo_test",
            display_name="CEO",
            role=UserRole.CEO,
            allowed_brands=[],
        )

        response = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="test_glossary_sess",
                message="What is our churn rate and return trend?",
            ),
            user,
        )

        assert response.reply
        assert len(response.used_metric_definitions) > 0
        assert any("churn" in d.lower() for d in response.used_metric_definitions)
        assert "### Metric Definitions Applied" in response.reply
        assert "Customer Churn Rate" in response.reply
        assert "You can override any metric definition" in response.reply
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_end_to_end_chat_custom_metric_definition_override(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_glossary_override.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
        custom_metric_glossary={
            "churn_rate": "Churn defined as no orders in 45 days for VIP segments.",
        },
    )
    container = ApplicationContainer(test_settings)
    await container.startup()

    try:
        churn_def = container.glossary.get_metric("churn_rate")
        assert churn_def is not None
        assert "45 days" in churn_def.description

        user = UserContext(
            user_id="ceo_test",
            display_name="CEO",
            role=UserRole.CEO,
            allowed_brands=[],
        )

        response = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="test_glossary_override_sess",
                message="Calculate churn rate defining churn as cancelled orders only",
            ),
            user,
        )

        assert response.reply
        assert len(response.used_metric_definitions) > 0
        assert "### Metric Definitions Applied" in response.reply
    finally:
        await container.shutdown()


def test_semantic_glossary_disabled() -> None:
    glossary = SemanticGlossary(enabled=False)
    assert glossary.enabled is False
    assert glossary.format_system_prompt_section() == ""
    assert glossary.detect_metrics("What is our churn rate and revenue?") == []
    assert glossary.format_response_definitions() == ""


@pytest.mark.asyncio
async def test_end_to_end_chat_glossary_disabled(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_glossary_disabled.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
        enable_semantic_glossary=False,
    )
    container = ApplicationContainer(test_settings)
    await container.startup()

    try:
        assert container.glossary.enabled is False

        user = UserContext(
            user_id="ceo_test",
            display_name="CEO",
            role=UserRole.CEO,
            allowed_brands=[],
        )

        response = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="test_glossary_disabled_sess",
                message="What is our churn rate and return trend?",
            ),
            user,
        )

        assert response.reply
        assert response.used_metric_definitions == []
        assert "### Metric Definitions Applied" not in response.reply
    finally:
        await container.shutdown()
