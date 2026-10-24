"""Canonical Job asynchronous persistence repository."""

import uuid
from collections.abc import Sequence

from sqlalchemy import or_, select

from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference
from app.persistence.repositories.base import BaseRepository


class JobRepository(BaseRepository):
    """Repository managing canonical Job vacancies and multi-stage deduplication lookups."""

    async def get_by_id(self, job_id: uuid.UUID) -> Job | None:
        """Retrieve a canonical Job by primary key UUID."""
        return await self.session.get(Job, job_id)

    async def get_by_source_reference(
        self,
        source_id: uuid.UUID,
        source_job_id: str,
    ) -> Job | None:
        """Retrieve a canonical Job by external source identity (Stage 1 deduplication).

        Matches existing JobSourceReference by platform and source_job_id.
        """
        stmt = (
            select(Job)
            .join(Job.source_references)
            .where(
                JobSourceReference.source_id == source_id,
                JobSourceReference.source_job_id == source_job_id,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_application_url(self, application_url: str) -> Job | None:
        """Retrieve a canonical Job by application ATS URL (Stage 2 deduplication).

        Checks both the direct Job application_url and linked JobSourceReferences.
        """
        stmt = (
            select(Job)
            .outerjoin(Job.source_references)
            .where(
                or_(
                    Job.application_url == application_url,
                    JobSourceReference.application_url == application_url,
                )
            )
            .order_by(Job.posted_at.desc().nullslast())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_all_by_application_url(
        self,
        application_url: str,
    ) -> Sequence[Job]:
        """Find all distinct canonical Jobs matching an application ATS URL (Stage 2 deduplication).

        Checks both direct Job.application_url and linked JobSourceReference.application_url.
        Does not limit to 1 so that multi-match conflicts can be detected and flagged.
        """
        stmt = (
            select(Job)
            .outerjoin(Job.source_references)
            .where(
                or_(
                    Job.application_url == application_url,
                    JobSourceReference.application_url == application_url,
                )
            )
            .distinct()
            .order_by(Job.posted_at.desc().nullslast())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def find_by_composite_attributes(
        self,
        company_id: uuid.UUID,
        normalized_title: str,
        normalized_location: str | None = None,
    ) -> Sequence[Job]:
        """Find candidate canonical jobs by company, title, and optional location (Stage 3 deduplication)."""
        stmt = select(Job).where(
            Job.company_id == company_id,
            Job.normalized_title == normalized_title,
        )
        if normalized_location is not None:
            stmt = stmt.where(Job.normalized_location == normalized_location)
        stmt = stmt.order_by(Job.posted_at.desc().nullslast())
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, job: Job) -> Job:
        """Add a new canonical Job to the session and flush to surface constraints.

        Does not commit the transaction.
        """
        self.session.add(job)
        await self.session.flush()
        return job

    async def list_by_company_id(
        self,
        company_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Job]:
        """List canonical jobs for a company ordered by posted_at descending."""
        stmt = (
            select(Job)
            .where(Job.company_id == company_id)
            .order_by(Job.posted_at.desc().nullslast())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
