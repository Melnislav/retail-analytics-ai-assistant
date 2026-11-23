"""Async tests validating PII masking, SQL RBAC policy, self-correction, and HITL confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.domain.models import UserContext, UserRole
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


def test_sql_policy_blocks_mutations_and_enforces_product_rbac() -> None:
    policy = SQLPolicyEngine()
    exec_user = UserContext(user_id="u1", role=UserRole.EXECUTIVE)

    bad_res = policy.validate_and_secure("DROP TABLE orders", exec_user)
    assert not bad_res.is_valid

    cat_mgr = UserContext(
        user_id="mgr_jeans",
        role=UserRole.CATEGORY_MANAGER,
        allowed_categories=["Jeans", "Outerwear & Coats"],
    )
    ok_res = policy.validate_and_secure(
        "SELECT category, SUM(retail_price) FROM `bigquery-public-data.thelook_ecommerce.products` GROUP BY category",
        cat_mgr,
    )
    assert ok_res.is_valid
    assert "__scoped_products" in ok_res.sanitized_sql
    assert "category IN ('Jeans', 'Outerwear & Coats')" in ok_res.sanitized_sql
    assert "LIMIT 200" in ok_res.sanitized_sql


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
            headers = {"X-User-Id": "exec_1", "X-User-Role": "executive"}

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
        golden_trio_top_k=5,
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
    assert container.chat_orchestrator._golden_trio_top_k == 5
    assert container.sql_policy._default_limit == 150
    assert container.sql_policy._dataset_id == "custom-project.custom_retail_dataset"
