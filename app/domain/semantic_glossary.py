"""Domain models and catalog for the retail semantic glossary of business metrics."""

from __future__ import annotations

import contextlib
import re
from typing import Any

from pydantic import BaseModel, Field


class MetricDefinition(BaseModel):
    """Business rule and standard SQL definition for a retail metric."""

    key: str = Field(..., description="Unique metric identifier, e.g. 'net_revenue'")
    name: str = Field(..., description="Human-readable title, e.g. 'Net Revenue'")
    description: str = Field(..., description="Executive definition of what the metric measures")
    sql_formula: str = Field(..., description="Standard BigQuery SQL calculation expression")
    business_rule: str = Field(
        ..., description="Business logic, inclusions, exclusions, and conventions"
    )
    override_hint: str = Field(
        ...,
        description="Guidance for users on how to override this metric definition in their prompt",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Search keywords used to detect when this metric is referenced",
    )


# Standard Retail Metric Definitions with Sensible Defaults (as per assessment disclaimers)
DEFAULT_METRICS: dict[str, MetricDefinition] = {
    "net_revenue": MetricDefinition(
        key="net_revenue",
        name="Net Revenue",
        description="Total recognized revenue excluding cancelled and returned items.",
        sql_formula="SUM(sale_price) WHERE status NOT IN ('Cancelled', 'Returned')",
        business_rule=(
            "Cancelled and returned orders are strictly excluded from net sales. "
            "For completed orders, sale_price represents actual realized transaction revenue."
        ),
        override_hint="Override example: 'define revenue including returned items' or 'calculate gross revenue'",
        keywords=[
            "revenue",
            "sales",
            "net sales",
            "net revenue",
            "spend",
            "top line",
            "underspending",
        ],
    ),
    "gmv": MetricDefinition(
        key="gmv",
        name="Gross Merchandise Value (GMV)",
        description="Total value of merchandise ordered over a period before returns and refunds.",
        sql_formula="SUM(sale_price) WHERE status != 'Cancelled'",
        business_rule=(
            "Reflects gross demand placed by customers. Excludes orders cancelled before shipment, "
            "but includes items that may be returned post-delivery."
        ),
        override_hint="Override example: 'define GMV including cancelled orders'",
        keywords=["gmv", "gross merchandise value", "gross sales", "gross volume"],
    ),
    "churn_rate": MetricDefinition(
        key="churn_rate",
        name="Customer Churn Rate",
        description="Customer churn: buyers with completed orders who placed no purchases in the last 90 days.",
        sql_formula=(
            "SAFE_DIVIDE(COUNT(DISTINCT churned_user_id), COUNT(DISTINCT total_user_id)) * 100 "
            "(churned = no orders in past 90 days)"
        ),
        business_rule=(
            "Default customer churn threshold is 90 days of purchase inactivity following a completed order."
        ),
        override_hint="Override example: 'define churn as no purchase in 60 days' or 'calculate churn for 60 days'",
        keywords=[
            "churn",
            "churned",
            "retention",
            "attrition",
            "lost customer",
            "отток",
        ],
    ),
    "gross_margin": MetricDefinition(
        key="gross_margin",
        name="Gross Margin",
        description="Percentage of net sales retained after subtracting cost of goods sold (COGS).",
        sql_formula="ROUND(SAFE_DIVIDE(SUM(oi.sale_price - p.cost), SUM(oi.sale_price)) * 100, 2)",
        business_rule=(
            "Requires joining order_items with products on order_items.product_id = products.id. "
            "Excludes cancelled and returned items."
        ),
        override_hint="Override example: 'define gross margin in absolute USD dollars' or 'calculate margin without product cost'",
        keywords=["margin", "gross margin", "profit", "profitability", "cogs", "markup"],
    ),
    "repeat_customer_rate": MetricDefinition(
        key="repeat_customer_rate",
        name="Repeat Customer Rate",
        description="Percentage of active purchasers who have placed 2 or more completed orders.",
        sql_formula=(
            "ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN completed_orders >= 2 THEN user_id END), "
            "COUNT(DISTINCT user_id)) * 100, 2)"
        ),
        business_rule=(
            "Evaluates customer retention and brand loyalty as share of customers with 2 or more completed orders."
        ),
        override_hint="Override example: 'define repeat customer as placed 3 or more orders'",
        keywords=[
            "repeat",
            "repeat customer",
            "repeat purchase",
            "loyalty",
            "re-order",
            "frequent buyer",
            "повторн",
        ],
    ),
    "aov": MetricDefinition(
        key="aov",
        name="Average Order Value (AOV)",
        description="Average monetary value generated per completed order.",
        sql_formula="ROUND(SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)), 2) WHERE status = 'Complete'",
        business_rule=(
            "Calculated as total net sale price divided by count of distinct completed orders."
        ),
        override_hint="Override example: 'calculate AOV across all orders including returned items'",
        keywords=[
            "aov",
            "average order value",
            "basket size",
            "avg order",
            "average spend per order",
        ],
    ),
}


class SemanticGlossary:
    """Catalog of retail business metric definitions with prompt formatting and override support."""

    def __init__(
        self, custom_definitions: dict[str, Any] | None = None, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._metrics: dict[str, MetricDefinition] = dict(DEFAULT_METRICS)
        if custom_definitions:
            for key, val in custom_definitions.items():
                if isinstance(val, MetricDefinition):
                    self._metrics[key] = val
                elif isinstance(val, dict):
                    self._metrics[key] = MetricDefinition(**val)
                elif isinstance(val, str) and key in self._metrics:
                    existing = self._metrics[key]
                    self._metrics[key] = existing.model_copy(
                        update={"description": val, "business_rule": val}
                    )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_metric(self, key: str) -> MetricDefinition | None:
        return self._metrics.get(key)

    def list_all(self) -> list[MetricDefinition]:
        return list(self._metrics.values())

    def format_system_prompt_section(self) -> str:
        """Render markdown section for the BigQuery SQL generator prompt."""
        if not self._enabled:
            return ""

        lines = [
            "BUSINESS METRIC DEFINITIONS (Lightweight Semantic Glossary):",
            "Follow these standard definitions when writing SQL queries unless the user explicitly overrides them:",
        ]
        for m in self._metrics.values():
            lines.append(f"- **{m.name}** (`{m.key}`): {m.sql_formula}. {m.business_rule}")

        lines.extend(
            [
                "",
                "METRIC OVERRIDE RULE:",
                "Users may override any metric definition in their natural language question "
                "(e.g., 'define churn as no purchase in 60 days', 'calculate revenue including returns', "
                "or 'define repeat customer as 3+ orders'). "
                "When a user provides their own definition, ALWAYS prioritize the user's custom definition "
                "over the default glossary definitions above.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_churn_days(low: str) -> int | None:
        """Extract user-specified churn inactivity days (e.g., 60 days)."""
        patterns = [
            r"interval\s+(\d+)\s+day",
            r"(?:churn|отток|inactivity|покупок|purchase|purchases|orders?)[^\n.]{0,40}?(\d+)\s*(?:days?|дней|дня|дн)",
            r"(\d+)\s*(?:days?|дней|дня|дн)[^\n.]{0,40}?(?:churn|отток|inactivity|покупок|purchase|purchases|orders?)",
            r"(?:churn|отток)[^\n.]{0,30}?(\d+)",
        ]
        for pat in patterns:
            match = re.search(pat, low)
            if match:
                with contextlib.suppress(ValueError):
                    val = int(match.group(1))
                    if 0 < val <= 3650:
                        return val
        return None

    def churn_inactivity_days(self, user_message: str) -> int:
        """Resolve a user override or configured default churn threshold."""
        user_override = self._extract_churn_days(user_message.lower())
        if user_override is not None:
            return user_override

        churn_metric = self.get_metric("churn_rate")
        if churn_metric is None:
            return 90

        configured_definition = " ".join(
            [
                churn_metric.description,
                churn_metric.sql_formula,
                churn_metric.business_rule,
            ]
        ).lower()
        return self._extract_churn_days(configured_definition) or 90

    @staticmethod
    def _includes_returns(text: str) -> bool:
        low = text.lower()
        return any(
            phrase in low
            for phrase in [
                "including return",
                "include returned",
                "includes returned",
                "с возвратами",  # noqa: RUF001
                "с учетом возвратов",  # noqa: RUF001
            ]
        )

    def revenue_includes_returns(self, user_message: str) -> bool:
        """Return whether the request or configured definition includes returned items."""
        if not any(metric.key == "net_revenue" for metric in self.detect_metrics(user_message)):
            return False
        if self._includes_returns(user_message):
            return True

        revenue_metric = self.get_metric("net_revenue")
        if revenue_metric is None:
            return False
        return self._includes_returns(
            " ".join(
                [
                    revenue_metric.description,
                    revenue_metric.sql_formula,
                    revenue_metric.business_rule,
                ]
            )
        )

    @staticmethod
    def _extract_repeat_threshold(low: str) -> int | None:
        """Extract user-specified minimum completed orders for repeat customer rate (e.g. 3+ orders)."""
        patterns = [
            r"completed_orders\s*>=\s*(\d+)",
            r"(?:repeat|повторн)[^\n.]{0,40}?(\d+)\+?\s*(?:orders?|заказ)",
            r"(\d+)\+?\s*(?:orders?|заказ)[^\n.]{0,40}?(?:repeat|повторн)",
            r"(?:repeat customer as|повторные клиенты как)[^\n.]{0,30}?(\d+)",
        ]
        for pat in patterns:
            match = re.search(pat, low)
            if match:
                with contextlib.suppress(ValueError):
                    val = int(match.group(1))
                    if 1 <= val <= 100:
                        return val
        return None

    def detect_metrics(self, text: str) -> list[MetricDefinition]:
        """Identify which metrics are referenced in the question or SQL text,
        and dynamically apply any custom user-specified rule overrides (e.g. 60 days churn).
        """
        if not self._enabled:
            return []

        low = text.lower()
        matched: list[MetricDefinition] = []
        for metric in self._metrics.values():
            if metric.key in low or any(kw in low for kw in metric.keywords):
                resolved_metric = metric
                if metric.key == "churn_rate":
                    custom_days = self._extract_churn_days(low)
                    if custom_days is not None and custom_days != 90:
                        resolved_metric = metric.model_copy(
                            update={
                                "description": (
                                    f"Customer churn: buyers with completed orders who placed no purchases "
                                    f"in the last {custom_days} days (User custom rule: {custom_days} days)."
                                ),
                                "sql_formula": (
                                    f"SAFE_DIVIDE(COUNT(DISTINCT churned_user_id), COUNT(DISTINCT total_user_id)) * 100 "
                                    f"(churned = no orders in past {custom_days} days)"
                                ),
                                "business_rule": (
                                    f"User-specified churn threshold: {custom_days} days of purchase inactivity "
                                    f"following a completed order."
                                ),
                            }
                        )
                elif metric.key == "repeat_customer_rate":
                    custom_orders = self._extract_repeat_threshold(low)
                    if custom_orders is not None and custom_orders != 2:
                        resolved_metric = metric.model_copy(
                            update={
                                "description": (
                                    f"Percentage of active purchasers who have placed {custom_orders} or more completed orders "
                                    f"(User custom rule: {custom_orders}+ orders)."
                                ),
                                "sql_formula": (
                                    f"ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN completed_orders >= {custom_orders} THEN user_id END), "
                                    f"COUNT(DISTINCT user_id)) * 100, 2)"
                                ),
                                "business_rule": (
                                    f"User-specified repeat threshold: {custom_orders} or more completed orders."
                                ),
                            }
                        )
                elif metric.key == "net_revenue" and self._includes_returns(low):
                    resolved_metric = metric.model_copy(
                        update={
                            "name": "Revenue (Including Returns)",
                            "description": (
                                "Total recognized revenue including returned items "
                                "(User custom rule: including returns)."
                            ),
                            "sql_formula": "SUM(sale_price) WHERE status != 'Cancelled'",
                            "business_rule": (
                                "User-specified rule: includes returned items in revenue calculation."
                            ),
                        }
                    )
                matched.append(resolved_metric)
        return matched

    def format_response_definitions(
        self,
        metrics: list[MetricDefinition] | None = None,
        include_override_note: bool = True,
    ) -> str:
        """Format a clear 'Metric Definitions Applied' section for executive reporting."""
        if not self._enabled:
            return ""

        active_metrics = metrics if metrics is not None else self.list_all()
        if not active_metrics:
            return ""

        lines = ["### Metric Definitions Applied"]
        for m in active_metrics:
            lines.append(f"- **{m.name}**: {m.description} *(Formula: `{m.sql_formula}`)*")

        if include_override_note:
            has_custom = any("User custom rule" in m.description for m in active_metrics)
            if has_custom:
                lines.append(
                    "\n*(Note: Custom user-specified metric rule applied for this calculation. "
                    "You can override any metric definition directly in your prompt.)*"
                )
            else:
                lines.append(
                    "\n*(Note: Standard retail glossary definitions applied. "
                    "You can override any metric definition directly in your prompt, e.g. "
                    "'define churn as no purchase in 60 days' or 'calculate revenue including returns'.)*"
                )

        return "\n".join(lines)
