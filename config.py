"""Single source of truth configuration re-export.

Canonical configuration models reside in app.infrastructure.config.
This module re-exports Settings and get_settings to maintain backward
compatibility for root-level entry points without duplicating definitions.
"""

from __future__ import annotations

from app.infrastructure.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
