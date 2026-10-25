"""CollectionRun asynchronous persistence repository.

Boundary: Encapsulates telemetry queries and persistence operations for CollectionRun rows.
Tracks automated harvesting execution batches, counts, durations, and diagnostic telemetry.
Transaction lifecycle is owned by the caller (no commit inside repository methods).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select

from app.persistence.models.run import CollectionRun
from app.persistence.repositories.base import BaseRepository


class CollectionRunRepository(BaseRepository):
    """Repository managing CollectionRun telemetry records and execution batches."""

    async def create(self, collection_run: CollectionRun) -> CollectionRun:
        """Add a new CollectionRun to the session and flush to generate default values.

        Does not commit the transaction.
        """
        self.session.add(collection_run)
        await self.session.flush()
        return collection_run

    async def get_by_id(self, run_id: uuid.UUID) -> CollectionRun | None:
        """Retrieve a CollectionRun by its primary key UUID."""
        return await self.session.get(CollectionRun, run_id)

    async def update(self, collection_run: CollectionRun) -> CollectionRun:
        """Flush changes to a tracked CollectionRun entity.

        Does not commit the transaction.
        """
        await self.session.flush()
        return collection_run

    async def list_recent(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CollectionRun]:
        """List collection runs ordered by started_at descending."""
        stmt = (
            select(CollectionRun)
            .order_by(CollectionRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_source_id(
        self,
        source_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CollectionRun]:
        """List collection runs for a specific source platform, ordered by started_at descending."""
        stmt = (
            select(CollectionRun)
            .where(CollectionRun.source_id == source_id)
            .order_by(CollectionRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_company_source_id(
        self,
        company_source_id: uuid.UUID,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[CollectionRun]:
        """List collection runs for a specific CompanySource endpoint mapping."""
        stmt = (
            select(CollectionRun)
            .where(CollectionRun.company_source_id == company_source_id)
            .order_by(CollectionRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_all(self) -> int:
        """Count total collection runs in the system."""
        stmt = select(func.count()).select_from(CollectionRun)
        result = await self.session.execute(stmt)
        return result.scalar_one()
