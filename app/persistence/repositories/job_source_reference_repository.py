"""JobSourceReference asynchronous persistence repository.

Manages provenance links mapping canonical jobs to specific platform ingestion events and raw jobs.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.raw_job import JobSourceReference
from app.persistence.repositories.base import BaseRepository


class JobSourceReferenceRepository(BaseRepository):
    """Repository managing provenance links between canonical jobs, raw discoveries, and sources."""

    async def get_by_id(self, reference_id: uuid.UUID) -> JobSourceReference | None:
        """Retrieve a JobSourceReference by primary key UUID."""
        return await self.session.get(JobSourceReference, reference_id)

    async def get_by_source_external_id(
        self,
        source_id: uuid.UUID,
        source_job_id: str,
    ) -> JobSourceReference | None:
        """Retrieve a JobSourceReference by platform source ID and external source job ID.

        Enforced unique by the uq_job_source_references_source_ext constraint.
        """
        stmt = select(JobSourceReference).where(
            JobSourceReference.source_id == source_id,
            JobSourceReference.source_job_id == source_job_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_application_url(
        self,
        application_url: str,
    ) -> Sequence[JobSourceReference]:
        """Retrieve JobSourceReferences matching a candidate application ATS URL.

        Utilizes the idx_job_source_ref_app_url index.
        """
        stmt = (
            select(JobSourceReference)
            .where(JobSourceReference.application_url == application_url)
            .order_by(JobSourceReference.collected_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_job_id(self, job_id: uuid.UUID) -> Sequence[JobSourceReference]:
        """List all provenance references linked to a canonical Job."""
        stmt = (
            select(JobSourceReference)
            .where(JobSourceReference.job_id == job_id)
            .order_by(JobSourceReference.is_primary.desc(), JobSourceReference.collected_at)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_raw_job_id(self, raw_job_id: uuid.UUID) -> Sequence[JobSourceReference]:
        """List all provenance references linked to a specific RawJob record."""
        stmt = (
            select(JobSourceReference)
            .where(JobSourceReference.raw_job_id == raw_job_id)
            .order_by(JobSourceReference.collected_at)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, reference: JobSourceReference) -> JobSourceReference:
        """Add a new JobSourceReference provenance link to the session and flush.

        Does not commit the transaction.
        """
        self.session.add(reference)
        await self.session.flush()
        return reference
