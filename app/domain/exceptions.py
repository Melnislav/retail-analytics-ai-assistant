"""Domain and application exception hierarchy."""

from __future__ import annotations


class DomainError(Exception):
    """Base exception for all domain-level failures."""

    def __init__(self, message: str, code: str = "DOMAIN_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class SecurityViolationError(DomainError):
    """Raised when prompt injection, off-topic abuse, or unauthorized query occurs."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SECURITY_VIOLATION")


class SQLSafetyError(DomainError):
    """Raised when generated SQL violates read-only, table allowlist, or RBAC rules."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="SQL_SAFETY_VIOLATION")


class UnauthorizedDataAccessError(DomainError):
    """Raised when a user tries to access products or reports outside their scope."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNAUTHORIZED_ACCESS")


class QueryExecutionError(DomainError):
    """Raised when BigQuery execution fails after all self-correction attempts."""

    def __init__(self, message: str, last_sql: str | None = None) -> None:
        super().__init__(message, code="QUERY_EXECUTION_FAILED")
        self.last_sql = last_sql


class ConfirmationRequiredError(DomainError):
    """Raised or returned when a destructive operation awaits human confirmation."""

    def __init__(self, message: str, token: str) -> None:
        super().__init__(message, code="CONFIRMATION_REQUIRED")
        self.token = token


class ConfirmationValidationError(DomainError):
    """Raised when a destructive confirmation fails owner, expiry, hash, or TOCTOU checks."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="CONFIRMATION_VALIDATION_FAILED")


class ExternalServiceUnavailableError(DomainError):
    """Raised when an external dependency (LLM, BigQuery) trips its circuit breaker."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service} unavailable: {message}", code="SERVICE_UNAVAILABLE")
        self.service = service
