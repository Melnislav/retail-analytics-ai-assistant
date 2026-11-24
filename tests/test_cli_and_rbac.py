"""Tests for local user RBAC profile loading and CLI standalone mode enhancements."""

from __future__ import annotations

from pathlib import Path

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


def test_cli_client_headers_contain_rbac_details() -> None:
    cli = RetailAssistantCLI(user_key="brand_manager_bob")
    headers = cli.headers
    assert headers["X-User-Id"] == "mgr_bob"
    assert headers["X-User-Role"] == "brand_manager"
    assert "Diesel" in headers["X-Allowed-Brands"]
    assert "Tommy Hilfiger" in headers["X-Allowed-Brands"]
