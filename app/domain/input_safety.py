"""Deterministic ingress guard for unsafe assistant requests."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InputSafetyDecision:
    """Outcome of deterministic request inspection before any LLM call."""

    is_safe: bool
    rejection_reason: str | None = None


class InputSafetyPolicy:
    """Blocks command-injection and direct PII-exposure requests at system ingress."""

    _INJECTION_PATTERNS = (
        re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions?\b", re.IGNORECASE),
        re.compile(r"\b(?:reveal|show|dump)\s+(?:the\s+)?system\s+prompt\b", re.IGNORECASE),
        re.compile(r"\bbypass\s+(?:the\s+)?(?:pii|security|guardrail)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:drop|alter|truncate|insert\s+into|update|delete\s+from)\b",
            re.IGNORECASE,
        ),
    )
    _PII_REQUEST_PATTERN = re.compile(
        r"\b(?:show|list|give|export|reveal|find|identify|what\s+did)\b.*\b(?:"
        r"first[_\s-]?name|last[_\s-]?name|full[_\s-]?name|e-?mail|"
        r"street[_\s-]?address|postal[_\s-]?code|zip(?:code)?|latitude|longitude|"
        r"coordinates|phone(?:[_\s-]?number)?|ip[_\s-]?address)\b",
        re.IGNORECASE,
    )

    def inspect(self, message: str) -> InputSafetyDecision:
        if any(pattern.search(message) for pattern in self._INJECTION_PATTERNS):
            return InputSafetyDecision(
                is_safe=False,
                rejection_reason=(
                    "Security policy violation: system-instruction override or destructive SQL "
                    "request blocked."
                ),
            )
        if self._PII_REQUEST_PATTERN.search(message):
            return InputSafetyDecision(
                is_safe=False,
                rejection_reason=(
                    "Privacy policy violation: requests for direct personal data are not supported. "
                    "Ask for an aggregate instead."
                ),
            )
        return InputSafetyDecision(is_safe=True)
