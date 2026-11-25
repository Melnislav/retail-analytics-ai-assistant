"""Tests for local user RBAC profile loading and CLI standalone mode enhancements."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.adapters.inbound.cli_client import RetailAssistantCLI
from app.infrastructure.config import load_user_rbac_profiles


def test_load_user_rbac_profiles_fallback() -> None:
    profiles = load_user_rbac_profiles(config_path="non_existent_config.yaml")
    assert "ceo" in profiles
    assert "brand_manager_alice" in profiles
    assert "brand_manager_bob" in profiles
    assert profiles["ceo"]["role"] == "ceo"
    assert profiles["brand_manager_alice"]["allowed_brands"] == ["Calvin Klein"]


def test_load_user_rbac_profiles_from_yaml(tmp_path: Path) -> None:
    yaml_content = """
users:
  custom_tester:
    user_id: "test_id"
    display_name: "Tester"
    role: "brand_manager"
    allowed_brands: ["BrandX"]
"""
    p = tmp_path / "test_users.yaml"
    p.write_text(yaml_content, encoding="utf-8")

    profiles = load_user_rbac_profiles(config_path=p)
    assert "custom_tester" in profiles
    assert profiles["custom_tester"]["user_id"] == "test_id"
    assert profiles["custom_tester"]["allowed_brands"] == ["BrandX"]


def test_cli_client_profile_initialization() -> None:
    cli_alice = RetailAssistantCLI(user_key="brand_manager_alice")
    assert cli_alice.user_id == "mgr_alice"
    assert cli_alice.user_role == "brand_manager"
    assert cli_alice.allowed_brands == "Calvin Klein"
    assert cli_alice.user_context.allowed_brands == ["Calvin Klein"]

    cli_ceo = RetailAssistantCLI(user_key="ceo")
    assert cli_ceo.user_id == "ceo_carol"
    assert cli_ceo.user_role == "ceo"
    assert cli_ceo.user_context.allowed_brands == []


def test_cli_client_resolves_rbac_details_without_authority_headers() -> None:
    cli = RetailAssistantCLI(user_key="brand_manager_bob")
    assert cli.headers == {}
    assert cli.user_context.user_id == "mgr_bob"
    assert cli.user_context.role.value == "brand_manager"
    assert cli.user_context.allowed_brands == ["Diesel", "Tommy Hilfiger"]


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
    from app.infrastructure.config import get_settings

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


def test_cli_client_respects_prototype_user_profile_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOTYPE_USER_PROFILE", "ceo")
    cli = RetailAssistantCLI()
    assert cli.profile_key == "ceo"
    assert cli.user_id == "ceo_carol"
    assert cli.user_role == "ceo"


@pytest.mark.asyncio
async def test_cli_confirm_action_handles_non_200_http_response(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import httpx

    transport = httpx.MockTransport(
        lambda _req: httpx.Response(status_code=503, text="Service unavailable")
    )
    cli = RetailAssistantCLI(base_url="http://test-server:8000", standalone=False)
    async with httpx.AsyncClient(transport=transport) as client:
        res = await cli._confirm_action(client, "confirm_abc123", True)

    assert res is None
    assert "API Error (503)" in capsys.readouterr().out
