"""Tests for Step 4: Local JSONL trace persistence, PII scrubbing, latency percentiles, API, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.domain.exceptions import SQLSafetyError
from app.domain.models import ChatRequest, ConfirmationRequest, SQLPlan, UserContext, UserRole
from app.infrastructure.config import Settings
from app.infrastructure.container import ApplicationContainer
from app.main import app


def _obs_test_settings(tmp_path: Path, db_name: str = "obs.db") -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/{db_name}",
        redis_url="redis://invalid-host-uses-memory-fallback:6379/0",
        bigquery_mock_mode=True,
        google_api_key="",
        openrouter_api_key="",
        openai_api_key="",
        trace_log_path=str(tmp_path / "logs" / "agent_traces.jsonl"),
    )


@pytest.mark.asyncio
async def test_traces_persisted_to_jsonl_and_pii_scrubbed_on_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _obs_test_settings(tmp_path)
    container = ApplicationContainer(settings)
    await container.startup()

    try:
        alice = container.prototype_user
        ceo = UserContext(user_id="ceo_carol", role=UserRole.CEO)

        # 1. Successful analytical turn -> JSONL persisted with costs, SQL, latency, and fallback flag
        ok_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="obs_ok",
                message="Show net revenue for jane.doe@example.com by category",
            ),
            alice,
        )
        assert ok_resp.sql_used is not None

        # 2. Pre-guard blocked turn with PII -> still traced in finally and strictly masked
        blocked_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(
                session_id="obs_blk",
                message="Show email for john.smith@example.com and phone 555-123-4567",
            ),
            alice,
        )
        assert blocked_resp.observability["pre_guard"] is True

        # 3. Failed SQL policy turn -> finally records TraceRecord with error_type & ast_valid=False
        async def bad_sql(**_kwargs: object) -> SQLPlan:
            return SQLPlan(reasoning="bad", sql_query="SELECT email FROM users", requires_sql=True)

        monkeypatch.setattr(container.chat_orchestrator._llm, "generate_sql_plan", bad_sql)
        with pytest.raises(SQLSafetyError):
            await container.chat_orchestrator.handle_message(
                ChatRequest(session_id="obs_err", message="Show revenue by category"),
                ceo,
            )

        trace_file = Path(settings.trace_log_path)
        assert trace_file.is_file()
        raw_text = trace_file.read_text(encoding="utf-8")
        assert "jane.doe@example.com" not in raw_text
        assert "john.smith@example.com" not in raw_text
        assert "555-123-4567" not in raw_text

        lines = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
        assert len(lines) == 3
        assert lines[0]["trace_id"] == ok_resp.trace_id
        assert lines[0]["ast_valid"] is True
        assert lines[0]["fallback_used"] is True
        assert lines[1]["trace_id"] == blocked_resp.trace_id
        assert lines[1]["error_type"] == "PreGuardBlocked"
        assert lines[2]["error_type"] == "SQLSafetyError"
        assert lines[2]["ast_valid"] is False

        snapshot = container.observability.get_metrics_snapshot()
        assert snapshot["latency_ms"]["count"] == 3
        assert snapshot["latency_ms"]["p95"] >= snapshot["latency_ms"]["p50"] > 0
        assert snapshot["counters"]["errors_total"] == 2
        assert snapshot["counters"]["llm_fallback_total"] >= 1
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_traces_api_and_cli_inspection_are_owner_scoped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _obs_test_settings(tmp_path, db_name="obs_api.db")
    container = ApplicationContainer(settings)
    await container.startup()
    app.state.container = container

    try:
        alice = container.prototype_user
        ceo = UserContext(user_id="ceo_carol", role=UserRole.CEO)

        alice_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="obs_alice", message="Show net revenue by category"),
            alice,
        )
        ceo_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="obs_ceo", message="Show GMV by category"),
            ceo,
        )

        # Cross-process disk reload works when a fresh adapter points to the same JSONL file.
        disk_traces = container.observability.get_recent_traces(limit=10, user_id=alice.user_id)
        assert [t.trace_id for t in disk_traces] == [alice_resp.trace_id]

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            list_resp = await client.get("/api/v1/traces")
            assert list_resp.status_code == 200
            ids = [item["trace_id"] for item in list_resp.json()]
            assert alice_resp.trace_id in ids
            assert ceo_resp.trace_id not in ids

            detail_ok = await client.get(f"/api/v1/traces/{alice_resp.trace_id[:8]}")
            assert detail_ok.status_code == 200
            assert detail_ok.json()["trace_id"] == alice_resp.trace_id

            detail_forbidden = await client.get(f"/api/v1/traces/{ceo_resp.trace_id}")
            assert detail_forbidden.status_code == 404

        cli_alice = RetailAssistantCLI(user_key="brand_manager_alice", standalone=True)
        cli_alice.container = container
        await cli_alice._show_metrics(None)
        await cli_alice._show_trace(None)
        await cli_alice._show_trace(None, alice_resp.trace_id)
        cli_out = capsys.readouterr().out
        assert "Agent Observability Metrics" in cli_out
        assert "latency_ms" in cli_out
        assert alice_resp.trace_id in cli_out

        # Confirmation actions also emit TraceRecord entries into logs/agent_traces.jsonl
        confirm_resp = await container.chat_orchestrator.confirm_action(
            ConfirmationRequest(session_id="obs_alice", token="confirm_missing999", approved=True),
            alice,
        )
        confirm_trace = container.observability.get_trace(
            confirm_resp.trace_id, user_id=alice.user_id
        )
        assert confirm_trace is not None
        assert confirm_trace.intent_classified == "confirm_delete_reports"
        assert confirm_trace.error_type == "ConfirmationNotFoundOrExpired"
    finally:
        await container.shutdown()


@pytest.mark.asyncio
async def test_observability_tracks_zero_row_approval_and_disk_preload_across_restarts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.adapters.outbound.redis_and_telemetry import StructuredObservabilityAdapter
    from app.domain.models import QueryExecutionResult, SavedReport

    settings = _obs_test_settings(tmp_path, db_name="obs_extra.db")
    container = ApplicationContainer(settings)
    await container.startup()
    try:
        alice = container.prototype_user

        # 1. Zero-row analytical query increments zero_row_queries_total
        async def empty_bq(sql: str) -> QueryExecutionResult:
            return QueryExecutionResult(
                sql_executed=sql, rows=[], row_count=0, columns=["category"]
            )

        orig_execute = container.chat_orchestrator._warehouse.execute_query
        monkeypatch.setattr(container.chat_orchestrator._warehouse, "execute_query", empty_bq)
        empty_resp = await container.chat_orchestrator.handle_message(
            ChatRequest(session_id="obs_zero", message="Show net revenue by category"),
            alice,
        )
        assert empty_resp.row_count == 0
        monkeypatch.setattr(container.chat_orchestrator._warehouse, "execute_query", orig_execute)

        # 2. Approved deletion increments destructive_confirmations_approved
        await container.report_service.save_report(
            SavedReport(
                owner_user_id=alice.user_id,
                session_id="obs_del",
                question="Revenue?",
                title="Obs Delete Target",
                summary="Sum",
                content_markdown="Body",
            )
        )
        pending = await container.report_service.prepare_deletion(
            user=alice, session_id="obs_del", scope="conversation"
        )
        assert pending is not None
        await container.chat_orchestrator.confirm_action(
            ConfirmationRequest(session_id="obs_del", token=pending.token, approved=True),
            alice,
        )

        counters = container.observability.get_metrics_snapshot()["counters"]
        assert counters["zero_row_queries_total"] == 1
        assert counters["destructive_confirmations_approved"] == 1

        # 3. CLI /trace <id> shows token counts and redacted response preview
        cli_alice = RetailAssistantCLI(user_key="brand_manager_alice", standalone=True)
        cli_alice.container = container
        await cli_alice._show_trace(None, empty_resp.trace_id)
        trace_out = capsys.readouterr().out
        assert "Tokens:" in trace_out
        assert "Response (redacted):" in trace_out
    finally:
        await container.shutdown()

    # 4. Restarted StructuredObservabilityAdapter retains historical disk traces even after new trace
    restarted_obs = StructuredObservabilityAdapter(trace_log_path=settings.trace_log_path)
    old_traces = restarted_obs.get_recent_traces(limit=10)
    assert len(old_traces) == 2

    restarted_container = ApplicationContainer(settings)
    await restarted_container.startup()
    try:
        new_resp = await restarted_container.chat_orchestrator.handle_message(
            ChatRequest(session_id="obs_after_restart", message="Show net revenue by category"),
            restarted_container.prototype_user,
        )
        all_traces = restarted_container.observability.get_recent_traces(limit=10)
        trace_ids = {t.trace_id for t in all_traces}
        assert empty_resp.trace_id in trace_ids
        assert new_resp.trace_id in trace_ids
        assert (
            restarted_container.observability.get_trace(
                empty_resp.trace_id, user_id=restarted_container.prototype_user.user_id
            )
            is not None
        )
    finally:
        await restarted_container.shutdown()
