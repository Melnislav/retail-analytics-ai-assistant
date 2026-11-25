"""Tests for Step 5: Session-level glossary overrides, range validation, and YAML glossary loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.models import ChatRequest, UserContext, UserRole
from app.domain.semantic_glossary import SemanticGlossary
from app.infrastructure.config import Settings, load_business_glossary
from app.infrastructure.container import ApplicationContainer


def _session_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/sess_glossary.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
        trace_log_path=str(tmp_path / "traces.jsonl"),
    )


def test_load_business_glossary_yaml_and_fallback(tmp_path: Path) -> None:
    loaded = load_business_glossary("configs/business_glossary.yaml")
    assert "churn_rate" in loaded
    assert "net_revenue" in loaded

    missing = load_business_glossary(tmp_path / "missing.yaml")
    assert missing == {}


def test_extract_session_overrides_ignores_months_and_validates_bounds() -> None:
    glossary = SemanticGlossary()

    months_overrides, months_err = glossary.extract_session_overrides(
        "What was our monthly churn rate for the last 6 months?"
    )
    assert months_err is None
    assert months_overrides.churn_days is None

    _, zero_err = glossary.extract_session_overrides("Define churn as no purchase in 0 days")
    assert zero_err is not None and "1 and 365" in zero_err

    _, high_err = glossary.extract_session_overrides("Calculate churn using 500 days")
    assert high_err is not None and "1 and 365" in high_err


@pytest.mark.asyncio
async def test_session_glossary_overrides_persist_within_session_without_global_mutation(
    tmp_path: Path,
) -> None:
    container = ApplicationContainer(_session_test_settings(tmp_path))
    await container.startup()
    ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)

    try:
        # Turn 1 in session A sets 60-day churn override
        r1 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_A",
                message="Calculate churn defining churn as no purchase in 60 days",
            ),
            ceo,
        )
        assert r1.sql_used is not None
        assert "INTERVAL '60' DAY" in r1.sql_used or "INTERVAL 60 DAY" in r1.sql_used
        assert any("60 days" in d for d in r1.used_metric_definitions)

        # Turn 2 in session A without repeating "60 days" still applies 60-day session override
        r2 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_A",
                message="What is our churn rate now?",
            ),
            ceo,
        )
        assert r2.sql_used is not None
        assert "INTERVAL '60' DAY" in r2.sql_used or "INTERVAL 60 DAY" in r2.sql_used
        assert any("60 days" in d for d in r2.used_metric_definitions)

        # Session B must remain isolated on the global 90-day default
        r_other = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_B",
                message="What is our churn rate?",
            ),
            ceo,
        )
        assert r_other.sql_used is not None
        assert "INTERVAL '90' DAY" in r_other.sql_used or "INTERVAL 90 DAY" in r_other.sql_used

        # Repeat customer threshold (3+ orders) & revenue including returns persist in sess_C
        r_repeat_1 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_C",
                message="Show repeat customer rate defining repeat customer as 3+ completed orders",
            ),
            ceo,
        )
        assert r_repeat_1.sql_used is not None
        assert "HAVING COUNT(DISTINCT order_id) >= 3" in r_repeat_1.sql_used

        r_repeat_2 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_C",
                message="Show repeat customer rate by category",
            ),
            ceo,
        )
        assert r_repeat_2.sql_used is not None
        assert "HAVING COUNT(DISTINCT order_id) >= 3" in r_repeat_2.sql_used

        r_rev_1 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_C",
                message="Calculate net revenue including returned items",
            ),
            ceo,
        )
        assert r_rev_1.sql_used is not None
        assert (
            "oi.status <> 'Cancelled'" in r_rev_1.sql_used
            or "oi.status != 'Cancelled'" in r_rev_1.sql_used
        )

        r_rev_2 = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_C",
                message="What is our revenue by category?",
            ),
            ceo,
        )
        assert r_rev_2.sql_used is not None
        assert (
            "oi.status <> 'Cancelled'" in r_rev_2.sql_used
            or "oi.status != 'Cancelled'" in r_rev_2.sql_used
        )

        # Out-of-range churn override returns polite validation error without SQL execution
        r_bad = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_D",
                message="Calculate churn using 500 days",
            ),
            ceo,
        )
        assert r_bad.sql_used is None
        assert "between 1 and 365 days" in r_bad.reply.lower()
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_churn_query_with_months_window_keeps_default_90_day_definition(
    tmp_path: Path,
) -> None:
    glossary = SemanticGlossary()
    query = "What was our monthly churn rate for the last 6 months?"
    assert glossary.churn_inactivity_days(query) == 90

    detected = glossary.detect_metrics(query)
    churn_metric = next((m for m in detected if m.key == "churn_rate"), None)
    assert churn_metric is not None
    assert "last 90 days" in churn_metric.description
    assert "User custom rule" not in churn_metric.description

    container = ApplicationContainer(_session_test_settings(tmp_path))
    await container.startup()
    ceo = UserContext(user_id="ceo_1", role=UserRole.CEO)

    try:
        resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="sess_ev03", message=query),
            ceo,
        )
        assert resp.sql_used is not None
        assert "INTERVAL '90' DAY" in resp.sql_used or "INTERVAL 90 DAY" in resp.sql_used
        assert any("90 days" in d for d in resp.used_metric_definitions)
        assert "6 days" not in resp.reply
    finally:
        await container.shutdown()
