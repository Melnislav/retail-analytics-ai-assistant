"""Saved Reports management and High-Stakes Destructive Operation oversight."""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, timedelta
from uuid import uuid4

from app.application.ports import ConfirmationStorePort, ObservabilityPort, ReportRepositoryPort
from app.domain.exceptions import ConfirmationValidationError
from app.domain.models import (
    ConfirmationAction,
    PendingConfirmation,
    SavedReport,
    UserContext,
    utc_now,
)

logger = logging.getLogger(__name__)


class ReportService:
    """Manages user-owned reports and enforces strict 2-step confirmation for deletions."""

    def __init__(
        self,
        repo: ReportRepositoryPort,
        confirmation_store: ConfirmationStorePort,
        observability: ObservabilityPort,
        confirmation_ttl_seconds: int = 600,
    ) -> None:
        self._repo = repo
        self._confirmations = confirmation_store
        self._obs = observability
        self._confirmation_ttl_seconds = confirmation_ttl_seconds

    async def list_user_reports(self, user: UserContext) -> list[SavedReport]:
        return await self._repo.list_by_owner(user.user_id)

    async def get_user_report(self, user: UserContext, report_id: str) -> SavedReport | None:
        return await self._repo.get_by_id(user.user_id, report_id.strip())

    async def save_report(self, report: SavedReport) -> SavedReport:
        saved = await self._repo.create(report)
        self._obs.increment_metric("reports_saved_total")
        logger.info("Saved report %s for user %s", saved.report_id, saved.owner_user_id)
        return saved

    async def prepare_deletion(
        self,
        user: UserContext,
        session_id: str,
        scope: str,
        keyword: str | None = None,
    ) -> PendingConfirmation | None:
        """Finds matching user-owned reports and stages a SHA-256 snapshot confirmation."""
        if scope == "conversation":
            matches = await self._repo.find_by_session(user.user_id, session_id)
            desc = f"Delete {len(matches)} report(s) created in current session '{session_id}'."
        elif scope == "all":
            matches = await self._repo.list_by_owner(user.user_id)
            desc = f"Delete all {len(matches)} saved report(s) owned by your account."
        else:
            search_term = (keyword or "").strip()
            if not search_term:
                return None
            matches = await self._repo.find_by_keyword(user.user_id, search_term)
            desc = f"Delete {len(matches)} report(s) mentioning '{search_term}'."

        if not matches:
            return None

        ordered_matches = sorted(
            matches,
            key=lambda report: (report.created_at.isoformat(), report.report_id),
            reverse=True,
        )
        target_ids = [report.report_id for report in ordered_matches]
        target_titles = [report.title for report in ordered_matches]
        target_created_at = [
            report.created_at.strftime("%Y-%m-%d %H:%M UTC") for report in ordered_matches
        ]
        nonce = uuid4().hex[:16]
        payload_hash = PendingConfirmation.compute_payload_hash(target_ids, user.user_id, nonce)
        created_at = utc_now()
        expires_at = created_at + timedelta(seconds=self._confirmation_ttl_seconds)

        pending = PendingConfirmation(
            user_id=user.user_id,
            session_id=session_id,
            action=ConfirmationAction.DELETE_REPORTS,
            description=desc,
            target_report_ids=target_ids,
            target_report_titles=target_titles,
            target_report_created_at=target_created_at,
            nonce=nonce,
            payload_hash=payload_hash,
            created_at=created_at,
            expires_at=expires_at,
        )
        await self._confirmations.save_pending(pending)
        self._obs.increment_metric("destructive_confirmations_staged")
        return pending

    async def execute_confirmed_deletion(
        self,
        confirmation: PendingConfirmation,
        user: UserContext,
    ) -> int:
        """Verifies owner, expiry, snapshot hash, and TOCTOU state before transactional delete."""
        if confirmation.user_id != user.user_id:
            raise ConfirmationValidationError(
                "Confirmation token does not belong to the current user. No reports were deleted."
            )

        expires_at = (
            confirmation.expires_at.replace(tzinfo=UTC)
            if confirmation.expires_at is not None and confirmation.expires_at.tzinfo is None
            else confirmation.expires_at
        )
        if expires_at is not None and utc_now() > expires_at:
            raise ConfirmationValidationError(
                "Confirmation token has expired. No reports were deleted."
            )

        expected_hash = PendingConfirmation.compute_payload_hash(
            confirmation.target_report_ids,
            confirmation.user_id,
            confirmation.nonce,
        )
        if not confirmation.payload_hash or not hmac.compare_digest(
            confirmation.payload_hash, expected_hash
        ):
            raise ConfirmationValidationError(
                "Confirmation snapshot integrity check failed. No reports were deleted."
            )

        deleted_count = await self._repo.delete_by_ids(
            owner_user_id=user.user_id,
            report_ids=confirmation.target_report_ids,
            require_exact_match=True,
        )
        self._obs.increment_metric("reports_deleted_total", deleted_count)
        logger.warning(
            "AUDIT: User %s confirmed deletion of %d report(s) (token=%s hash=%s)",
            user.user_id,
            deleted_count,
            confirmation.token,
            confirmation.payload_hash[:12],
        )
        return deleted_count
