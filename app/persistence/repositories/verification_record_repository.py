"""VerificationRecord asynchronous persistence repository.

Boundary: Encapsulates database operations for multi-dimensional VerificationRecord entities.
Preserves repeated verification attempts across independent runs.
Does not commit transactions; transaction lifecycle is owned by the caller.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.decision import VerificationRecord
from app.persistence.repositories.base import BaseRepository


class VerificationRecordRepository(BaseRepository):
    """Repository managing VerificationRecord verification outcomes."""

    async def create(self, record: VerificationRecord) -> VerificationRecord:
        """Add a VerificationRecord to the session and flush to surface constraints.

        Does not commit the transaction.
        """
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_by_id(self, record_id: uuid.UUID) -> VerificationRecord | None:
        """Retrieve a VerificationRecord by primary key UUID."""
        return await self.session.get(VerificationRecord, record_id)

    async def get_latest_by_job_id(
        self,
        job_id: uuid.UUID,
    ) -> VerificationRecord | None:
        """Retrieve the most recent VerificationRecord for a given canonical job.

        Ordered by verification_run_at descending.
        """
        stmt = (
            select(VerificationRecord)
            .where(VerificationRecord.job_id == job_id)
            .order_by(VerificationRecord.verification_run_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_by_job_id(
        self,
        job_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[VerificationRecord]:
        """List historical verification records for a job, ordered by verification_run_at descending."""
        stmt = (
            select(VerificationRecord)
            .where(VerificationRecord.job_id == job_id)
            .order_by(VerificationRecord.verification_run_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
