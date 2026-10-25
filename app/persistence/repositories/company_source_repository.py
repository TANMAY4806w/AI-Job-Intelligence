"""CompanySource asynchronous persistence repository."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.persistence.models.source import CompanySource
from app.persistence.repositories.base import BaseRepository


class CompanySourceRepository(BaseRepository):
    """Repository managing company-to-source endpoint mappings and ATS locators."""

    async def get_by_id(self, company_source_id: uuid.UUID) -> CompanySource | None:
        """Retrieve a CompanySource mapping by primary key UUID."""
        return await self.session.get(CompanySource, company_source_id)

    async def get_by_locator(
        self,
        company_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        ats_identifier: str | None = None,
        career_url: str | None = None,
        api_endpoint: str | None = None,
    ) -> CompanySource | None:
        """Retrieve a CompanySource mapping matching the partial unique index conditions.

        Evaluation precedence:
        1. uq_company_sources_ats: (company_id, source_id, ats_identifier) WHERE ats_identifier IS NOT NULL
        2. uq_company_sources_career_url: (company_id, source_id, career_url) WHERE ats_identifier IS NULL AND career_url IS NOT NULL
        3. uq_company_sources_api_endpoint: (company_id, source_id, api_endpoint) WHERE ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NOT NULL

        Raises:
            ValueError: If all locator inputs are absent or whitespace-only, as bare mappings
                are prohibited by check constraint chk_company_sources_has_locator.
        """
        # Normalize whitespace strings to None for consistent SQL NULL matching
        clean_ats = ats_identifier.strip() if ats_identifier and ats_identifier.strip() else None
        clean_url = career_url.strip() if career_url and career_url.strip() else None
        clean_api = api_endpoint.strip() if api_endpoint and api_endpoint.strip() else None

        if clean_ats is None and clean_url is None and clean_api is None:
            raise ValueError(
                "At least one locator (ats_identifier, career_url, or api_endpoint) must be provided. "
                "Bare mappings without locators are prohibited by check constraint chk_company_sources_has_locator."
            )

        stmt = select(CompanySource).where(
            CompanySource.company_id == company_id,
            CompanySource.source_id == source_id,
        )

        if clean_ats is not None:
            stmt = stmt.where(CompanySource.ats_identifier == clean_ats)
        elif clean_url is not None:
            stmt = stmt.where(
                CompanySource.ats_identifier.is_(None),
                CompanySource.career_url == clean_url,
            )
        else:
            stmt = stmt.where(
                CompanySource.ats_identifier.is_(None),
                CompanySource.career_url.is_(None),
                CompanySource.api_endpoint == clean_api,
            )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_ats_identifier(
        self,
        source_id: uuid.UUID,
        ats_identifier: str,
    ) -> CompanySource | None:
        """Retrieve a CompanySource mapping by ATS token and source ID.

        Utilizes the idx_company_sources_ats operational index.
        Because the database unique constraint is scoped to (company_id, source_id, ats_identifier),
        source_id + ats_identifier is not guaranteed to be globally unique across all companies.

        If multiple company mappings share the same ATS token on a source, this method detects
        the ambiguity and raises ValueError rather than silently selecting an arbitrary row.

        Raises:
            ValueError: If multiple companies are mapped to the same (source_id, ats_identifier).
        """
        clean_token = ats_identifier.strip() if ats_identifier and ats_identifier.strip() else None
        if clean_token is None:
            return None

        stmt = (
            select(CompanySource)
            .where(
                CompanySource.source_id == source_id,
                CompanySource.ats_identifier == clean_token,
            )
            .limit(2)
        )
        result = await self.session.execute(stmt)
        matches = result.scalars().all()

        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous ATS identifier: multiple company sources found for "
                f"source_id={source_id} and ats_identifier='{clean_token}'."
            )

        return matches[0] if matches else None

    async def list_by_ats_identifier(
        self,
        source_id: uuid.UUID,
        ats_identifier: str,
    ) -> Sequence[CompanySource]:
        """Retrieve all CompanySource mappings matching an ATS token and source ID."""
        clean_token = ats_identifier.strip() if ats_identifier and ats_identifier.strip() else None
        if clean_token is None:
            return []

        stmt = (
            select(CompanySource)
            .where(
                CompanySource.source_id == source_id,
                CompanySource.ats_identifier == clean_token,
            )
            .order_by(CompanySource.created_at)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_company_id(self, company_id: uuid.UUID) -> Sequence[CompanySource]:
        """List all source mappings for a given company."""
        stmt = (
            select(CompanySource)
            .where(CompanySource.company_id == company_id)
            .order_by(CompanySource.created_at)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_by_source_id(
        self,
        source_id: uuid.UUID,
        *,
        active_only: bool = True,
    ) -> Sequence[CompanySource]:
        """List all company mappings for a given source."""
        stmt = select(CompanySource).where(CompanySource.source_id == source_id)
        if active_only:
            stmt = stmt.where(CompanySource.is_active.is_(True))
        stmt = stmt.order_by(CompanySource.created_at)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, company_source: CompanySource) -> CompanySource:
        """Add a new CompanySource mapping to the session and flush.

        Does not commit the transaction.
        """
        self.session.add(company_source)
        await self.session.flush()
        return company_source

    async def update(self, company_source: CompanySource) -> CompanySource:
        """Flush changes to a tracked CompanySource entity.

        Does not commit the transaction.
        """
        await self.session.flush()
        return company_source
