"""Circuit Breaker pattern for protecting against cascading external service failures."""

from __future__ import annotations

import logging
import time
from enum import StrEnum

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """In-memory Circuit Breaker tracking consecutive failures and managing cooldown windows."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 2,
        recovery_timeout_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_seconds = max(0.01, recovery_timeout_seconds)
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.state: CircuitState = CircuitState.CLOSED

    def can_execute(self) -> bool:
        """Check if execution is allowed through the circuit breaker."""
        if self.state == CircuitState.CLOSED:
            return True

        now = time.monotonic()
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout_seconds:
                logger.info(
                    "Circuit breaker '%s' entering HALF_OPEN state after %.1fs recovery window.",
                    self.name,
                    self.recovery_timeout_seconds,
                )
                self.state = CircuitState.HALF_OPEN
                return True
            return False

        # In HALF_OPEN state, allow a single canary probe
        return True

    def record_success(self) -> None:
        """Record successful execution; closes circuit and resets failure counter."""
        if self.state != CircuitState.CLOSED:
            logger.info("Circuit breaker '%s' recovered and transitioned to CLOSED.", self.name)
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self, error: Exception | None = None) -> None:
        """Record failed execution (timeout or exception); trips to OPEN on exceeding threshold."""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        err_msg = f" ({error})" if error else ""
        logger.warning(
            "Circuit breaker '%s' recorded failure #%d%s; current state: %s",
            self.name,
            self.failure_count,
            err_msg,
            self.state,
        )

        if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.error(
                    "Circuit breaker '%s' TRIPPED to OPEN! Threshold %d exceeded. Calls will be redirected for %.1fs.",
                    self.name,
                    self.failure_threshold,
                    self.recovery_timeout_seconds,
                )
            self.state = CircuitState.OPEN
