"""Async SQLAlchemy persistence for the prototype's required saved reports."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime

from sqlalchemy import DateTime, String, Text, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.exceptions import ConfirmationValidationError
from app.domain.models import SavedReport


class Base(DeclarativeBase):
    pass


class SavedReportORM(Base):
    __tablename__ = "saved_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    question: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text)
    content_markdown: Mapped[str] = mapped_column(Text)
    action_items_json: Mapped[str] = mapped_column(Text, default="[]")
    sql_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_definitions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DatabaseManager:
    """Manages async SQLAlchemy engine and schema initialization."""

    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, echo=False, future=True)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def init_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            with contextlib.suppress(Exception):
                await conn.exec_driver_sql(
                    "ALTER TABLE saved_reports ADD COLUMN metric_definitions_json TEXT DEFAULT '[]'"
                )
            with contextlib.suppress(Exception):
                await conn.exec_driver_sql(
                    "ALTER TABLE saved_reports ADD COLUMN question TEXT DEFAULT ''"
                )

    async def close(self) -> None:
        await self.engine.dispose()


class PostgresReportRepository:
    """Stores only the saved reports required by the destructive-action flow."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_domain(row: SavedReportORM) -> SavedReport:
        raw_defs = getattr(row, "metric_definitions_json", None) or "[]"
        try:
            metric_defs = json.loads(raw_defs) if isinstance(raw_defs, str) else list(raw_defs)
        except Exception:
            metric_defs = []
        return SavedReport(
            report_id=row.report_id,
            owner_user_id=row.owner_user_id,
            session_id=row.session_id,
            question=getattr(row, "question", "") or "",
            title=row.title,
            summary=row.summary,
            content_markdown=row.content_markdown,
            action_items=json.loads(row.action_items_json or "[]"),
            sql_used=row.sql_used,
            metric_definitions=metric_defs,
            created_at=row.created_at,
        )

    async def create(self, report: SavedReport) -> SavedReport:
        async with self._sf() as session:
            session.add(
                SavedReportORM(
                    report_id=report.report_id,
                    owner_user_id=report.owner_user_id,
                    session_id=report.session_id,
                    question=report.question,
                    title=report.title,
                    summary=report.summary,
                    content_markdown=report.content_markdown,
                    action_items_json=json.dumps(report.action_items),
                    sql_used=report.sql_used,
                    metric_definitions_json=json.dumps(report.metric_definitions),
                    created_at=report.created_at,
                )
            )
            await session.commit()
            return report

    async def list_by_owner(self, owner_user_id: str) -> list[SavedReport]:
        async with self._sf() as session:
            stmt = (
                select(SavedReportORM)
                .where(SavedReportORM.owner_user_id == owner_user_id)
                .order_by(SavedReportORM.created_at.desc())
            )
            result = await session.execute(stmt)
            return [self._to_domain(row) for row in result.scalars().all()]

    async def get_by_id(self, owner_user_id: str, report_id: str) -> SavedReport | None:
        normalized_id = report_id.strip()
        if not normalized_id:
            return None
        async with self._sf() as session:
            exact_stmt = select(SavedReportORM).where(
                SavedReportORM.owner_user_id == owner_user_id,
                SavedReportORM.report_id == normalized_id,
            )
            exact_row = (await session.execute(exact_stmt)).scalar_one_or_none()
            if exact_row is not None:
                return self._to_domain(exact_row)
            if len(normalized_id) >= 4:
                prefix_stmt = (
                    select(SavedReportORM)
                    .where(
                        SavedReportORM.owner_user_id == owner_user_id,
                        SavedReportORM.report_id.startswith(normalized_id),
                    )
                    .limit(2)
                )
                matches = list((await session.execute(prefix_stmt)).scalars().all())
                if len(matches) == 1:
                    return self._to_domain(matches[0])
            return None

    async def find_by_session(self, owner_user_id: str, session_id: str) -> list[SavedReport]:
        async with self._sf() as session:
            stmt = select(SavedReportORM).where(
                SavedReportORM.owner_user_id == owner_user_id,
                SavedReportORM.session_id == session_id,
            )
            result = await session.execute(stmt)
            return [self._to_domain(row) for row in result.scalars().all()]

    async def find_by_keyword(self, owner_user_id: str, keyword: str) -> list[SavedReport]:
        reports = await self.list_by_owner(owner_user_id)
        normalized_keyword = keyword.strip().lower()
        if not normalized_keyword:
            return []
        return [
            report
            for report in reports
            if report.report_id.lower() == normalized_keyword
            or (
                len(normalized_keyword) >= 4
                and report.report_id.lower().startswith(normalized_keyword)
            )
            or normalized_keyword in report.title.lower()
            or normalized_keyword in report.question.lower()
            or normalized_keyword in report.summary.lower()
            or normalized_keyword in report.content_markdown.lower()
        ]

    async def delete_by_ids(
        self,
        owner_user_id: str,
        report_ids: list[str],
        *,
        require_exact_match: bool = False,
    ) -> int:
        unique_ids = sorted(set(report_ids))
        if not unique_ids:
            return 0
        async with self._sf() as session, session.begin():
            if require_exact_match:
                existing_stmt = select(SavedReportORM.report_id).where(
                    SavedReportORM.owner_user_id == owner_user_id,
                    SavedReportORM.report_id.in_(unique_ids),
                )
                existing_ids = sorted((await session.execute(existing_stmt)).scalars().all())
                if existing_ids != unique_ids:
                    raise ConfirmationValidationError(
                        "Target reports changed since confirmation was prepared. "
                        "No reports were deleted; please request deletion again."
                    )
            stmt = delete(SavedReportORM).where(
                SavedReportORM.owner_user_id == owner_user_id,
                SavedReportORM.report_id.in_(unique_ids),
            )
            result = await session.execute(stmt)
            return int(getattr(result, "rowcount", 0) or 0)
