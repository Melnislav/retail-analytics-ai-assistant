"""Centralized privacy policy value object (Single Source of Truth)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrivacyPolicy:
    """Immutable domain policy defining PII, user identifiers, and demographic k-anonymity."""

    pii_columns: frozenset[str] = frozenset(
        {
            "email",
            "user_email",
            "customer_email",
            "first_name",
            "last_name",
            "full_name",
            "street_address",
            "address",
            "postal_code",
            "zip",
            "zipcode",
            "latitude",
            "longitude",
            "user_geom",
            "coordinates",
            "ip_address",
            "phone",
            "phone_number",
            "ssn",
        }
    )
    demographic_columns: frozenset[str] = frozenset(
        {"age", "gender", "city", "state", "country", "traffic_source"}
    )
    user_identifier_columns: frozenset[str] = frozenset({"user_id", "customer_id"})
    demographic_min_group_size: int = 5

    def __post_init__(self) -> None:
        if self.demographic_min_group_size < 2:
            raise ValueError("demographic_min_group_size must be at least 2")

    def is_pii_column(self, name: str) -> bool:
        return name.lower() in self.pii_columns

    def is_demographic_column(self, name: str) -> bool:
        return name.lower() in self.demographic_columns

    def is_user_identifier_column(self, name: str) -> bool:
        return name.lower() in self.user_identifier_columns


DEFAULT_PRIVACY_POLICY = PrivacyPolicy()
