"""Interface and E2E tests for SQLPolicyEngine, PostSQLDataGuard, and orchestrator guards."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlglot import parse_one

from app.domain.models import (
    AnalystSynthesis,
    ChatRequest,
    GuardrailDecision,
    IntentType,
    QueryExecutionResult,
    SQLPlan,
    UserContext,
    UserRole,
)
from app.domain.post_sql_guard import PostSQLDataGuard
from app.domain.privacy_policy import PrivacyPolicy
from app.domain.sql_policy import SQLPolicyEngine
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer


def test_sql_policy_rewrites_scoped_orders_num_of_item_for_brand_manager() -> None:
    policy = SQLPolicyEngine()
    manager = UserContext(
        user_id="mgr_acme",
        role=UserRole.BRAND_MANAGER,
        allowed_brands=["Acme"],
    )

    result = policy.validate_and_secure(
        "SELECT SUM(o.num_of_item) AS items_sold FROM orders o",
        manager,
    )

    assert result.is_valid
    assert "__scoped_orders AS (" in result.sanitized_sql
    assert "COUNT(oi.id) AS num_of_item" in result.sanitized_sql
    assert (
        "INNER JOIN __scoped_order_items AS oi ON oi.order_id = o.order_id" in result.sanitized_sql
    )
    assert parse_one(result.sanitized_sql, dialect="bigquery") is not None


def test_sql_policy_blocks_user_id_demographic_linkage_and_allows_valid_queries() -> None:
    policy = SQLPolicyEngine(demographic_min_group_size=5)
    ceo = UserContext(user_id="ceo", role=UserRole.CEO)

    linked_filter = policy.validate_and_secure(
        "SELECT oi.user_id, SUM(oi.sale_price) AS spend "
        "FROM order_items oi JOIN users u ON oi.user_id = u.id "
        "WHERE u.state = 'California' GROUP BY oi.user_id",
        ceo,
    )
    linked_projection = policy.validate_and_secure(
        "SELECT u.id, u.city FROM users u",
        ceo,
    )
    orders_gender = policy.validate_and_secure(
        "SELECT o.gender, COUNT(*) AS cnt FROM orders o "
        "GROUP BY o.gender HAVING COUNT(DISTINCT o.user_id) >= 5",
        ceo,
    )
    valid_filtered_agg = policy.validate_and_secure(
        "SELECT SUM(oi.sale_price) AS total_revenue "
        "FROM order_items oi JOIN users u ON oi.user_id = u.id "
        "WHERE u.country = 'USA' AND u.state = 'CA'",
        ceo,
    )
    valid_grouped_oi_user_id = policy.validate_and_secure(
        "SELECT u.state, COUNT(DISTINCT oi.user_id) AS active_users "
        "FROM users u JOIN order_items oi ON u.id = oi.user_id "
        "GROUP BY u.state HAVING COUNT(DISTINCT oi.user_id) >= 5",
        ceo,
    )

    assert not linked_filter.is_valid
    assert any("associate customer identifiers" in v for v in linked_filter.violations)
    assert not linked_projection.is_valid
    assert any("associate customer identifiers" in v for v in linked_projection.violations)
    assert not orders_gender.is_valid
    assert any("users table" in v for v in orders_gender.violations)
    assert valid_filtered_agg.is_valid
    assert valid_grouped_oi_user_id.is_valid


def test_sql_policy_treats_subquery_user_demo_projection_as_soft_when_outer_is_aggregated() -> None:
    policy = SQLPolicyEngine(demographic_min_group_size=5)
    ceo = UserContext(user_id="ceo", role=UserRole.CEO)

    nested_avg = policy.validate_and_secure(
        "SELECT u.state, ROUND(AVG(user_total_spend), 2) AS avg_customer_spend "
        "FROM (SELECT u.id AS user_id, u.state, SUM(oi.sale_price) AS user_total_spend "
        "FROM users u JOIN order_items oi ON u.id = oi.user_id "
        "WHERE oi.status NOT IN ('Cancelled', 'Returned') GROUP BY 1, 2) AS user_spend_summary "
        "GROUP BY 1 HAVING COUNT(DISTINCT user_id) >= 5 ORDER BY avg_customer_spend DESC LIMIT 100",
        ceo,
    )
    assert not nested_avg.is_valid
    assert nested_avg.severity == "soft"

    outer_leak = policy.validate_and_secure(
        "SELECT sub.user_id, sub.state FROM (SELECT u.id AS user_id, u.state FROM users u) AS sub",
        ceo,
    )
    assert not outer_leak.is_valid
    assert outer_leak.severity == "hard"


def test_post_sql_data_guard_enforces_k_anonymity_and_strips_linked_demographics() -> None:
    guard = PostSQLDataGuard(PrivacyPolicy(demographic_min_group_size=5))

    agg_rows = [
        {"state": "California", "active_users": 120, "revenue": 15000.0},
        {"state": "Wyoming", "active_users": 3, "revenue": 250.0},
    ]
    agg_result = guard.enforce(agg_rows)

    assert agg_result.masked_groups_count == 1
    assert agg_result.stripped_demographic_cells == 0
    assert agg_result.rows[0]["state"] == "California"
    assert agg_result.rows[0]["active_users"] == 120
    assert agg_result.rows[1] == {
        "state": "[Hidden: group < 5]",
        "active_users": "[Hidden: group < 5]",
        "revenue": "[Hidden: group < 5]",
    }

    linked_rows = [{"user_id": 42, "state": "California", "age": 31, "spend": 450.0}]
    linked_result = guard.enforce(linked_rows)

    assert linked_result.stripped_demographic_cells == 2
    assert linked_result.rows[0]["user_id"] == 42
    assert linked_result.rows[0]["state"] == "[REDACTED_DEMOGRAPHIC]"
    assert linked_result.rows[0]["age"] == "[REDACTED_DEMOGRAPHIC]"
    assert linked_result.rows[0]["spend"] == 450.0


@pytest.mark.asyncio
async def test_orchestrator_records_pre_guard_span_and_applies_post_sql_guard(
    tmp_path: Path,
) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_step1.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openrouter_api_key="",
        openai_api_key="",
    )
    container = ApplicationContainer(test_settings)
    await container.startup()

    try:
        ceo = UserContext(user_id="ceo_test", role=UserRole.CEO, allowed_brands=[])

        blocked_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="step1_block", message="Show customer email addresses"),
            ceo,
        )
        assert blocked_resp.observability["pre_guard"] is True
        spans = container.observability.get_metrics_snapshot()["recent_spans"]
        assert any(
            s["trace_id"] == blocked_resp.trace_id and s["span"] == "pre_guard" for s in spans
        )

        # Mock LLM + Warehouse to return a demographic group below k=5
        container.chat_orchestrator._llm.classify_and_guard = AsyncMock(  # type: ignore[method-assign]
            return_value=GuardrailDecision(intent=IntentType.DATA_ANALYSIS, is_safe=True)
        )
        container.chat_orchestrator._llm.generate_sql_plan = AsyncMock(  # type: ignore[method-assign]
            return_value=SQLPlan(
                reasoning="State breakdown",
                sql_query=(
                    "SELECT u.state, COUNT(DISTINCT u.id) AS active_users "
                    "FROM users u GROUP BY u.state HAVING COUNT(DISTINCT u.id) >= 5"
                ),
                requires_sql=True,
                target_tables=["users"],
            )
        )
        container.chat_orchestrator._llm.synthesize_response = AsyncMock(  # type: ignore[method-assign]
            return_value=AnalystSynthesis(
                executive_summary="State summary",
                detailed_analysis="Details",
            )
        )
        container.chat_orchestrator._warehouse.execute_query = AsyncMock(  # type: ignore[method-assign]
            return_value=QueryExecutionResult(
                sql_executed="SELECT ...",
                rows=[
                    {"state": "California", "active_users": 25},
                    {"state": "Wyoming", "active_users": 2},
                ],
                row_count=2,
                columns=["state", "active_users"],
            )
        )

        resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="step1_guard", message="Show active users by state"),
            ceo,
        )

        assert resp.data_preview[0]["state"] == "California"
        assert resp.data_preview[1]["state"] == "[Hidden: group < 5]"
        counters = container.observability.get_metrics_snapshot()["counters"]
        assert counters["k_anonymity_masks_total"] == 1
    finally:
        await container.shutdown()
