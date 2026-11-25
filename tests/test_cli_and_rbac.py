"""Tests for CLI interface behavior and persona switching guardrails."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from app.adapters.inbound import cli_client
from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.infrastructure.config import get_settings


@pytest.mark.asyncio
async def test_cli_user_switch_allowed_in_standalone_and_blocked_in_http_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    http_cli = RetailAssistantCLI(user_key="brand_manager_alice", standalone=False)
    http_inputs = iter(["/user ceo", "/exit"])
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *_args, **_kwargs: next(http_inputs))
    await asyncio.wait_for(http_cli.run(), timeout=5.0)
    http_out = capsys.readouterr().out
    assert http_cli.user_id == "mgr_alice"
    assert "cannot switch personas in http mode" in http_out.lower()

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cli_switch.db")
    monkeypatch.setenv("REDIS_URL", "redis://invalid-host-uses-memory-fallback:6379/0")
    monkeypatch.setenv("BIGQUERY_MOCK_MODE", "true")
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "cli_traces.jsonl"))
    get_settings.cache_clear()
    try:
        standalone_cli = RetailAssistantCLI(user_key="brand_manager_alice", standalone=True)
        standalone_inputs = iter(["/user ceo", "/exit"])
        monkeypatch.setattr(
            "rich.prompt.Prompt.ask", lambda *_args, **_kwargs: next(standalone_inputs)
        )
        await asyncio.wait_for(standalone_cli.run(), timeout=5.0)
        standalone_out = capsys.readouterr().out
        assert standalone_cli.user_id == "ceo_carol"
        assert "switched active user" in standalone_out.lower()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cli_confirm_action_handles_non_200_http_response(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = httpx.MockTransport(
        lambda _req: httpx.Response(status_code=503, text="Service unavailable")
    )
    cli = RetailAssistantCLI(base_url="http://test-server:8000", standalone=False)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await cli._confirm_action(client, "confirm_abc123", True)

    assert res is None
    assert "API Error (503)" in capsys.readouterr().out


def test_cli_main_exits_cleanly_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def raise_interrupt(self: RetailAssistantCLI) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(sys, "argv", ["cli_client", "--standalone"])
    monkeypatch.setattr(RetailAssistantCLI, "run", raise_interrupt)

    cli_client.main()
    out = capsys.readouterr().out
    assert "Interrupted. Exiting CLI." in out
