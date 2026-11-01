"""Unit tests for Company Registry and Discovery Management.

Covers:
- CompanyRegistryEntry validation and normalization
- CompanyRegistry file loading and error handling
- Filtering by provider, company name/slug, and active status
- Identifier lookup (slug and name, case-insensitive)
- Idempotent database synchronization (sync_entry, sync_all) with AsyncSession mock
"""

import json
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.discovery.company_registry import (
    DEFAULT_REGISTRY_PATH,
    CompanyRegistry,
    CompanyRegistryEntry,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source


def test_registry_entry_normalization_and_validation():
    """Test valid entry initialization with case-insensitivity and normalization."""
    entry = CompanyRegistryEntry(
        name="  Example Tech  ",
        slug="  EXAMPLE-TECH  ",
        ats_provider="lever",  # Lowercase should be normalized to LEVER
        board_identifier="exampletech",
        career_url="https://jobs.example.com",
        is_active=True,
    )
    assert entry.name == "  Example Tech  "
    assert entry.slug == "example-tech"
    assert entry.ats_provider == "LEVER"
    assert entry.board_identifier == "exampletech"
    assert entry.is_active is True


def test_registry_entry_invalid_provider():
    """Test that unsupported ATS provider raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported ATS provider 'workday'"):
        CompanyRegistryEntry(
            name="Workday Corp",
            slug="workday-corp",
            ats_provider="workday",
            board_identifier="wd",
            career_url="https://example.com",
        )


def test_registry_entry_invalid_slug():
    """Test that empty slug raises ValueError."""
    with pytest.raises(ValueError, match="Company slug must be a non-empty string"):
        CompanyRegistryEntry(
            name="Empty Slug Inc",
            slug="   ",
            ats_provider="GREENHOUSE",
            board_identifier="empty",
            career_url="https://example.com",
        )


def test_load_default_registry_file():
    """Test loading verified default registry from data/companies.json."""
    assert DEFAULT_REGISTRY_PATH.is_file(), f"Default registry not found at {DEFAULT_REGISTRY_PATH}"
    registry = CompanyRegistry.from_file()

    assert len(registry.entries) >= 20
    assert any(e.ats_provider == "GREENHOUSE" for e in registry.entries)
    assert any(e.ats_provider == "LEVER" for e in registry.entries)
    assert any(e.ats_provider == "ASHBY" for e in registry.entries)


def test_registry_from_file_not_found(tmp_path: Path):
    """Test from_file with non-existent file."""
    fake_path = tmp_path / "non_existent.json"
    with pytest.raises(FileNotFoundError, match="Company registry file not found"):
        CompanyRegistry.from_file(fake_path)


def test_registry_from_file_invalid_json(tmp_path: Path):
    """Test from_file with non-list JSON payload."""
    invalid_file = tmp_path / "invalid.json"
    invalid_file.write_text(json.dumps({"companies": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="must contain a JSON array"):
        CompanyRegistry.from_file(invalid_file)


def test_registry_filtering_by_provider():
    """Test filtering entries by ATS provider."""
    registry = CompanyRegistry.from_file()

    gh_entries = registry.filter(provider="greenhouse")
    assert len(gh_entries) >= 5
    assert all(e.ats_provider == "GREENHOUSE" for e in gh_entries)

    lever_entries = registry.filter(provider="LEVER")
    assert len(lever_entries) >= 5
    assert all(e.ats_provider == "LEVER" for e in lever_entries)

    ashby_entries = registry.filter(provider="ashby")
    assert len(ashby_entries) >= 5
    assert all(e.ats_provider == "ASHBY" for e in ashby_entries)


def test_registry_filtering_by_company_and_active():
    """Test filtering entries by company slug/name and active status."""
    entries = [
        CompanyRegistryEntry(
            name="Active GH",
            slug="active-gh",
            ats_provider="GREENHOUSE",
            board_identifier="active-gh",
            career_url="https://active-gh.com",
            is_active=True,
        ),
        CompanyRegistryEntry(
            name="Inactive Lever",
            slug="inactive-lever",
            ats_provider="LEVER",
            board_identifier="inactive-lever",
            career_url="https://inactive-lever.com",
            is_active=False,
        ),
        CompanyRegistryEntry(
            name="Active Ashby",
            slug="active-ashby",
            ats_provider="ASHBY",
            board_identifier="active-ashby",
            career_url="https://active-ashby.com",
            is_active=True,
        ),
    ]
    registry = CompanyRegistry(entries)

    # Filter active only (default)
    active = registry.filter()
    assert len(active) == 2
    assert {e.slug for e in active} == {"active-gh", "active-ashby"}

    # Filter inactive included
    all_entries = registry.filter(active_only=False)
    assert len(all_entries) == 3

    # Filter by company name / slug
    by_comp = registry.filter(companies=["active-gh", "Inactive Lever"], active_only=False)
    assert len(by_comp) == 2
    assert {e.slug for e in by_comp} == {"active-gh", "inactive-lever"}


def test_registry_get_lookup():
    """Test get() lookup by exact slug, uppercase slug, or company name."""
    entry = CompanyRegistryEntry(
        name="Vercel Inc",
        slug="vercel",
        ats_provider="ASHBY",
        board_identifier="vercel",
        career_url="https://vercel.com/careers",
    )
    registry = CompanyRegistry([entry])

    assert registry.get("vercel") == entry
    assert registry.get("VERCEL") == entry
    assert registry.get("Vercel Inc") == entry
    assert registry.get("vercel inc") == entry
    assert registry.get("non-existent") is None


@pytest.mark.asyncio
async def test_sync_entry_creates_new_source_and_company():
    """Test sync_entry when neither Source nor Company exists."""
    session = AsyncMock(spec=AsyncSession)

    # Return None for Source lookup, None for Company lookup, None for CompanySource lookup
    mock_res_empty = MagicMock()
    mock_res_empty.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_res_empty)
    session.add = MagicMock()
    session.flush = AsyncMock()

    entry = CompanyRegistryEntry(
        name="Synthetix",
        slug="synthetix",
        ats_provider="LEVER",
        board_identifier="synthetix",
        career_url="https://jobs.synthetix.io",
    )

    registry = CompanyRegistry([entry])
    mapping = await registry.sync_entry(session, entry)

    assert isinstance(mapping, CompanySource)
    assert mapping.ats_identifier == "synthetix"
    assert mapping.career_url == "https://jobs.synthetix.io"
    assert mapping.discovery_method == "MANUAL_REGISTRY"
    assert mapping.confidence == Decimal("1.00")
    assert mapping.is_active is True

    # Check that session.add was called 3 times (Source, Company, CompanySource)
    assert session.add.call_count == 3
    added_entities = [call.args[0] for call in session.add.call_args_list]
    assert any(isinstance(e, Source) for e in added_entities)
    assert any(isinstance(e, Company) for e in added_entities)
    assert any(isinstance(e, CompanySource) for e in added_entities)
    assert session.flush.call_count == 3


@pytest.mark.asyncio
async def test_sync_entry_existing_source_and_company():
    """Test sync_entry when Source and Company already exist."""
    session = AsyncMock(spec=AsyncSession)

    existing_source = Source(
        id=uuid.uuid4(),
        source_name="Greenhouse",
        source_type="ATS",
        base_url="https://boards-api.greenhouse.io/v1/boards",
        rate_limit_rpm=60,
        is_active=True,
    )
    existing_company = Company(
        id=uuid.uuid4(),
        name="Existing Corp",
        normalized_name="existing-corp",
        website="https://existing.com",
        is_verified=True,
    )
    existing_mapping = CompanySource(
        id=uuid.uuid4(),
        company_id=existing_company.id,
        source_id=existing_source.id,
        ats_identifier="old-board",
        career_url="https://existing.com/old",
        discovery_method="MANUAL_REGISTRY",
        confidence=Decimal("1.00"),
        is_active=False,
    )

    mock_res_src = MagicMock()
    mock_res_src.scalar_one_or_none.return_value = existing_source

    mock_res_comp = MagicMock()
    mock_res_comp.scalar_one_or_none.return_value = existing_company

    mock_res_map = MagicMock()
    mock_res_map.scalar_one_or_none.return_value = existing_mapping

    session.execute = AsyncMock(side_effect=[mock_res_src, mock_res_comp, mock_res_map])
    session.add = MagicMock()
    session.flush = AsyncMock()

    entry = CompanyRegistryEntry(
        name="Existing Corp",
        slug="existing-corp",
        ats_provider="GREENHOUSE",
        board_identifier="new-board",
        career_url="https://existing.com/new",
        is_active=True,
    )

    registry = CompanyRegistry([entry])
    mapping = await registry.sync_entry(session, entry)

    # Should update existing mapping
    assert mapping.id == existing_mapping.id
    assert mapping.ats_identifier == "new-board"
    assert mapping.career_url == "https://existing.com/new"
    assert mapping.is_active is True
    # session.add should NOT be called for any new objects
    assert session.add.call_count == 0
    # session.flush should be called once to flush the mapping update
    assert session.flush.call_count == 1


@pytest.mark.asyncio
async def test_sync_all():
    """Test sync_all synchronizes multiple entries."""
    session = AsyncMock(spec=AsyncSession)

    mock_res_empty = MagicMock()
    mock_res_empty.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_res_empty)
    session.add = MagicMock()
    session.flush = AsyncMock()

    entries = [
        CompanyRegistryEntry(
            name="Co1",
            slug="co1",
            ats_provider="LEVER",
            board_identifier="co1",
            career_url="https://co1.com",
        ),
        CompanyRegistryEntry(
            name="Co2",
            slug="co2",
            ats_provider="ASHBY",
            board_identifier="co2",
            career_url="https://co2.com",
        ),
    ]
    registry = CompanyRegistry(entries)
    mappings = await registry.sync_all(session)

    assert len(mappings) == 2
    assert mappings[0].ats_identifier == "co1"
    assert mappings[1].ats_identifier == "co2"
