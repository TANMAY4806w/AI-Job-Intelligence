"""FilterDecision asynchronous persistence repository.

Boundary: Encapsulates audit log queries and persistence operations for FilterDecision rows.
Preserves immutable decision history across rule engine executions.
Does not commit transactions; transaction lifecycle is owned by the caller.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.persistence.models.decision import FilterDecision
from app.persistence.repositories.base import BaseRepository


class FilterDecisionRepository(BaseRepository):
    """Repository managing immutable FilterDecision audit records."""

    async def create(self, filter_decision: FilterDecision) -> FilterDecision:
        """Add a new FilterDecision record to the session and flush to surface constraints.

        Does not commit the transaction.
        """
        self.session.add(filter_decision)
        await self.session.flush()
        return filter_decision

    async def get_by_id(self, decision_id: uuid.UUID) -> FilterDecision | None:
        """Retrieve a FilterDecision by primary key UUID."""
        return await self.session.get(FilterDecision, decision_id)

    async def get_latest_by_job_id(self, job_id: uuid.UUID) -> FilterDecision | None:
        """Retrieve the most recent FilterDecision for a given canonical job.

        Ordered by evaluated_at descending.
        """
        stmt = (
            select(FilterDecision)
            .where(FilterDecision.job_id == job_id)
            .order_by(FilterDecision.evaluated_at.desc())
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
    ) -> Sequence[FilterDecision]:
        """List historical filter decisions for a job, ordered by evaluated_at descending."""
        stmt = (
            select(FilterDecision)
            .where(FilterDecision.job_id == job_id)
            .order_by(FilterDecision.evaluated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_by_job_id(self, job_id: uuid.UUID) -> int:
        """Count the total number of historical filter decisions recorded for a job."""
        stmt = (
            select(func.count()).select_from(FilterDecision).where(FilterDecision.job_id == job_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()
