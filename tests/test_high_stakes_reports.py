"""Tests for Step 2: SavedReport completeness, owner isolation, SHA-256 snapshot, TOCTOU, and TTL."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.adapters.outbound.redis_and_telemetry import AsyncRedisAdapter
from app.domain.exceptions import ConfirmationValidationError
from app.domain.models import (
    ChatRequest,
    ConfirmationRequest,
    SavedReport,
    UserContext,
    UserRole,
    utc_now,
)
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer
from app.main import app


def _build_test_settings(tmp_path: Path, db_name: str, confirmation_ttl: int = 600) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/{db_name}",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openai_api_key="",
        confirmation_ttl_seconds=confirmation_ttl,
    )


@pytest.mark.asyncio
async def test_saved_report_persisted_only_on_explicit_save_and_owner_scoped(
    tmp_path: Path,
) -> None:
    container = ApplicationContainer(_build_test_settings(tmp_path, "owner_scope.db"))
    await container.startup()
    app.state.container = container

    try:
        alice = container.prototype_user
        bob = UserContext(
            user_id="mgr_bob",
            display_name="Bob",
            role=UserRole.BRAND_MANAGER,
            allowed_brands=["Diesel"],
        )

        # Normal analytical query must NOT auto-save a report.
        normal_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="sess_no_save", message="Show revenue by category"),
            alice,
        )
        assert normal_resp.saved_report_id is None
        assert await container.report_service.list_user_reports(alice) == []

        # Explicit save persists question, SQL, and answer with PII masked.
        saved_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_with_save",
                message="Compare sales for Calvin Klein for contact jane@example.com",
                save_report=True,
            ),
            alice,
        )
        assert saved_resp.saved_report_id is not None

        loaded = await container.report_service.get_user_report(
            alice, saved_resp.saved_report_id[:8]
        )
        assert loaded is not None
        assert loaded.report_id == saved_resp.saved_report_id
        assert loaded.sql_used == saved_resp.sql_used
        assert "jane@example.com" not in loaded.question
        assert "[REDACTED_EMAIL]" in loaded.question
        assert loaded.content_markdown == saved_resp.reply

        # Another owner must not be able to fetch Alice's report directly.
        assert await container.report_service.get_user_report(bob, loaded.report_id) is None

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ok_resp = await client.get(f"/api/v1/reports/{loaded.report_id[:8]}")
            assert ok_resp.status_code == 200
            body = ok_resp.json()
            assert body["report_id"] == loaded.report_id
            assert body["question"] == loaded.question
            assert body["sql_used"] == loaded.sql_used

            missing_resp = await client.get("/api/v1/reports/nonexistent-id")
            assert missing_resp.status_code == 404
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_confirmation_snapshot_hash_and_toctou_protection(tmp_path: Path) -> None:
    container = ApplicationContainer(_build_test_settings(tmp_path, "toctou.db"))
    await container.startup()

    try:
        alice = container.prototype_user
        r1 = await container.report_service.save_report(
            SavedReport(
                owner_user_id=alice.user_id,
                session_id="sess_toctou",
                question="Client X revenue Q1?",
                title="Client X Q1 Revenue",
                summary="Summary 1",
                content_markdown="Body 1",
                sql_used="SELECT 1",
            )
        )
        r2 = await container.report_service.save_report(
            SavedReport(
                owner_user_id=alice.user_id,
                session_id="sess_toctou",
                question="Client X churn Q1?",
                title="Client X Q1 Churn",
                summary="Summary 2",
                content_markdown="Body 2",
                sql_used="SELECT 2",
            )
        )

        # Deletion request stages snapshot with SHA-256 hash and preview IDs/timestamps.
        stage_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="sess_toctou",
                message="Delete all reports mentioning Client X",
            ),
            alice,
        )
        pending = stage_resp.pending_confirmation
        assert pending is not None
        assert pending.payload_hash
        assert set(pending.target_report_ids) == {r1.report_id, r2.report_id}
        assert r1.report_id[:8] in stage_resp.reply
        assert r2.report_id[:8] in stage_resp.reply
        assert pending.payload_hash[:12] in stage_resp.reply

        # Tampering with payload hash or target IDs must fail closed.
        tampered = pending.model_copy(update={"payload_hash": "0" * 64})
        with pytest.raises(ConfirmationValidationError):
            await container.report_service.execute_confirmed_deletion(tampered, alice)
        assert len(await container.report_service.list_user_reports(alice)) == 2

        # Wrong user must also fail closed.
        other_user = UserContext(
            user_id="other_mgr",
            display_name="Other",
            role=UserRole.BRAND_MANAGER,
            allowed_brands=["Calvin Klein"],
        )
        with pytest.raises(ConfirmationValidationError):
            await container.report_service.execute_confirmed_deletion(pending, other_user)
        assert len(await container.report_service.list_user_reports(alice)) == 2

        # TOCTOU: remove r1 out-of-band before confirmation executes -> abort & preserve r2.
        await container.report_repo.delete_by_ids(alice.user_id, [r1.report_id])
        confirm_resp = await container.chat_orchestrator.confirm_action(
            ConfirmationRequest(
                session_id="sess_toctou",
                token=pending.token,
                approved=True,
            ),
            alice,
        )
        assert "no reports were deleted" in confirm_resp.reply.lower()
        remaining = await container.report_service.list_user_reports(alice)
        assert [r.report_id for r in remaining] == [r2.report_id]
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_confirmation_cancellation_is_single_use_and_retains_reports(
    tmp_path: Path,
) -> None:
    container = ApplicationContainer(_build_test_settings(tmp_path, "cancel.db"))
    await container.startup()

    try:
        alice = container.prototype_user
        report = await container.report_service.save_report(
            SavedReport(
                owner_user_id=alice.user_id,
                session_id="sess_cancel",
                question="Show revenue",
                title="Revenue Snapshot",
                summary="Summary",
                content_markdown="Body",
            )
        )

        pending = await container.report_service.prepare_deletion(
            user=alice,
            session_id="sess_cancel",
            scope="conversation",
        )
        assert pending is not None

        cancel_resp = await container.chat_orchestrator.confirm_action(
            ConfirmationRequest(session_id="sess_cancel", token=pending.token, approved=False),
            alice,
        )
        assert "remain untouched" in cancel_resp.reply.lower()
        assert len(await container.report_service.list_user_reports(alice)) == 1

        # Replaying the same token after cancellation must fail cleanly.
        replay_resp = await container.chat_orchestrator.confirm_action(
            ConfirmationRequest(session_id="sess_cancel", token=pending.token, approved=True),
            alice,
        )
        assert "invalid, expired" in replay_resp.reply.lower()
        assert (await container.report_service.list_user_reports(alice))[0].report_id == (
            report.report_id
        )
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_in_memory_ttl_and_confirmation_expiration(tmp_path: Path) -> None:
    adapter = AsyncRedisAdapter(
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        confirmation_ttl_seconds=1,
    )
    await adapter.set_json("short_lived", {"ok": True}, ttl_seconds=1)
    assert await adapter.get_json("short_lived") == {"ok": True}
    await asyncio.sleep(1.05)
    assert await adapter.get_json("short_lived") is None

    # Also verify explicit expires_at check inside ReportService even if store still holds item.
    container = ApplicationContainer(_build_test_settings(tmp_path, "expired.db"))
    await container.startup()
    try:
        alice = container.prototype_user
        await container.report_service.save_report(
            SavedReport(
                owner_user_id=alice.user_id,
                session_id="sess_exp",
                question="Q",
                title="Title",
                summary="Sum",
                content_markdown="Body",
            )
        )
        pending = await container.report_service.prepare_deletion(
            user=alice, session_id="sess_exp", scope="conversation"
        )
        assert pending is not None
        expired = pending.model_copy(update={"expires_at": utc_now() - timedelta(seconds=5)})
        with pytest.raises(ConfirmationValidationError, match="expired"):
            await container.report_service.execute_confirmed_deletion(expired, alice)
        assert len(await container.report_service.list_user_reports(alice)) == 1
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_cli_view_report_standalone_owner_scoped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    container = ApplicationContainer(_build_test_settings(tmp_path, "cli_view.db"))
    await container.startup()

    try:
        cli_alice = RetailAssistantCLI(user_key="brand_manager_alice", standalone=True)
        cli_alice.container = container

        saved = await container.report_service.save_report(
            SavedReport(
                owner_user_id=cli_alice.user_id,
                session_id="cli_sess",
                question="How did Calvin Klein perform?",
                title="Calvin Klein Q1 Brief",
                summary="Growth +12%",
                content_markdown="Detailed markdown answer.",
                sql_used="SELECT brand FROM products",
            )
        )

        await cli_alice._view_report(None, saved.report_id[:8])
        alice_out = capsys.readouterr().out
        assert "Calvin Klein Q1 Brief" in alice_out
        assert "How did Calvin Klein perform?" in alice_out
        assert "SELECT brand FROM products" in alice_out

        cli_bob = RetailAssistantCLI(user_key="brand_manager_bob", standalone=True)
        cli_bob.container = container
        await cli_bob._view_report(None, saved.report_id)
        bob_out = capsys.readouterr().out
        assert "not found" in bob_out.lower()
    finally:
        await container.shutdown()
