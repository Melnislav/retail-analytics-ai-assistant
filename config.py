"""Pydantic v2 BaseSettings configuration loaded from .env."""

from __future__ import annotations

from app.domain.semantic_glossary import DEFAULT_METRICS, MetricDefinition, SemanticGlossary
from app.infrastructure.config import Settings, get_settings

__all__ = [
    "DEFAULT_METRICS",
    "MetricDefinition",
    "SemanticGlossary",
    "Settings",
    "get_settings",
]
