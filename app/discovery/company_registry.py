"""Company Registry and Discovery Management.

Boundary: Manages verified company board registry definitions and provides
idempotent synchronization with database persistence models (Source, Company, CompanySource).
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "companies.json"

AtsProvider = Literal["GREENHOUSE", "LEVER", "ASHBY"]

DEFAULT_SOURCE_METADATA: dict[str, dict[str, Any]] = {
    "GREENHOUSE": {
        "source_name": "Greenhouse",
        "source_type": "ATS",
        "base_url": "https://boards-api.greenhouse.io/v1/boards",
        "rate_limit_rpm": 60,
    },
    "LEVER": {
        "source_name": "Lever",
        "source_type": "ATS",
        "base_url": "https://api.lever.co/v0/postings",
        "rate_limit_rpm": 60,
    },
    "ASHBY": {
        "source_name": "Ashby",
        "source_type": "ATS",
        "base_url": "https://api.ashbyhq.com/posting-api/job-board",
        "rate_limit_rpm": 60,
    },
}


class CompanyRegistryEntry(BaseModel):
    """Structured entry in the verified company registry."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Company display name (e.g. 'Stripe')")
    slug: str = Field(..., description="URL-safe unique company identifier (e.g. 'stripe')")
    ats_provider: AtsProvider = Field(..., description="ATS platform: GREENHOUSE, LEVER, or ASHBY")
    board_identifier: str = Field(
        ..., description="Platform-native job board token or organization slug"
    )
    career_url: str = Field(..., description="Verified official careers or jobs page URL")
    is_active: bool = Field(
        default=True, description="Whether company is active for automatic harvesting"
    )
    notes: str | None = Field(
        default=None, description="Optional operational notes or domain classification"
    )

    @field_validator("ats_provider", mode="before")
    @classmethod
    def normalize_provider(cls, val: Any) -> str:
        """Normalize ATS provider string to uppercase."""
        if isinstance(val, str):
            clean = val.strip().upper()
            if clean in ("GREENHOUSE", "LEVER", "ASHBY"):
                return clean
        raise ValueError(f"Unsupported ATS provider '{val}'. Supported: GREENHOUSE, LEVER, ASHBY.")

    @field_validator("slug", mode="before")
    @classmethod
    def normalize_slug(cls, val: Any) -> str:
        """Normalize company slug."""
        if isinstance(val, str):
            clean = val.strip().lower()
            if clean:
                return clean
        raise ValueError("Company slug must be a non-empty string.")


class CompanyRegistry:
    """Registry manager providing querying, filtering, and database onboarding."""

    def __init__(self, entries: list[CompanyRegistryEntry] | None = None) -> None:
        """Initialize registry with entries."""
        self._entries = entries or []
        self._by_slug: dict[str, CompanyRegistryEntry] = {e.slug: e for e in self._entries}
        self._by_name: dict[str, CompanyRegistryEntry] = {e.name.lower(): e for e in self._entries}

    @property
    def entries(self) -> list[CompanyRegistryEntry]:
        """List of all registered company entries."""
        return list(self._entries)

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "CompanyRegistry":
        """Load company registry from JSON file.

        Args:
            path: Optional file path. Defaults to data/companies.json.

        Returns:
            CompanyRegistry: Instantiated registry with validated entries.

        Raises:
            FileNotFoundError: If registry file does not exist.
            ValueError: If file content is malformed.
        """
        registry_path = Path(path) if path else DEFAULT_REGISTRY_PATH
        if not registry_path.is_file():
            raise FileNotFoundError(f"Company registry file not found: {registry_path}")

        with open(registry_path, encoding="utf-8") as f:
            raw_data = json.load(f)

        if not isinstance(raw_data, list):
            raise ValueError(
                f"Registry file must contain a JSON array, got {type(raw_data).__name__}"
            )

        entries = [CompanyRegistryEntry(**item) for item in raw_data]
        return cls(entries)

    def filter(
        self,
        *,
        provider: str | None = None,
        companies: list[str] | None = None,
        active_only: bool = True,
    ) -> list[CompanyRegistryEntry]:
        """Filter registry entries by provider, company name/slug, and active status.

        Args:
            provider: Optional ATS provider filter ('GREENHOUSE', 'LEVER', 'ASHBY').
            companies: Optional list of company names or slugs to match.
            active_only: If True, returns only active entries.

        Returns:
            list[CompanyRegistryEntry]: Filtered matching entries.
        """
        results = self._entries

        if active_only:
            results = [e for e in results if e.is_active]

        if provider:
            prov_clean = provider.strip().upper()
            results = [e for e in results if e.ats_provider == prov_clean]

        if companies:
            norm_targets = {c.strip().lower() for c in companies if c and c.strip()}
            results = [
                e for e in results if e.slug in norm_targets or e.name.lower() in norm_targets
            ]

        return results

    def get(self, identifier: str) -> CompanyRegistryEntry | None:
        """Find an entry by name or slug (case-insensitive)."""
        key = identifier.strip().lower()
        return self._by_slug.get(key) or self._by_name.get(key)

    async def sync_entry(
        self,
        session: AsyncSession,
        entry: CompanyRegistryEntry,
    ) -> CompanySource:
        """Idempotently onboard or update Source, Company, and CompanySource records.

        Args:
            session: Active SQLAlchemy AsyncSession.
            entry: CompanyRegistryEntry definition.

        Returns:
            CompanySource: Persisted and synchronized mapping instance.
        """
        # 1. Ensure Source exists
        source_meta = DEFAULT_SOURCE_METADATA.get(entry.ats_provider)
        if not source_meta:
            raise ValueError(f"Unsupported ATS provider: {entry.ats_provider}")

        source_stmt = select(Source).where(Source.source_name.ilike(source_meta["source_name"]))
        source_res = await session.execute(source_stmt)
        source = source_res.scalar_one_or_none()
        if source is None:
            source = Source(
                source_name=source_meta["source_name"],
                source_type=source_meta["source_type"],
                base_url=source_meta["base_url"],
                rate_limit_rpm=source_meta["rate_limit_rpm"],
                is_active=True,
            )
            session.add(source)
            await session.flush()

        # 2. Ensure Company exists
        comp_stmt = select(Company).where(
            (Company.normalized_name == entry.slug) | (Company.name.ilike(entry.name))
        )
        comp_res = await session.execute(comp_stmt)
        company = comp_res.scalar_one_or_none()
        if company is None:
            company = Company(
                name=entry.name,
                normalized_name=entry.slug,
                website=entry.career_url,
                is_verified=True,
            )
            session.add(company)
            await session.flush()
        else:
            if not company.website and entry.career_url:
                company.website = entry.career_url

        # 3. Ensure CompanySource mapping exists
        cs_stmt = select(CompanySource).where(
            CompanySource.company_id == company.id,
            CompanySource.source_id == source.id,
        )
        cs_res = await session.execute(cs_stmt)
        company_source = cs_res.scalar_one_or_none()

        if company_source is None:
            company_source = CompanySource(
                company_id=company.id,
                source_id=source.id,
                ats_identifier=entry.board_identifier,
                career_url=entry.career_url,
                discovery_method="MANUAL_REGISTRY",
                confidence=Decimal("1.00"),
                is_active=entry.is_active,
            )
            session.add(company_source)
            await session.flush()
        else:
            # Update attributes if modified
            company_source.ats_identifier = entry.board_identifier
            company_source.career_url = entry.career_url
            company_source.is_active = entry.is_active
            await session.flush()

        # Attach loaded relations for collector consumption
        company_source.company = company
        company_source.source = source
        return company_source

    async def sync_all(
        self,
        session: AsyncSession,
        entries: list[CompanyRegistryEntry] | None = None,
    ) -> list[CompanySource]:
        """Idempotently synchronize multiple registry entries.

        Args:
            session: Active AsyncSession.
            entries: Optional subset of entries; defaults to all registered entries.

        Returns:
            list[CompanySource]: Persisted and synchronized company-source mappings.
        """
        targets = entries if entries is not None else self._entries
        mappings: list[CompanySource] = []
        for entry in targets:
            mapping = await self.sync_entry(session, entry)
            mappings.append(mapping)
        return mappings
