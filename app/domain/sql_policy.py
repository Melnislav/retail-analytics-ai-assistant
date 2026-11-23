"""SQL safety firewall and product-level RBAC enforcement."""

from __future__ import annotations

import re

from app.domain.models import SQLValidationResult, UserContext, UserRole

ALLOWED_TABLES = frozenset(
    {
        "orders",
        "order_items",
        "products",
        "users",
    }
)

FORBIDDEN_KEYWORDS = frozenset(
    {
        "DROP",
        "DELETE",
        "UPDATE",
        "INSERT",
        "ALTER",
        "TRUNCATE",
        "CREATE",
        "REPLACE",
        "MERGE",
        "GRANT",
        "REVOKE",
        "EXEC",
        "EXECUTE",
        "CALL",
        "EXPORT",
        "LOAD",
    }
)

TABLE_REF_PATTERN = re.compile(
    r"(?:FROM|JOIN)\s+`?(?:[a-zA-Z0-9_-]+\.)?(?:[a-zA-Z0-9_-]+\.)?([a-zA-Z0-9_-]+)`?",
    re.IGNORECASE,
)


class SQLPolicyEngine:
    """Validates read-only SQL constraints and enforces per-user product access control."""

    def __init__(
        self,
        dataset_id: str = "bigquery-public-data.thelook_ecommerce",
        default_limit: int = 200,
    ) -> None:
        self._dataset_id = dataset_id
        self._default_limit = default_limit

    def validate_and_secure(self, raw_sql: str, user: UserContext) -> SQLValidationResult:
        violations: list[str] = []
        applied_filters: list[str] = []

        cleaned = raw_sql.strip().rstrip(";")
        if not cleaned:
            return SQLValidationResult(is_valid=False, violations=["Empty SQL query generated."])

        # Block stacked statements
        if ";" in cleaned:
            violations.append("Multiple SQL statements (semicolons) are prohibited.")

        # Strip comments before keyword inspection
        no_comments = re.sub(r"--.*?$|/\*.*?\*/", " ", cleaned, flags=re.MULTILINE | re.DOTALL)
        upper_tokens = set(re.findall(r"\b[A-Z_]+\b", no_comments.upper()))

        first_word_match = re.match(r"^\s*([A-Z]+)", no_comments.upper())
        first_word = first_word_match.group(1) if first_word_match else ""
        if first_word not in {"SELECT", "WITH"}:
            violations.append(
                f"Only read-only SELECT/WITH queries are allowed (got '{first_word}')."
            )

        blocked_found = sorted(upper_tokens.intersection(FORBIDDEN_KEYWORDS))
        if blocked_found:
            violations.append(f"Forbidden SQL operations detected: {', '.join(blocked_found)}")

        # Check table allowlist
        referenced_tables = [m.group(1).lower() for m in TABLE_REF_PATTERN.finditer(no_comments)]
        # Exclude CTE names defined in WITH clauses
        cte_names = {
            m.group(1).lower()
            for m in re.finditer(r"\b([a-zA-Z0-9_-]+)\s+AS\s*\(", no_comments, flags=re.IGNORECASE)
        }
        for tbl in referenced_tables:
            if tbl not in ALLOWED_TABLES and tbl not in cte_names:
                violations.append(
                    f"Access to table '{tbl}' is not permitted. Allowed tables: {sorted(ALLOWED_TABLES)}."
                )

        if violations:
            return SQLValidationResult(is_valid=False, violations=violations)

        secured_sql = cleaned

        # Enforce product-level RBAC scope when user has restricted categories/departments
        if (
            user.role == UserRole.CATEGORY_MANAGER
            or user.allowed_categories
            or user.allowed_departments
        ):
            if not user.allowed_categories and not user.allowed_departments:
                return SQLValidationResult(
                    is_valid=False,
                    violations=[
                        "Category Manager has no assigned product categories or departments configured."
                    ],
                )

            predicates: list[str] = []
            if user.allowed_categories:
                safe_cats = ", ".join(
                    "'" + c.replace("'", "''") + "'" for c in user.allowed_categories
                )
                predicates.append(f"category IN ({safe_cats})")
                applied_filters.append(f"category IN ({safe_cats})")

            if user.allowed_departments:
                safe_depts = ", ".join(
                    "'" + d.replace("'", "''") + "'" for d in user.allowed_departments
                )
                predicates.append(f"department IN ({safe_depts})")
                applied_filters.append(f"department IN ({safe_depts})")

            rbac_where = " AND ".join(predicates)
            # Wrap products table with a filtered CTE so every reference to `products` is scoped
            scoped_products_cte = (
                f"__scoped_products AS ("
                f"SELECT * FROM `{self._dataset_id}.products` WHERE {rbac_where}"
                f")"
            )
            secured_sql = re.sub(
                rf"`?{re.escape(self._dataset_id)}\.products`?",
                "__scoped_products",
                secured_sql,
                flags=re.IGNORECASE,
            )
            if secured_sql.upper().lstrip().startswith("WITH "):
                secured_sql = re.sub(
                    r"^\s*WITH\s+",
                    f"WITH {scoped_products_cte}, ",
                    secured_sql,
                    count=1,
                    flags=re.IGNORECASE,
                )
            else:
                secured_sql = f"WITH {scoped_products_cte}\n{secured_sql}"

        # Enforce LIMIT if missing to protect compute cost & token budgets
        if not re.search(r"\bLIMIT\s+\d+\b", secured_sql, flags=re.IGNORECASE):
            secured_sql = f"{secured_sql}\nLIMIT {self._default_limit}"

        return SQLValidationResult(
            is_valid=True,
            sanitized_sql=secured_sql,
            applied_rbac_filters=applied_filters,
        )
