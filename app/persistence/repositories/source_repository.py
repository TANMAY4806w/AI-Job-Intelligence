"""Source asynchronous persistence repository."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.source import Source
from app.persistence.repositories.base import BaseRepository


class SourceRepository(BaseRepository):
    """Repository managing Source catalog entities."""

    async def get_by_id(self, source_id: uuid.UUID) -> Source | None:
        """Retrieve a Source by its primary key UUID."""
        return await self.session.get(Source, source_id)

    async def get_by_name(self, source_name: str) -> Source | None:
        """Retrieve a Source by its unique source name."""
        stmt = select(Source).where(Source.source_name == source_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(self, source: Source) -> Source:
        """Add a new Source to the session and flush to surface constraints.

        Does not commit the transaction.
        """
        self.session.add(source)
        await self.session.flush()
        return source

    async def list_active(self) -> Sequence[Source]:
        """List all active sources ordered by source_name."""
        stmt = select(Source).where(Source.is_active.is_(True)).order_by(Source.source_name)
        result = await self.session.execute(stmt)
        return result.scalars().all()
