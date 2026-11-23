"""Deterministic PII masking and redaction engine for raw transaction rows and LLM text."""

from __future__ import annotations

import hashlib
import re
from typing import Any

SENSITIVE_COLUMN_NAMES = frozenset(
    {
        "email",
        "user_email",
        "customer_email",
        "first_name",
        "last_name",
        "full_name",
        "name",
        "street_address",
        "address",
        "postal_code",
        "zip",
        "zipcode",
        "latitude",
        "longitude",
        "ip_address",
        "phone",
        "phone_number",
        "ssn",
    }
)

# Columns that can be pseudonymized rather than dropped when grouping by customer is needed
PSEUDONYMIZED_ID_COLUMNS = frozenset({"user_id", "customer_id"})

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_REGEX = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
IPV4_REGEX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
STREET_REGEX = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9\s]{2,35}\s+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct)\b",
    re.IGNORECASE,
)


class PIIMasker:
    """Enforces zero-PII leakage across tabular results and natural-language outputs."""

    def __init__(self, salt: str = "opsfleet-pii-salt-2026") -> None:
        self._salt = salt

    def pseudonymize_id(self, raw_id: Any) -> str:
        digest = hashlib.sha256(f"{self._salt}:{raw_id}".encode()).hexdigest()[:10]
        return f"CUST_{digest.upper()}"

    def mask_text(self, text: str) -> tuple[str, int]:
        """Scrubs emails, phone numbers, IPv4 addresses, and street addresses from text."""
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
        return masked, redactions

    def mask_rows(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Masks sensitive columns and scans string values across all query result rows."""
        masked_rows: list[dict[str, Any]] = []
        total_redactions = 0

        for row in rows:
            clean_row: dict[str, Any] = {}
            for col, val in row.items():
                col_lower = col.lower()
                if col_lower in SENSITIVE_COLUMN_NAMES:
                    clean_row[col] = "[REDACTED_PII]"
                    total_redactions += 1
                elif col_lower in PSEUDONYMIZED_ID_COLUMNS and val is not None:
                    clean_row[col] = self.pseudonymize_id(val)
                elif isinstance(val, str):
                    clean_val, count = self.mask_text(val)
                    clean_row[col] = clean_val
                    total_redactions += count
                else:
                    clean_row[col] = val
            masked_rows.append(clean_row)

        return masked_rows, total_redactions
