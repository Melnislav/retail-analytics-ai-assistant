"""E2E and persistence interface tests for the Retail Semantic Glossary."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.outbound.persistence_adapter import DatabaseManager, PostgresReportRepository
from app.domain.models import ChatRequest, SavedReport, UserContext, UserRole
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer


@pytest.mark.asyncio
async def test_end_to_end_chat_declares_metric_definitions(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_glossary.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openrouter_api_key="",
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
        openrouter_api_key="",
        openai_api_key="",
        custom_metric_glossary={
            "churn_rate": "Churn defined as no orders in 45 days for VIP segments.",
        },
        metric_clarification_messages={
            "unsupported_configured_churn_scope": (
                "VIP churn is unavailable. Use an unrestricted churn rule."
            )
        },
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
                session_id="test_glossary_override_sess",
                message="Calculate customer churn rate",
            ),
            user,
        )

        assert response.reply
        assert "VIP churn is unavailable" in response.reply
        assert response.data_preview == []
        assert response.used_metric_definitions == []
        assert "Metric Definitions Applied" not in response.reply
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_end_to_end_chat_glossary_disabled(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_glossary_disabled.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openrouter_api_key="",
        openai_api_key="",
        enable_semantic_glossary=False,
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


@pytest.mark.asyncio
async def test_end_to_end_user_custom_rule_preservation(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_custom_rule_e2e.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openrouter_api_key="",
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
                session_id="test_custom_rule_sess",
                message="Рассчитай отток клиентов за 60 дней",
            ),
            user,
        )

        assert response.reply
        assert len(response.used_metric_definitions) > 0
        assert any("60 days" in d for d in response.used_metric_definitions)
        assert "60 days" in response.reply
        assert len(response.data_preview) > 0
        assert response.data_preview[0].get("inactivity_period_days") == 60
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_saved_report_metric_definitions_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "test_report_persistence.db"
    db_mgr = DatabaseManager(f"sqlite+aiosqlite:///{db_path}")
    await db_mgr.init_schema()

    repo = PostgresReportRepository(db_mgr.session_factory)
    test_defs = [
        "Customer Churn Rate: buyers with no purchases in 60 days (Formula: SAFE_DIVIDE...)",
        "Repeat Customer Rate: buyers with 2+ completed orders",
    ]
    report = SavedReport(
        owner_user_id="test_user_1",
        session_id="session_xyz",
        title="Q2 Churn & Retention Analysis",
        summary="Executive summary of churn with custom 60-day rule.",
        content_markdown="# Report Content\nDetailed analysis here.",
        action_items=["Review regional marketing", "Deploy reactivation coupon"],
        sql_used="SELECT * FROM orders",
        metric_definitions=test_defs,
    )

    saved = await repo.create(report)
    assert saved.report_id == report.report_id

    by_owner = await repo.list_by_owner("test_user_1")
    assert len(by_owner) == 1
    assert by_owner[0].metric_definitions == test_defs

    by_sess = await repo.find_by_session("test_user_1", "session_xyz")
    assert len(by_sess) == 1
    assert by_sess[0].metric_definitions == test_defs

    await db_mgr.close()
