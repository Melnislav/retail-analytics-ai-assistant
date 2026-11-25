"""Domain models and catalog for the retail semantic glossary of business metrics."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel, Field


class SessionGlossaryOverrides(BaseModel):
    """Per-session metric parameter overrides applied without mutating global defaults."""

    churn_days: int | None = None
    repeat_min_orders: int | None = None
    revenue_include_returns: bool | None = None
    aov_include_returns: bool | None = None

    def is_empty(self) -> bool:
        return (
            self.churn_days is None
            and self.repeat_min_orders is None
            and self.revenue_include_returns is None
            and self.aov_include_returns is None
        )

    def merge(self, other: SessionGlossaryOverrides) -> SessionGlossaryOverrides:
        return SessionGlossaryOverrides(
            churn_days=other.churn_days if other.churn_days is not None else self.churn_days,
            repeat_min_orders=(
                other.repeat_min_orders
                if other.repeat_min_orders is not None
                else self.repeat_min_orders
            ),
            revenue_include_returns=(
                other.revenue_include_returns
                if other.revenue_include_returns is not None
                else self.revenue_include_returns
            ),
            aov_include_returns=(
                other.aov_include_returns
                if other.aov_include_returns is not None
                else self.aov_include_returns
            ),
        )


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
        description=(
            "Percentage of active purchasers who have placed 2 or more completed orders within the same brand."
        ),
        sql_formula=(
            "ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN completed_orders >= 2 THEN user_id END), "
            "COUNT(DISTINCT user_id)) * 100, 2)"
        ),
        business_rule=(
            "Evaluates brand loyalty as the share of customers with 2 or more completed orders within that brand."
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


DEFAULT_METRIC_CLARIFICATION_MESSAGES: dict[str, str] = {
    "unsupported_cancelled_order_churn_override": (
        "I cannot calculate customer churn as cancelled orders only because cancellations are a "
        "separate metric. Please define churn as no purchases for a number of days, or ask for "
        "the cancellation rate."
    ),
    "unsupported_configured_churn_scope": (
        "I cannot calculate customer churn for a restricted audience because the configured "
        "audience filter is not supported. Please define churn as no purchases for a number of "
        "days without an audience restriction."
    ),
}


class SemanticGlossary:
    """Catalog of retail business metric definitions with prompt formatting and override support."""

    _CHURN_DAY_PATTERNS: tuple[str, ...] = (
        r"interval\s+(\d+)\s+day",
        r"(?:churn|отток|inactivity|покупок|purchase|purchases|orders?)[^\n.]{0,50}?\b(\d+)\s*(?:days?|дней|дня|дн)\b",
        r"\b(\d+)\s*(?:days?|дней|дня|дн)\b[^\n.]{0,40}?(?:churn|отток|inactivity|покупок|purchase|purchases|orders?)",
    )

    def __init__(
        self,
        custom_definitions: dict[str, Any] | None = None,
        enabled: bool = True,
        clarification_messages: dict[str, str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._metrics: dict[str, MetricDefinition] = dict(DEFAULT_METRICS)
        self._clarification_messages = {
            **DEFAULT_METRIC_CLARIFICATION_MESSAGES,
            **(clarification_messages or {}),
        }
        self._custom_churn_definition: str | None = None
        if custom_definitions:
            for key, val in custom_definitions.items():
                if isinstance(val, MetricDefinition):
                    self._metrics[key] = val
                elif isinstance(val, dict):
                    self._metrics[key] = MetricDefinition(**val)
                elif isinstance(val, str) and key in self._metrics:
                    existing = self._metrics[key]
                    update: dict[str, str] = {"description": val, "business_rule": val}
                    if key == "churn_rate":
                        self._custom_churn_definition = val
                        churn_days = self._extract_churn_days(val.lower())
                        if churn_days is not None:
                            update["sql_formula"] = (
                                "SAFE_DIVIDE(COUNT(DISTINCT churned_user_id), "
                                "COUNT(DISTINCT total_user_id)) * 100 "
                                f"(churned = no orders in past {churn_days} days)"
                            )
                    elif key == "repeat_customer_rate":
                        min_orders = self._extract_repeat_threshold(val.lower())
                        if min_orders is not None:
                            update["sql_formula"] = (
                                "ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN completed_orders "
                                f">= {min_orders} THEN user_id END), "
                                "COUNT(DISTINCT user_id)) * 100, 2)"
                            )
                    elif key == "aov" and self._includes_returns(val):
                        update["sql_formula"] = (
                            "ROUND(SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)), 2) "
                            "WHERE status != 'Cancelled'"
                        )
                    self._metrics[key] = existing.model_copy(update=update)

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

    @classmethod
    def _extract_churn_days(cls, low: str) -> int | None:
        """Extract user-specified churn inactivity days (e.g., 60 days)."""
        for pat in cls._CHURN_DAY_PATTERNS:
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
    def has_unsupported_cancelled_order_churn_override(user_message: str) -> bool:
        """Return whether the request defines customer churn by cancelled orders."""
        low = user_message.lower()
        if not ("churn" in low or "отток" in low):
            return False

        has_cancellation = any(term in low for term in ("cancel", "отмен"))
        explicit_override = bool(
            re.search(
                r"(?:define|defined|defining|treat|consider)\s+(?:customer\s+)?churn|"
                r"(?:customer\s+)?churn\s+(?:as|from|using|is)|"
                r"(?:считать|определ\w*)[^.\n]{0,50}(?:отток|churn)|"
                r"(?:отток|churn)[^.\n]{0,50}(?:считать|определ\w*)",
                low,
            )
        )
        return has_cancellation and explicit_override

    def has_unsupported_configured_churn_scope(self, user_message: str) -> bool:
        """Return whether a configured churn rule requires an unsupported audience filter."""
        if not self._custom_churn_definition or not any(
            term in user_message.lower() for term in ("churn", "отток")
        ):
            return False

        return bool(
            re.search(
                r"\b(?:vip|segment(?:s)?|brand|category|region|state|country|store|channel)\b",
                self._custom_churn_definition.lower(),
            )
        )

    def clarification_message(self, key: str) -> str:
        """Return a configured, English-language clarification message."""
        return self._clarification_messages[key]

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

    def aov_includes_returns(self, user_message: str) -> bool:
        """Return whether the AOV rule includes returned items."""
        if not any(metric.key == "aov" for metric in self.detect_metrics(user_message)):
            return False
        if self._includes_returns(user_message):
            return True

        aov_metric = self.get_metric("aov")
        if aov_metric is None:
            return False
        return self._includes_returns(
            " ".join(
                [
                    aov_metric.description,
                    aov_metric.sql_formula,
                    aov_metric.business_rule,
                ]
            )
        )

    @staticmethod
    def _extract_repeat_threshold(low: str) -> int | None:
        """Extract user-specified minimum completed orders for repeat customer rate (e.g. 3+ orders)."""
        patterns = [
            r"completed_orders\s*>=\s*(\d+)",
            r"(?:repeat|повторн)[^\n.]{0,40}?(\d+)\s*(?:\+|or more)?\s*(?:completed\s+)?(?:orders?|заказ)",
            r"(\d+)\s*(?:\+|or more)?\s*(?:completed\s+)?(?:orders?|заказ)[^\n.]{0,40}?(?:repeat|повторн)",
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

    def repeat_customer_min_orders(self, user_message: str) -> int:
        """Resolve a user override or configured default repeat-order threshold."""
        user_override = self._extract_repeat_threshold(user_message.lower())
        if user_override is not None:
            return user_override

        repeat_metric = self.get_metric("repeat_customer_rate")
        if repeat_metric is None:
            return 2
        return (
            self._extract_repeat_threshold(
                " ".join(
                    [
                        repeat_metric.description,
                        repeat_metric.sql_formula,
                        repeat_metric.business_rule,
                    ]
                ).lower()
            )
            or 2
        )

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
                elif metric.key == "aov" and self._includes_returns(low):
                    resolved_metric = metric.model_copy(
                        update={
                            "description": (
                                "Average monetary value per order, including returned items "
                                "(User custom rule: including returns)."
                            ),
                            "sql_formula": (
                                "ROUND(SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)), 2) "
                                "WHERE status != 'Cancelled'"
                            ),
                            "business_rule": (
                                "User-specified rule: includes returned items and excludes cancelled items."
                            ),
                        }
                    )
                matched.append(resolved_metric)
        return matched

    @staticmethod
    def _excludes_returns(text: str) -> bool:
        low = text.lower()
        return any(
            phrase in low
            for phrase in [
                "excluding return",
                "exclude returned",
                "excludes returned",
                "без возвратов",
                "исключая возвраты",
            ]
        )

    def extract_session_overrides(
        self, user_message: str
    ) -> tuple[SessionGlossaryOverrides, str | None]:
        """Parse and range-validate explicit metric overrides requested in conversation."""
        low = user_message.lower()
        churn_days: int | None = None
        if "churn" in low or "отток" in low:
            for pat in self._CHURN_DAY_PATTERNS:
                match = re.search(pat, low)
                if match:
                    val = int(match.group(1))
                    if val < 1 or val > 365:
                        return (
                            SessionGlossaryOverrides(),
                            "Invalid churn window: inactivity threshold must be between 1 and 365 days.",
                        )
                    churn_days = val
                    break

        repeat_min_orders: int | None = None
        if "repeat" in low or "повторн" in low:
            explicit_repeat = self._extract_repeat_threshold(low)
            if explicit_repeat is not None:
                repeat_min_orders = explicit_repeat

        revenue_include_returns: bool | None = None
        if any(term in low for term in ("revenue", "sales", "выруч", "продаж")):
            if self._includes_returns(low):
                revenue_include_returns = True
            elif self._excludes_returns(low):
                revenue_include_returns = False

        aov_include_returns: bool | None = None
        if any(term in low for term in ("aov", "average order", "basket size")):
            if self._includes_returns(low):
                aov_include_returns = True
            elif self._excludes_returns(low):
                aov_include_returns = False

        return (
            SessionGlossaryOverrides(
                churn_days=churn_days,
                repeat_min_orders=repeat_min_orders,
                revenue_include_returns=revenue_include_returns,
                aov_include_returns=aov_include_returns,
            ),
            None,
        )

    def with_session_overrides(
        self, overrides: SessionGlossaryOverrides | None
    ) -> SemanticGlossary:
        """Return a non-mutating copy with session-level metric overrides applied."""
        if overrides is None or overrides.is_empty() or not self._enabled:
            return self

        scoped = SemanticGlossary(
            enabled=self._enabled,
            clarification_messages=dict(self._clarification_messages),
        )
        scoped._metrics = dict(self._metrics)
        scoped._custom_churn_definition = self._custom_churn_definition

        if overrides.churn_days is not None and "churn_rate" in scoped._metrics:
            days = overrides.churn_days
            scoped._metrics["churn_rate"] = scoped._metrics["churn_rate"].model_copy(
                update={
                    "description": (
                        "Customer churn: buyers with completed orders who placed no purchases "
                        f"in the last {days} days (User custom rule: {days} days)."
                    ),
                    "sql_formula": (
                        "SAFE_DIVIDE(COUNT(DISTINCT churned_user_id), "
                        "COUNT(DISTINCT total_user_id)) * 100 "
                        f"(churned = no orders in past {days} days)"
                    ),
                    "business_rule": (
                        f"User-specified churn threshold: {days} days of purchase inactivity "
                        "following a completed order."
                    ),
                }
            )

        if overrides.repeat_min_orders is not None and "repeat_customer_rate" in scoped._metrics:
            min_orders = overrides.repeat_min_orders
            scoped._metrics["repeat_customer_rate"] = scoped._metrics[
                "repeat_customer_rate"
            ].model_copy(
                update={
                    "description": (
                        f"Percentage of active purchasers who have placed {min_orders} or more "
                        f"completed orders (User custom rule: {min_orders}+ orders)."
                    ),
                    "sql_formula": (
                        "ROUND(SAFE_DIVIDE(COUNT(DISTINCT CASE WHEN completed_orders >= "
                        f"{min_orders} THEN user_id END), COUNT(DISTINCT user_id)) * 100, 2)"
                    ),
                    "business_rule": (
                        f"User-specified repeat threshold: {min_orders} or more completed orders."
                    ),
                }
            )

        if overrides.revenue_include_returns is not None and "net_revenue" in scoped._metrics:
            if overrides.revenue_include_returns:
                scoped._metrics["net_revenue"] = scoped._metrics["net_revenue"].model_copy(
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
            else:
                scoped._metrics["net_revenue"] = DEFAULT_METRICS["net_revenue"]

        if overrides.aov_include_returns is not None and "aov" in scoped._metrics:
            if overrides.aov_include_returns:
                scoped._metrics["aov"] = scoped._metrics["aov"].model_copy(
                    update={
                        "description": (
                            "Average monetary value per order, including returned items "
                            "(User custom rule: including returns)."
                        ),
                        "sql_formula": (
                            "ROUND(SAFE_DIVIDE(SUM(sale_price), COUNT(DISTINCT order_id)), 2) "
                            "WHERE status != 'Cancelled'"
                        ),
                        "business_rule": (
                            "User-specified rule: includes returned items and excludes cancelled items."
                        ),
                    }
                )
            else:
                scoped._metrics["aov"] = DEFAULT_METRICS["aov"]

        return scoped

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


_ACTIVE_SESSION_GLOSSARY: ContextVar[SemanticGlossary | None] = ContextVar(
    "active_session_glossary", default=None
)


def get_active_glossary(default: SemanticGlossary) -> SemanticGlossary:
    """Return session-scoped glossary if active in context, otherwise the default instance."""
    return _ACTIVE_SESSION_GLOSSARY.get() or default


@contextmanager
def session_glossary_scope(glossary: SemanticGlossary) -> Iterator[SemanticGlossary]:
    """Bind a session-scoped SemanticGlossary to the current async execution context."""
    token = _ACTIVE_SESSION_GLOSSARY.set(glossary)
    try:
        yield glossary
    finally:
        _ACTIVE_SESSION_GLOSSARY.reset(token)
