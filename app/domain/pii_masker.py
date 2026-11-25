"""Deterministic PII masking and redaction engine for raw transaction rows and LLM text."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from app.domain.privacy_policy import DEFAULT_PRIVACY_POLICY, PrivacyPolicy

SENSITIVE_COLUMN_NAMES = DEFAULT_PRIVACY_POLICY.pii_columns

# Columns that can be pseudonymized rather than dropped when grouping by customer is needed
PSEUDONYMIZED_ID_COLUMNS = DEFAULT_PRIVACY_POLICY.user_identifier_columns

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
IPV4_REGEX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
STREET_REGEX = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9\s]{2,35}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|"
    r"Court|Ct|Terrace|Ter|Parkway|Pkwy|Place|Pl|Highway|Hwy|Circle|Cir|Trail|Trl)\b",
    re.IGNORECASE,
)
COORDINATE_PAIR_REGEX = re.compile(
    r"(?<![\d.])[-+]?(?:[1-8]?\d\.\d{3,}|90\.0{3,})\s*[,;]\s*"
    r"[-+]?(?:1[0-7]\d\.\d{3,}|[1-9]?\d\.\d{3,}|180\.0{3,})(?![\d.])"
)
LABELED_PII_REGEX = re.compile(
    r"(?P<label>\b(?:first[_\s-]?name|last[_\s-]?name|full[_\s-]?name|"
    r"e-?mail|street[_\s-]?address|address|postal[_\s-]?code|zip(?:code)?|"
    r"latitude|longitude|user[_\s-]?geom|coordinates|phone(?:[_\s-]?number)?|"
    r"ip[_\s-]?address)\b"
    r"[`\"']?\s*(?:=|:|\bis\b|\blike\b)\s*)"
    r"(?P<value>(?:\[[A-Z_]+\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n)\]}]+))",
    re.IGNORECASE,
)


class PIIMasker:
    """Enforces zero-PII leakage across tabular results and natural-language outputs."""

    def __init__(
        self,
        salt: str = "retail-pii-salt-2026",
        policy: PrivacyPolicy | None = None,
    ) -> None:
        self._salt = salt
        self._policy = policy or DEFAULT_PRIVACY_POLICY

    def pseudonymize_id(self, raw_id: Any) -> str:
        digest = hmac.new(
            self._salt.encode("utf-8"),
            str(raw_id).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:12]
        return f"CUST_{digest.upper()}"

    def mask_text(self, text: str) -> tuple[str, int]:
        """Scrub known PII values from free text before external or user-facing use."""
        if not text:
            return text, 0

        redactions = 0

        def _count_sub(pattern: re.Pattern[str], replacement: str, source: str) -> str:
            nonlocal redactions
            new_text, count = pattern.subn(replacement, source)
            redactions += count
            return new_text

        masked = _count_sub(EMAIL_REGEX, "[REDACTED_EMAIL]", text)
        masked = _count_sub(PHONE_REGEX, "[REDACTED_PHONE]", masked)
        masked = _count_sub(IPV4_REGEX, "[REDACTED_IP]", masked)
        masked = _count_sub(STREET_REGEX, "[REDACTED_ADDRESS]", masked)
        masked = _count_sub(COORDINATE_PAIR_REGEX, "[REDACTED_COORDINATES]", masked)

        def _replace_labeled(match: re.Match[str]) -> str:
            nonlocal redactions
            redactions += 1
            return f"{match.group('label')}[REDACTED_PII]"

        masked = LABELED_PII_REGEX.sub(_replace_labeled, masked)
        return masked, redactions

    def mask_rows(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Masks sensitive columns and scans string values across all query result rows."""
        masked_rows: list[dict[str, Any]] = []
        total_redactions = 0

        for row in rows:
            clean_row: dict[str, Any] = {}
            for col, val in row.items():
                col_lower = col.lower()
                if self._policy.is_pii_column(col_lower):
                    clean_row[col] = "[REDACTED_PII]"
                    total_redactions += 1
                elif self._policy.is_user_identifier_column(col_lower) and val is not None:
                    clean_row[col] = self.pseudonymize_id(val)
                elif isinstance(val, str):
                    clean_val, count = self.mask_text(val)
                    clean_row[col] = clean_val
                    total_redactions += count
                else:
                    clean_row[col] = val
            masked_rows.append(clean_row)

        return masked_rows, total_redactions
