"""Company asynchronous persistence repository."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision
from app.persistence.models.job import Job
from app.persistence.models.source import CompanySource
from app.persistence.repositories.base import BaseRepository


class CompanyRepository(BaseRepository):
    """Repository managing canonical Company entities."""

    async def get_by_id(self, company_id: uuid.UUID) -> Company | None:
        """Retrieve a Company by its primary key UUID."""
        return await self.session.get(Company, company_id)

    async def get_by_normalized_name(self, normalized_name: str) -> Company | None:
        """Retrieve a Company by its unique normalized slug/name."""
        stmt = select(Company).where(Company.normalized_name == normalized_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, company: Company) -> Company:
        """Add a new Company to the session and flush to surface constraints.

        Does not commit the transaction.
        """
        self.session.add(company)
        await self.session.flush()
        return company

    async def list_verified(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Company]:
        """List verified companies ordered by name with pagination."""
        stmt = (
            select(Company)
            .where(Company.is_verified.is_(True))
            .order_by(Company.name)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_all_with_stats(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[tuple[Company, int]]:
        """List companies with active job counts and eagerly loaded ATS sources."""
        latest_human_decision = (
            select(FilterDecision.final_status)
            .where(
                FilterDecision.job_id == Job.id,
                FilterDecision.rule_version == "HUMAN_REVIEW_V1",
            )
            .order_by(FilterDecision.evaluated_at.desc(), FilterDecision.id.desc())
            .limit(1)
            .scalar_subquery()
        )

        active_subq = (
            select(Job.company_id, func.count(Job.id).label("active_jobs_count"))
            .where(
                or_(
                    latest_human_decision.is_(None),
                    latest_human_decision != "DISMISSED",
                )
            )
            .group_by(Job.company_id)
            .subquery()
        )

        stmt = (
            select(
                Company,
                func.coalesce(active_subq.c.active_jobs_count, 0).label("active_jobs_count"),
            )
            .outerjoin(active_subq, Company.id == active_subq.c.company_id)
            .options(selectinload(Company.company_sources).selectinload(CompanySource.source))
            .order_by(Company.name)
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.all()
