"""Pydantic v2 BaseSettings configuration loaded from .env."""

from __future__ import annotations

from app.infrastructure.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
