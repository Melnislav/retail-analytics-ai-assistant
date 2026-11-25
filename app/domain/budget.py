"""Per-turn LLM call count and estimated USD cost guard."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import TurnBudgetExceededError

MODEL_PRICING_PER_1M_TOKENS: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-3-5-sonnet": (3.00, 15.00),
}
DEFAULT_PRICING_PER_1M_TOKENS: tuple[float, float] = (1.00, 3.00)


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model_name: str = "",
) -> float:
    """Estimate USD cost from token counts using configured model pricing."""
    normalized = model_name.lower()
    in_price, out_price = DEFAULT_PRICING_PER_1M_TOKENS
    for prefix, pricing in MODEL_PRICING_PER_1M_TOKENS.items():
        if prefix in normalized:
            in_price, out_price = pricing
            break
    safe_in = max(0, input_tokens)
    safe_out = max(0, output_tokens)
    return (safe_in / 1_000_000.0) * in_price + (safe_out / 1_000_000.0) * out_price


@dataclass
class TurnBudget:
    """Tracks per-turn LLM calls, token usage, and cost ceilings."""

    max_llm_calls: int = 5
    max_cost_usd: float = 1.0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost_usd: float = 0.0

    def check_can_call(self) -> None:
        """Fail fast before invoking an LLM if call or cost ceilings are reached."""
        if self.llm_calls >= self.max_llm_calls:
            raise TurnBudgetExceededError(
                f"Per-turn LLM call limit reached ({self.llm_calls}/{self.max_llm_calls})."
            )
        if self.total_cost_usd >= self.max_cost_usd:
            raise TurnBudgetExceededError(
                f"Per-turn cost ceiling reached (${self.total_cost_usd:.4f}/${self.max_cost_usd:.2f})."
            )

    def record_call(self) -> None:
        """Record one LLM invocation and enforce the call count ceiling."""
        self.llm_calls += 1
        if self.llm_calls > self.max_llm_calls:
            raise TurnBudgetExceededError(
                f"Per-turn LLM call limit exceeded ({self.llm_calls}/{self.max_llm_calls})."
            )

    def record_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model_name: str = "",
    ) -> None:
        """Accumulate token usage and enforce the per-turn USD cost ceiling."""
        safe_in = max(0, int(input_tokens))
        safe_out = max(0, int(output_tokens))
        self.input_tokens += safe_in
        self.output_tokens += safe_out
        self.total_cost_usd += estimate_cost_usd(safe_in, safe_out, model_name)
        if self.total_cost_usd > self.max_cost_usd:
            raise TurnBudgetExceededError(
                f"Per-turn cost ceiling exceeded (${self.total_cost_usd:.4f}/${self.max_cost_usd:.2f})."
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize budget usage for response observability metadata."""
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.total_cost_usd, 6),
            "max_llm_calls": self.max_llm_calls,
            "max_cost_usd": self.max_cost_usd,
        }


_ACTIVE_TURN_BUDGET: ContextVar[TurnBudget | None] = ContextVar("active_turn_budget", default=None)


def get_active_turn_budget() -> TurnBudget | None:
    """Return the active turn budget in the current async task context, if any."""
    return _ACTIVE_TURN_BUDGET.get()


@contextmanager
def turn_budget_scope(budget: TurnBudget) -> Iterator[TurnBudget]:
    """Bind a TurnBudget to the current execution context."""
    token = _ACTIVE_TURN_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_TURN_BUDGET.reset(token)
