"""Post-SQL defense-in-depth guard enforcing k-anonymity and non-linkage on query rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.privacy_policy import DEFAULT_PRIVACY_POLICY, PrivacyPolicy

USER_COUNT_COLUMNS = frozenset(
    {
        "active_users",
        "user_count",
        "users_count",
        "customer_count",
        "customers_count",
        "customers",
        "distinct_users",
        "total_customers",
        "total_active_customers",
        "unique_users",
        "unique_customers",
    }
)


@dataclass(frozen=True)
class PostSQLGuardResult:
    """Outcome of post-SQL demographic and linkage checks."""

    rows: list[dict[str, Any]]
    masked_groups_count: int
    stripped_demographic_cells: int


class PostSQLDataGuard:
    """Defense-in-depth row guard enforcing k-anonymity and user/demographic non-linkage."""

    def __init__(self, policy: PrivacyPolicy | None = None) -> None:
        self._policy = policy or DEFAULT_PRIVACY_POLICY

    def enforce(self, rows: list[dict[str, Any]]) -> PostSQLGuardResult:
        if not rows:
            return PostSQLGuardResult(rows=[], masked_groups_count=0, stripped_demographic_cells=0)

        guarded_rows: list[dict[str, Any]] = []
        masked_groups_count = 0
        stripped_demographic_cells = 0
        min_group = self._policy.demographic_min_group_size

        for row in rows:
            demo_cols = [col for col in row if self._policy.is_demographic_column(col)]
            if not demo_cols:
                guarded_rows.append(dict(row))
                continue

            has_user_id = any(
                row.get(col) is not None
                and (self._policy.is_user_identifier_column(col) or col.lower() == "id")
                for col in row
            )
            if has_user_id:
                cleaned = dict(row)
                for col in demo_cols:
                    if cleaned[col] != "[REDACTED_DEMOGRAPHIC]":
                        cleaned[col] = "[REDACTED_DEMOGRAPHIC]"
                        stripped_demographic_cells += 1
                guarded_rows.append(cleaned)
                continue

            has_small_group = any(
                isinstance(val, (int, float)) and not isinstance(val, bool) and val < min_group
                for col, val in row.items()
                if col.lower() in USER_COUNT_COLUMNS
            )
            if has_small_group:
                hidden_label = f"[Hidden: group < {min_group}]"
                guarded_rows.append(dict.fromkeys(row, hidden_label))
                masked_groups_count += 1
            else:
                guarded_rows.append(dict(row))

        return PostSQLGuardResult(
            rows=guarded_rows,
            masked_groups_count=masked_groups_count,
            stripped_demographic_cells=stripped_demographic_cells,
        )
