"""Async SQLAlchemy repositories for Saved Reports, Golden Bucket Trios, Preferences, and CEO Persona."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain.models import (
    AnalysisDepthPreference,
    GoldenTrio,
    OutputFormatPreference,
    PersonaConfig,
    SavedReport,
    UserPreferenceProfile,
)

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class SavedReportORM(Base):
    __tablename__ = "saved_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), index=True)
    session_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text)
    content_markdown: Mapped[str] = mapped_column(Text)
    action_items_json: Mapped[str] = mapped_column(Text, default="[]")
    sql_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class GoldenTrioORM(Base):
    __tablename__ = "golden_trios"

    trio_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    sql_query: Mapped[str] = mapped_column(Text)
    analyst_report: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserPreferenceORM(Base):
    __tablename__ = "user_preferences"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    preferred_format: Mapped[str] = mapped_column(String(32), default="auto")
    preferred_depth: Mapped[str] = mapped_column(String(32), default="standard")
    prefers_charts_or_visuals: Mapped[bool] = mapped_column(default=False)
    custom_notes_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PersonaConfigORM(Base):
    __tablename__ = "persona_configs"

    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tone_instructions: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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

    async def close(self) -> None:
        await self.engine.dispose()


class PostgresReportRepository:
    """Implements ReportRepositoryPort."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _to_domain(row: SavedReportORM) -> SavedReport:
        return SavedReport(
            report_id=row.report_id,
            owner_user_id=row.owner_user_id,
            session_id=row.session_id,
            title=row.title,
            summary=row.summary,
            content_markdown=row.content_markdown,
            action_items=json.loads(row.action_items_json or "[]"),
            sql_used=row.sql_used,
            created_at=row.created_at,
        )

    async def create(self, report: SavedReport) -> SavedReport:
        async with self._sf() as session:
            orm_obj = SavedReportORM(
                report_id=report.report_id,
                owner_user_id=report.owner_user_id,
                session_id=report.session_id,
                title=report.title,
                summary=report.summary,
                content_markdown=report.content_markdown,
                action_items_json=json.dumps(report.action_items),
                sql_used=report.sql_used,
                created_at=report.created_at,
            )
            session.add(orm_obj)
            await session.commit()
            return report

    async def list_by_owner(self, owner_user_id: str) -> list[SavedReport]:
        async with self._sf() as session:
            stmt = (
                select(SavedReportORM)
                .where(SavedReportORM.owner_user_id == owner_user_id)
                .order_by(SavedReportORM.created_at.desc())
            )
            res = await session.execute(stmt)
            return [self._to_domain(r) for r in res.scalars().all()]

    async def find_by_session(self, owner_user_id: str, session_id: str) -> list[SavedReport]:
        async with self._sf() as session:
            stmt = select(SavedReportORM).where(
                SavedReportORM.owner_user_id == owner_user_id,
                SavedReportORM.session_id == session_id,
            )
            res = await session.execute(stmt)
            return [self._to_domain(r) for r in res.scalars().all()]

    async def find_by_keyword(self, owner_user_id: str, keyword: str) -> list[SavedReport]:
        all_reports = await self.list_by_owner(owner_user_id)
        kw = keyword.lower()
        return [
            r
            for r in all_reports
            if kw in r.title.lower() or kw in r.summary.lower() or kw in r.content_markdown.lower()
        ]

    async def delete_by_ids(self, owner_user_id: str, report_ids: list[str]) -> int:
        if not report_ids:
            return 0
        async with self._sf() as session:
            stmt = delete(SavedReportORM).where(
                SavedReportORM.owner_user_id == owner_user_id,
                SavedReportORM.report_id.in_(report_ids),
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)


class PostgresGoldenKnowledgeRepository:
    """Implements GoldenKnowledgePort with semantic/lexical scoring over historical Analyst Trios."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        dataset_id: str = "bigquery-public-data.thelook_ecommerce",
    ) -> None:
        self._sf = session_factory
        self._dataset_id = dataset_id

    async def seed_defaults_if_empty(self) -> None:
        async with self._sf() as session:
            existing = await session.execute(select(GoldenTrioORM).limit(1))
            if existing.scalar_one_or_none() is not None:
                return

            defaults = [
                GoldenTrio(
                    question="Why are users in one state underspending compared to another state?",
                    sql_query=(
                        "SELECT u.state, COUNT(DISTINCT u.id) AS customers, "
                        "COUNT(DISTINCT oi.order_id) AS completed_orders, "
                        "ROUND(SUM(oi.sale_price), 2) AS total_revenue, "
                        "ROUND(SUM(oi.sale_price) / COUNT(DISTINCT u.id), 2) AS arpu_usd "
                        f"FROM `{self._dataset_id}.users` u "
                        f"JOIN `{self._dataset_id}.order_items` oi ON u.id = oi.user_id "
                        "WHERE oi.status = 'Complete' GROUP BY u.state ORDER BY arpu_usd DESC LIMIT 25"
                    ),
                    analyst_report=(
                        "Analysts define underspending by comparing ARPU (total completed sale_price / distinct active users) "
                        "and order frequency per user across states, isolating status='Complete' to avoid distortion from returns."
                    ),
                    tags=["state", "underspending", "arpu", "regional"],
                ),
                GoldenTrio(
                    question="Why did our churn rate spike last month?",
                    sql_query=(
                        "SELECT FORMAT_TIMESTAMP('%Y-%m', created_at) AS month, "
                        "COUNT(*) AS total_orders, "
                        "COUNTIF(status = 'Cancelled') AS cancelled, "
                        "COUNTIF(status = 'Returned') AS returned, "
                        "ROUND(SAFE_DIVIDE(COUNTIF(status IN ('Cancelled', 'Returned')), COUNT(*)) * 100, 2) AS churn_rate_pct "
                        f"FROM `{self._dataset_id}.orders` "
                        "GROUP BY month ORDER BY month DESC LIMIT 12"
                    ),
                    analyst_report=(
                        "In retail transaction logs without recurring subscriptions, analysts measure monthly order churn "
                        "as (Cancelled + Returned orders) / Total orders, segmented by month and product category."
                    ),
                    tags=["churn", "cancelled", "returned", "monthly"],
                ),
                GoldenTrio(
                    question="Create a quarterly performance report by product category with margin and action items.",
                    sql_query=(
                        "SELECT p.category, p.department, COUNT(DISTINCT oi.order_id) AS orders, "
                        "ROUND(SUM(oi.sale_price), 2) AS revenue_usd, "
                        "ROUND(SUM(oi.sale_price - p.cost), 2) AS gross_profit_usd, "
                        "ROUND(SAFE_DIVIDE(SUM(oi.sale_price - p.cost), SUM(oi.sale_price)) * 100, 2) AS margin_pct "
                        f"FROM `{self._dataset_id}.order_items` oi "
                        f"JOIN `{self._dataset_id}.products` p ON oi.product_id = p.id "
                        "WHERE oi.status = 'Complete' GROUP BY p.category, p.department ORDER BY gross_profit_usd DESC LIMIT 20"
                    ),
                    analyst_report=(
                        "Quarterly category reviews evaluate completed revenue, gross profit (sale_price - cost), "
                        "and gross margin % to recommend inventory reallocation and promotional adjustments."
                    ),
                    tags=["quarter", "q1", "q2", "report", "category", "margin"],
                ),
            ]
            for trio in defaults:
                session.add(
                    GoldenTrioORM(
                        trio_id=trio.trio_id,
                        question=trio.question,
                        sql_query=trio.sql_query,
                        analyst_report=trio.analyst_report,
                        tags_json=json.dumps(trio.tags),
                        created_at=trio.created_at,
                    )
                )
            await session.commit()

    async def add_trio(self, trio: GoldenTrio) -> GoldenTrio:
        async with self._sf() as session:
            session.add(
                GoldenTrioORM(
                    trio_id=trio.trio_id,
                    question=trio.question,
                    sql_query=trio.sql_query,
                    analyst_report=trio.analyst_report,
                    tags_json=json.dumps(trio.tags),
                    created_at=trio.created_at,
                )
            )
            await session.commit()
            return trio

    async def search_similar_trios(self, query: str, top_k: int = 3) -> list[GoldenTrio]:
        async with self._sf() as session:
            res = await session.execute(select(GoldenTrioORM))
            all_rows = res.scalars().all()

        q_tokens = set(query.lower().split())
        scored: list[tuple[int, GoldenTrio]] = []
        for r in all_rows:
            tags = json.loads(r.tags_json or "[]")
            doc_tokens = set((r.question + " " + " ".join(tags)).lower().split())
            overlap = len(q_tokens.intersection(doc_tokens))
            scored.append(
                (
                    overlap,
                    GoldenTrio(
                        trio_id=r.trio_id,
                        question=r.question,
                        sql_query=r.sql_query,
                        analyst_report=r.analyst_report,
                        tags=tags,
                        created_at=r.created_at,
                    ),
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [trio for _, trio in scored[:top_k]]


class PostgresUserPreferenceRepository:
    """Implements UserPreferencePort."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get_preferences(self, user_id: str) -> UserPreferenceProfile:
        async with self._sf() as session:
            row = await session.get(UserPreferenceORM, user_id)
            if row is None:
                return UserPreferenceProfile(user_id=user_id)
            return UserPreferenceProfile(
                user_id=row.user_id,
                preferred_format=OutputFormatPreference(row.preferred_format),
                preferred_depth=AnalysisDepthPreference(row.preferred_depth),
                prefers_charts_or_visuals=row.prefers_charts_or_visuals,
                custom_notes=json.loads(row.custom_notes_json or "[]"),
                updated_at=row.updated_at,
            )

    async def upsert_preferences(self, profile: UserPreferenceProfile) -> UserPreferenceProfile:
        profile.updated_at = datetime.now(UTC)
        async with self._sf() as session:
            row = await session.get(UserPreferenceORM, profile.user_id)
            if row is None:
                row = UserPreferenceORM(
                    user_id=profile.user_id,
                    preferred_format=profile.preferred_format.value,
                    preferred_depth=profile.preferred_depth.value,
                    prefers_charts_or_visuals=profile.prefers_charts_or_visuals,
                    custom_notes_json=json.dumps(profile.custom_notes),
                    updated_at=profile.updated_at,
                )
                session.add(row)
            else:
                row.preferred_format = profile.preferred_format.value
                row.preferred_depth = profile.preferred_depth.value
                row.prefers_charts_or_visuals = profile.prefers_charts_or_visuals
                row.custom_notes_json = json.dumps(profile.custom_notes)
                row.updated_at = profile.updated_at
            await session.commit()
            return profile


class PostgresPersonaRepository:
    """Implements PersonaStorePort for dynamic CEO tone updates without redeployment."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        default_tone: str,
    ) -> None:
        self._sf = session_factory
        self._default_tone = default_tone

    async def get_active_persona(self) -> PersonaConfig:
        async with self._sf() as session:
            row = await session.get(PersonaConfigORM, "global_executive_persona")
            if row is None:
                return PersonaConfig(tone_instructions=self._default_tone)
            return PersonaConfig(
                persona_id=row.persona_id,
                tone_instructions=row.tone_instructions,
                updated_by=row.updated_by,
                updated_at=row.updated_at,
            )

    async def save_persona(self, persona: PersonaConfig) -> PersonaConfig:
        persona.updated_at = datetime.now(UTC)
        async with self._sf() as session:
            row = await session.get(PersonaConfigORM, persona.persona_id)
            if row is None:
                row = PersonaConfigORM(
                    persona_id=persona.persona_id,
                    tone_instructions=persona.tone_instructions,
                    updated_by=persona.updated_by,
                    updated_at=persona.updated_at,
                )
                session.add(row)
            else:
                row.tone_instructions = persona.tone_instructions
                row.updated_by = persona.updated_by
                row.updated_at = persona.updated_at
            await session.commit()
            return persona
