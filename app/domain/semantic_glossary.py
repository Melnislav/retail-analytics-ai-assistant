"""Domain models and catalog for the retail semantic glossary of business metrics."""

from __future__ import annotations

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
        description=(
            "Customer churn: buyers with completed orders who placed no purchases in the last 90 days. "
            "Order churn: share of cancelled and returned orders."
        ),
        sql_formula=(
            "Customer-level: SAFE_DIVIDE(COUNT(DISTINCT churned_user_id), COUNT(DISTINCT total_user_id)) * 100 "
            "(churned = no orders in past 90 days); "
            "Order-level: ROUND(SAFE_DIVIDE(COUNTIF(status IN ('Cancelled', 'Returned')), COUNT(*)) * 100, 2)"
        ),
        business_rule=(
            "Default customer churn threshold is 90 days of purchase inactivity following a completed order. "
            "Order-level cancellation/return rate is used for transactional trend analysis."
        ),
        override_hint="Override example: 'define churn as no purchase in 60 days' or 'define churn as cancelled orders only'",
        keywords=[
            "churn",
            "churned",
            "retention",
            "attrition",
            "lost customer",
            "cancelled",
            "returned",
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
            "ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN order_count > 1 THEN user_id END), "
            "COUNT(DISTINCT user_id)) * 100, 2)"
        ),
        business_rule=(
            "Evaluates customer retention and brand loyalty across shoppers who have completed at least one order."
        ),
        override_hint="Override example: 'define repeat customer as placed 3 or more orders'",
        keywords=[
            "repeat",
            "repeat customer",
            "repeat purchase",
            "loyalty",
            "re-order",
            "frequent buyer",
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

    def detect_metrics(self, text: str) -> list[MetricDefinition]:
        """Identify which metrics are referenced in the question or SQL text."""
        if not self._enabled:
            return []

        low = text.lower()
        matched: list[MetricDefinition] = []
        for metric in self._metrics.values():
            if metric.key in low or any(kw in low for kw in metric.keywords):
                matched.append(metric)
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
            lines.append(
                "\n*(Note: Standard retail glossary definitions applied. "
                "You can override any metric definition directly in your prompt, e.g. "
                "'define churn as no purchase in 60 days' or 'calculate revenue including returns'.)*"
            )

        return "\n".join(lines)
