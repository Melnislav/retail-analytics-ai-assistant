"""AST-based SQL safety, brand RBAC, and demographic privacy policy."""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from app.domain.models import SQLValidationResult, UserContext, UserRole

ALLOWED_TABLES = frozenset({"orders", "order_items", "products", "users"})
INTERNAL_CTES = frozenset(
    {"__allowed_products", "__scoped_order_items", "__scoped_orders", "__scoped_users"}
)
FORBIDDEN_PII_COLUMNS = frozenset(
    {
        "email",
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
DEMOGRAPHIC_COLUMNS = frozenset({"age", "gender", "city", "state", "country", "traffic_source"})


class SQLPolicyEngine:
    """Validates analytical SQL and enforces brand and privacy boundaries."""

    def __init__(
        self,
        dataset_id: str = "bigquery-public-data.thelook_ecommerce",
        default_limit: int = 200,
        demographic_min_group_size: int = 5,
    ) -> None:
        self._dataset_id = dataset_id
        self._default_limit = default_limit
        self._demographic_min_group_size = demographic_min_group_size
        dataset_parts = dataset_id.split(".")
        if len(dataset_parts) != 2:
            raise ValueError("dataset_id must use '<project>.<dataset>' format")
        self._project_id, self._dataset_name = dataset_parts

    def validate_and_secure(self, raw_sql: str, user: UserContext) -> SQLValidationResult:
        cleaned = raw_sql.strip().rstrip(";")
        if not cleaned:
            return SQLValidationResult(is_valid=False, violations=["Empty SQL query generated."])

        try:
            statements = sqlglot.parse(cleaned, read="bigquery")
        except ParseError as exc:
            return SQLValidationResult(
                is_valid=False,
                violations=[f"Invalid BigQuery SQL: {exc}"],
            )

        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            return SQLValidationResult(
                is_valid=False,
                violations=["Exactly one read-only SELECT/WITH query is required."],
            )

        query = statements[0]
        violations = self._validate_query(query)
        restricted = user.role not in {UserRole.CEO, UserRole.ADMIN}
        if restricted and not user.allowed_brands:
            violations.append(
                "Non-CEO users must have at least one authorized brand; access fails closed."
            )

        if violations:
            return SQLValidationResult(is_valid=False, violations=violations)

        if query.args.get("limit") is None:
            query = query.limit(self._default_limit)

        applied_filters: list[str] = []
        if restricted:
            brand_literals = ", ".join(
                exp.Literal.string(brand).sql(dialect="bigquery") for brand in user.allowed_brands
            )
            applied_filters.append(f"brand IN ({brand_literals})")
            secured_sql = self._apply_brand_scope(query, brand_literals)
        else:
            secured_sql = query.sql(dialect="bigquery")

        return SQLValidationResult(
            is_valid=True,
            sanitized_sql=secured_sql,
            applied_rbac_filters=applied_filters,
        )

    def _validate_query(self, query: exp.Query) -> list[str]:
        violations: list[str] = []
        cte_names = {
            cte.alias_or_name.lower() for cte in query.find_all(exp.CTE) if cte.alias_or_name
        }
        reserved_ctes = sorted(cte_names.intersection(INTERNAL_CTES))
        if reserved_ctes:
            violations.append(
                f"Reserved security CTE names cannot be supplied: {', '.join(reserved_ctes)}."
            )

        physical_tables: list[exp.Table] = []
        for table in query.find_all(exp.Table):
            table_name = table.name.lower()
            is_cte_reference = not table.catalog and not table.db and table_name in cte_names
            if is_cte_reference:
                continue
            physical_tables.append(table)

            if table_name not in ALLOWED_TABLES:
                violations.append(
                    f"Access to table '{table.name}' is not permitted. "
                    f"Allowed tables: {sorted(ALLOWED_TABLES)}."
                )
                continue
            if table.catalog and table.catalog.lower() != self._project_id.lower():
                violations.append(f"Table '{table.sql()}' is outside the configured project.")
            if table.db and table.db.lower() != self._dataset_name.lower():
                violations.append(f"Table '{table.sql()}' is outside the configured dataset.")

        users_aliases = {
            table.alias_or_name.lower()
            for table in physical_tables
            if table.name.lower() == "users" and table.alias_or_name
        }

        if any(not isinstance(star.parent, exp.Count) for star in query.find_all(exp.Star)):
            violations.append("Wildcard projections are prohibited because they can expose PII.")

        if any(
            not column.table and column.name.lower() in users_aliases
            for column in query.find_all(exp.Column)
        ):
            violations.append(
                "Whole users-table row references are prohibited because they expose PII."
            )

        forbidden_columns = sorted(
            {
                column.name.lower()
                for column in query.find_all(exp.Column)
                if column.name.lower() in FORBIDDEN_PII_COLUMNS
            }
        )
        if forbidden_columns:
            violations.append(
                "PII source columns are prohibited even when renamed with an alias: "
                + ", ".join(forbidden_columns)
                + "."
            )

        violations.extend(self._validate_demographics(query, physical_tables))
        return violations

    def _validate_demographics(
        self,
        query: exp.Query,
        physical_tables: list[exp.Table],
    ) -> list[str]:
        demographic_columns = [
            column
            for column in query.find_all(exp.Column)
            if column.name.lower() in DEMOGRAPHIC_COLUMNS
        ]
        if not demographic_columns:
            return []

        violations: list[str] = []
        users_aliases = {
            table.alias_or_name.lower()
            for table in physical_tables
            if table.name.lower() == "users" and table.alias_or_name
        }

        for select in query.find_all(exp.Select):
            select_demographics = [
                column
                for projection in select.expressions
                for column in projection.find_all(exp.Column)
                if column.name.lower() in DEMOGRAPHIC_COLUMNS
            ]
            if not select_demographics:
                continue

            group = select.args.get("group")
            having = select.args.get("having")
            if group is None or having is None:
                violations.append(
                    "Demographic fields may only be returned as groups with a minimum-size HAVING guard."
                )
                continue

            if any(column.table.lower() not in users_aliases for column in select_demographics):
                violations.append(
                    "Demographic fields must be qualified columns from the users table."
                )

            grouped_columns = {
                column.sql(dialect="bigquery").lower()
                for group_expression in group.expressions
                for column in group_expression.find_all(exp.Column)
            }
            projected_demographics = {
                column.sql(dialect="bigquery").lower() for column in select_demographics
            }
            if not projected_demographics.issubset(grouped_columns):
                violations.append(
                    "Every returned demographic field must be an explicit GROUP BY key."
                )

            if not self._has_minimum_group_guard(having, users_aliases):
                violations.append(
                    "Demographic groups must use "
                    f"HAVING COUNT(DISTINCT customer) >= {self._demographic_min_group_size}."
                )

        return violations

    def _has_minimum_group_guard(
        self,
        having: exp.Expression,
        users_aliases: set[str],
    ) -> bool:
        if any(True for _ in having.find_all(exp.Or)):
            return False
        for comparison in having.find_all(exp.GTE):
            count = comparison.this
            threshold = comparison.expression
            if not isinstance(count, exp.Count) or not isinstance(threshold, exp.Literal):
                continue
            if threshold.is_string:
                continue
            try:
                threshold_value = int(threshold.this)
            except (TypeError, ValueError):
                continue
            if threshold_value < self._demographic_min_group_size:
                continue
            distinct = count.this
            if not isinstance(distinct, exp.Distinct):
                continue
            columns = list(distinct.find_all(exp.Column))
            if len(columns) != 1:
                continue
            column = columns[0]
            if column.name.lower() == "id" and column.table.lower() in users_aliases:
                return True
        return False

    def _apply_brand_scope(self, query: exp.Query, brand_literals: str) -> str:
        cte_names = {
            cte.alias_or_name.lower() for cte in query.find_all(exp.CTE) if cte.alias_or_name
        }
        replacements = {
            "products": "__allowed_products",
            "order_items": "__scoped_order_items",
            "orders": "__scoped_orders",
            "users": "__scoped_users",
        }
        for table in query.find_all(exp.Table):
            table_name = table.name.lower()
            if not table.catalog and not table.db and table_name in cte_names:
                continue
            replacement = replacements.get(table_name)
            if replacement is None:
                continue
            table.set("catalog", None)
            table.set("db", None)
            table.set("this", exp.to_identifier(replacement))

        security_ctes = (
            "__allowed_products AS ("
            f"SELECT p.* FROM `{self._dataset_id}.products` AS p "
            f"WHERE p.brand IN ({brand_literals})"
            "), "
            "__scoped_order_items AS ("
            f"SELECT oi.* FROM `{self._dataset_id}.order_items` AS oi "
            "INNER JOIN __allowed_products AS p ON oi.product_id = p.id"
            "), "
            "__scoped_orders AS ("
            f"SELECT o.* FROM `{self._dataset_id}.orders` AS o WHERE EXISTS ("
            "SELECT 1 FROM __scoped_order_items AS oi WHERE oi.order_id = o.order_id)"
            "), "
            "__scoped_users AS ("
            f"SELECT u.* FROM `{self._dataset_id}.users` AS u WHERE EXISTS ("
            "SELECT 1 FROM __scoped_order_items AS oi WHERE oi.user_id = u.id)"
            ")"
        )
        scoped_query = query.sql(dialect="bigquery")
        if scoped_query.upper().startswith("WITH "):
            return f"WITH {security_ctes}, {scoped_query[5:]}"
        return f"WITH {security_ctes} {scoped_query}"
