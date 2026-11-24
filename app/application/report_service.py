"""Saved Reports management and High-Stakes Destructive Operation oversight."""

from __future__ import annotations

import logging

from app.application.ports import ConfirmationStorePort, ObservabilityPort, ReportRepositoryPort
from app.domain.models import ConfirmationAction, PendingConfirmation, SavedReport, UserContext

logger = logging.getLogger(__name__)


class ReportService:
    """Manages user-owned reports and enforces strict 2-step confirmation for deletions."""

    def __init__(
        self,
        repo: ReportRepositoryPort,
        confirmation_store: ConfirmationStorePort,
        observability: ObservabilityPort,
    ) -> None:
        self._repo = repo
        self._confirmations = confirmation_store
        self._obs = observability

    async def list_user_reports(self, user: UserContext) -> list[SavedReport]:
        return await self._repo.list_by_owner(user.user_id)

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
        """Finds matching user-owned reports and stages a pending confirmation token."""
        if scope == "conversation":
            matches = await self._repo.find_by_session(user.user_id, session_id)
            desc = f"Delete {len(matches)} report(s) created in current session '{session_id}'."
        else:
            search_term = (keyword or "").strip()
            if not search_term:
                return None
            matches = await self._repo.find_by_keyword(user.user_id, search_term)
            desc = f"Delete {len(matches)} report(s) mentioning '{search_term}'."

        if not matches:
            return None

        pending = PendingConfirmation(
            user_id=user.user_id,
            session_id=session_id,
            action=ConfirmationAction.DELETE_REPORTS,
            description=desc,
            target_report_ids=[r.report_id for r in matches],
            target_report_titles=[r.title for r in matches],
        )
        await self._confirmations.save_pending(pending)
        self._obs.increment_metric("destructive_confirmations_staged")
        return pending

    async def execute_confirmed_deletion(
        self,
        confirmation: PendingConfirmation,
        user: UserContext,
    ) -> int:
        """Executes deletion strictly scoped to owner_user_id after explicit approval."""
        deleted_count = await self._repo.delete_by_ids(
            owner_user_id=user.user_id,
            report_ids=confirmation.target_report_ids,
        )
        self._obs.increment_metric("reports_deleted_total", deleted_count)
        logger.warning(
            "AUDIT: User %s confirmed deletion of %d report(s) (token=%s)",
            user.user_id,
            deleted_count,
            confirmation.token,
        )
        return deleted_count
