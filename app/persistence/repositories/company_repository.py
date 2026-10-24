"""Company asynchronous persistence repository."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.company import Company
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
