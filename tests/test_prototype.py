"""Async tests validating PII masking, SQL RBAC policy, self-correction, and HITL confirmation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableLambda
from sqlglot import parse_one

from app.adapters.inbound.http_api import get_current_user
from app.adapters.outbound.langchain_adapter import LangChainRetailAgentAdapter, LLMProvider
from app.domain.models import (
    ChatMessage,
    ChatRequest,
    GuardrailDecision,
    IntentType,
    UserContext,
    UserRole,
)
from app.domain.pii_masker import PIIMasker
from app.domain.sql_policy import SQLPolicyEngine
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer
from app.main import app


def test_pii_masker_redacts_columns_and_text() -> None:
    masker = PIIMasker()
    rows = [
        {
            "user_id": 42,
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane.doe@retail.com",
            "street_address": "742 Evergreen Terrace",
            "total_spend": 520.0,
        }
    ]
    masked_rows, count = masker.mask_rows(rows)
    assert count >= 4
    assert masked_rows[0]["email"] == "[REDACTED_PII]"
    assert masked_rows[0]["first_name"] == "[REDACTED_PII]"
    assert masked_rows[0]["user_id"].startswith("CUST_")
    assert masked_rows[0]["total_spend"] == 520.0

    masked_txt, txt_count = masker.mask_text("Contact john@example.com or 555-123-4567")
    assert txt_count == 2
    assert "john@example.com" not in masked_txt


def test_pii_masker_redacts_sql_literals_and_exact_coordinates() -> None:
    masker = PIIMasker()
    text = (
        "SELECT * FROM users WHERE first_name = 'Jane' "
        "AND postal_code = '10001'; location 37.7749, -122.4194"
    )

    masked, count = masker.mask_text(text)

    assert count >= 3
    assert "Jane" not in masked
    assert "10001" not in masked
    assert "37.7749" not in masked
    assert "-122.4194" not in masked
    assert "[REDACTED_PII]" in masked
    assert "[REDACTED_COORDINATES]" in masked


@pytest.mark.asyncio
async def test_langchain_boundary_masks_current_message_and_history() -> None:
    adapter = LangChainRetailAgentAdapter()
    captured_messages: list[BaseMessage] = []

    async def capture_messages(messages: list[BaseMessage]) -> GuardrailDecision:
        captured_messages.extend(messages)
        return GuardrailDecision(intent=IntentType.DATA_ANALYSIS, is_safe=True)

    structured_model = RunnableLambda(capture_messages)
    model = MagicMock()
    model.with_structured_output.return_value = structured_model
    adapter._providers = [
        LLMProvider(
            name="test",
            model=model,
        )
    ]
    history = [
        ChatMessage(
            role="user",
            content="Contact jane@example.com at 742 Evergreen Terrace or 555-123-4567",
        ),
        ChatMessage(role="assistant", content="Compare revenue by brand."),
    ]

    decision = await adapter.classify_and_guard(
        "Analyze first_name='Jane', postal_code='10001', coordinates: 37.7749, -122.4194",
        history,
    )

    assert decision.is_safe is True
    sent_text = "\n".join(str(message.content) for message in captured_messages)
    for pii_value in (
        "jane@example.com",
        "742 Evergreen Terrace",
        "555-123-4567",
        "Jane",
        "10001",
        "37.7749",
        "-122.4194",
    ):
        assert pii_value not in sent_text
    assert "Compare revenue by brand." in sent_text


def test_sql_policy_enforces_brand_scope_without_recursive_cte() -> None:
    policy = SQLPolicyEngine()
    ceo = UserContext(user_id="u1", role=UserRole.CEO)

    bad_res = policy.validate_and_secure("DROP TABLE orders", ceo)
    assert not bad_res.is_valid

    brand_manager = UserContext(
        user_id="mgr_acme",
        role=UserRole.BRAND_MANAGER,
        allowed_brands=["Acme", "O'Reilly"],
    )
    ok_res = policy.validate_and_secure(
        "SELECT p.brand, SUM(oi.sale_price) AS revenue "
        "FROM `bigquery-public-data.thelook_ecommerce.order_items` oi "
        "JOIN `bigquery-public-data.thelook_ecommerce.products` p ON oi.product_id = p.id "
        "GROUP BY p.brand",
        brand_manager,
    )
    assert ok_res.is_valid
    assert "p.brand IN ('Acme', 'O\\'Reilly')" in ok_res.sanitized_sql
    assert "FROM `__scoped_order_items` AS oi" in ok_res.sanitized_sql
    assert "JOIN `__allowed_products` AS p" in ok_res.sanitized_sql
    assert "FROM `__allowed_products` AS p WHERE" not in ok_res.sanitized_sql
    assert "FROM `bigquery-public-data.thelook_ecommerce.products` AS p" in ok_res.sanitized_sql
    assert "LIMIT 200" in ok_res.sanitized_sql
    assert parse_one(ok_res.sanitized_sql, dialect="bigquery") is not None


def test_sql_policy_scopes_orders_and_users_without_product_join() -> None:
    policy = SQLPolicyEngine()
    manager = UserContext(
        user_id="mgr_acme",
        role=UserRole.BRAND_MANAGER,
        allowed_brands=["Acme"],
    )

    order_result = policy.validate_and_secure(
        "SELECT COUNT(DISTINCT order_id) AS orders FROM orders",
        manager,
    )
    user_result = policy.validate_and_secure(
        "SELECT COUNT(DISTINCT id) AS customers FROM users",
        manager,
    )

    assert order_result.is_valid
    assert "FROM __scoped_orders" in order_result.sanitized_sql
    assert user_result.is_valid
    assert "FROM __scoped_users" in user_result.sanitized_sql


def test_sql_policy_blocks_pii_alias_and_small_demographic_groups() -> None:
    policy = SQLPolicyEngine(demographic_min_group_size=5)
    ceo = UserContext(user_id="ceo", role=UserRole.CEO)

    aliased_pii = policy.validate_and_secure(
        "SELECT first_name AS client FROM users",
        ceo,
    )
    wildcard_pii = policy.validate_and_secure("SELECT * FROM users", ceo)
    serialized_user = policy.validate_and_secure(
        "SELECT TO_JSON_STRING(u) AS client FROM users u",
        ceo,
    )
    aggregate_count = policy.validate_and_secure("SELECT COUNT(*) AS customers FROM users", ceo)
    unsafe_demographics = policy.validate_and_secure(
        "SELECT u.city, COUNT(DISTINCT u.id) AS customers "
        "FROM users u GROUP BY u.city HAVING COUNT(DISTINCT u.id) >= 4",
        ceo,
    )
    bypassed_demographics = policy.validate_and_secure(
        "SELECT u.city, COUNT(DISTINCT u.id) AS customers "
        "FROM users u GROUP BY u.city "
        "HAVING COUNT(DISTINCT u.id) >= 5 OR 1 = 1",
        ceo,
    )
    safe_demographics = policy.validate_and_secure(
        "SELECT u.city, COUNT(DISTINCT u.id) AS customers "
        "FROM users u GROUP BY u.city HAVING COUNT(DISTINCT u.id) >= 5",
        ceo,
    )
    demographic_filtered_revenue = policy.validate_and_secure(
        "SELECT SUM(oi.sale_price) AS total_revenue "
        "FROM order_items oi JOIN users u ON oi.user_id = u.id "
        "WHERE u.country = 'USA' AND u.state = 'CA'",
        ceo,
    )

    assert not aliased_pii.is_valid
    assert "even when renamed" in aliased_pii.violations[0]
    assert not wildcard_pii.is_valid
    assert not serialized_user.is_valid
    assert aggregate_count.is_valid
    assert not unsafe_demographics.is_valid
    assert not bypassed_demographics.is_valid
    assert any("minimum-size" in item or ">= 5" in item for item in unsafe_demographics.violations)
    assert safe_demographics.is_valid
    assert demographic_filtered_revenue.is_valid


def test_http_identity_uses_brands_and_rejects_unknown_roles() -> None:
    user = get_current_user(
        x_user_id="manager_1",
        x_user_name="Manager One",
        x_user_role="brand_manager",
        x_allowed_brands="Acme, Contoso",
    )
    assert user.allowed_brands == ["Acme", "Contoso"]

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(
            x_user_id="attacker",
            x_user_name="Unknown",
            x_user_role="not-a-role",
            x_allowed_brands="",
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_end_to_end_report_creation_and_two_step_deletion(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_opsfleet.db",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
    )
    container = ApplicationContainer(test_settings)
    await container.startup()
    app.state.container = container

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"X-User-Id": "ceo_1", "X-User-Role": "ceo"}

            # 1. Create and save a report mentioning Client X
            r1 = await client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "session_id": "sess_test_1",
                    "message": "Create a report for Client X state underspending in Q1",
                    "save_report": True,
                },
            )
            assert r1.status_code == 200
            body1 = r1.json()
            assert body1["saved_report_id"] is not None

            # 2. Request destructive deletion -> must stage confirmation token, NOT delete immediately
            r2 = await client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "session_id": "sess_test_1",
                    "message": "Delete all reports mentioning Client X",
                },
            )
            assert r2.status_code == 200
            pending = r2.json()["pending_confirmation"]
            assert pending is not None
            token = pending["token"]

            # 3. Confirm deletion with token -> deletes report
            r3 = await client.post(
                "/api/v1/chat/confirm",
                headers=headers,
                json={
                    "session_id": "sess_test_1",
                    "token": token,
                    "approved": True,
                },
            )
            assert r3.status_code == 200
            assert "deleted 1 report" in r3.json()["reply"].lower()
    finally:
        await container.shutdown()


def test_settings_and_container_wiring() -> None:
    custom_settings = Settings(
        app_name="Custom OpsFleet Retail Assistant",
        app_env="testing",
        bq_dataset_id="custom-project.custom_retail_dataset",
        bq_query_timeout_seconds=42,
        pii_salt="custom-pii-salt-xyz",
        chat_history_limit=15,
        chat_history_ttl_seconds=3600,
        demographic_min_group_size=7,
        default_row_limit=150,
        database_url="sqlite+aiosqlite:///:memory:",
        bigquery_mock_mode=True,
    )
    container = ApplicationContainer(custom_settings)
    assert container.settings.bq_query_timeout_seconds == 42
    assert container.warehouse._query_timeout_seconds == 42
    assert container.warehouse._dataset_id == "custom-project.custom_retail_dataset"
    assert container.pii_masker._salt == "custom-pii-salt-xyz"
    assert container.redis_adapter._chat_history_ttl == 3600
    assert container.chat_orchestrator._chat_history_limit == 15
    assert container.sql_policy._default_limit == 150
    assert container.sql_policy._demographic_min_group_size == 7
    assert container.sql_policy._dataset_id == "custom-project.custom_retail_dataset"


@pytest.mark.asyncio
async def test_langgraph_sql_workflow_execution(tmp_path: Path) -> None:
    test_settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test_opsfleet.db",
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
        assert container.chat_orchestrator._analysis_graph is not None

        response = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="test_langgraph_sess",
                message="Show me top categories by sales",
            ),
            user,
        )
        assert response.reply
        assert response.observability["sql_attempts"] >= 1
    finally:
        await container.shutdown()
