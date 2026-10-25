"""Greenhouse operator workflow commands and business logic.

Boundary: Orchestrates Greenhouse company onboarding, locator validation,
manual collection execution, and human-readable outcome formatting.
Caller retains strict ownership of database commit and rollback.
"""

import json
import logging
import re
import sys
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import validate_board_token
from app.orchestration.collection_runner import (
    CollectionRunner,
    MappingCollectionOutcome,
    _get_loaded_relationship,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.source_repository import SourceRepository

logger = logging.getLogger(__name__)

# Common legal corporate suffixes to strip when normalizing company names
_LEGAL_SUFFIXES_PATTERN = re.compile(
    r"(?i)\b(?:inc|incorporated|corp|corporation|llc|ltd|limited|pvt\s+ltd|pvt|private\s+limited|co|company)\b\.?$"
)


def _compare_company_names(name1: str, name2: str) -> bool:
    """Compare two company names ignoring punctuation and extra whitespace.

    Returns True if both names have identical wording modulo punctuation.
    For example:
        'Stripe, Inc.' and 'Stripe Inc' -> True
        'Acme' and 'acme' -> True
        'Acme Corp' and 'Acme LLC' -> False (different legal suffix words)
        'Acme' and 'Acme Corp' -> False (one has suffix word, one does not)
    """
    words1 = re.sub(r"[^\w\s]", "", name1.lower()).split()
    words2 = re.sub(r"[^\w\s]", "", name2.lower()).split()
    return words1 == words2


def normalize_company_name(name: str | None) -> str:
    """Normalize a company name into a deterministic lowercase slug.

    Strips legal entity suffixes, replaces non-alphanumeric characters
    with hyphens, collapses consecutive hyphens, and truncates to 255 chars.

    Args:
        name: Raw company display name.

    Returns:
        str: Standardized normalized slug.

    Raises:
        ValueError: If name is blank or contains no valid alphanumeric characters.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Company name must be a non-empty string.")

    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Company name cannot be blank or whitespace-only.")

    # Repeatedly strip legal suffixes from the end
    previous = ""
    while previous != cleaned:
        previous = cleaned
        cleaned = _LEGAL_SUFFIXES_PATTERN.sub("", cleaned).strip()

    # Lowercase and convert non-alphanumerics to hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")

    if not slug:
        raise ValueError(
            f"Company name '{name}' does not contain any valid alphanumeric characters for slugification."
        )

    return slug[:255]


async def get_or_create_greenhouse_source(
    source_repository: SourceRepository,
) -> tuple[Source, bool]:
    """Retrieve the standard Greenhouse Source catalog entity, creating it if absent.

    Flushes to surface constraints; caller retains transaction commit ownership.

    Args:
        source_repository: Repository managing Source entities.

    Returns:
        tuple[Source, bool]: (Source entity, created_flag)
    """
    source = await source_repository.get_by_name("GREENHOUSE")
    if source is not None:
        return source, False

    source = Source(
        id=uuid.uuid4(),
        source_name="GREENHOUSE",
        source_type="ATS_API",
        base_url="https://boards-api.greenhouse.io/v1/boards",
        rate_limit_rpm=60,
        requires_browser=False,
        is_active=True,
    )
    created_source = await source_repository.create(source)
    return created_source, True


async def get_or_create_company(
    company_repository: CompanyRepository,
    company_name: str,
    *,
    company_slug: str | None = None,
) -> tuple[Company, bool]:
    """Retrieve an existing Company by normalized name or create a new canonical entity.

    Detects ambiguous name collisions where distinct legal entities produce the same slug
    (e.g., 'Acme Corp' vs 'Acme LLC'). Reuses the existing company only when the names match.
    Flushes to surface constraints; caller retains transaction commit ownership.

    Args:
        company_repository: Repository managing Company entities.
        company_name: Human-readable display name.
        company_slug: Optional explicit normalized slug override.

    Returns:
        tuple[Company, bool]: (Company entity, created_flag)

    Raises:
        ValueError: If normalized slug collides with an existing company of a different name
            and no distinct explicit slug is provided.
    """
    clean_name = company_name.strip()
    explicit_slug = bool(company_slug and company_slug.strip())
    slug = company_slug.strip().lower() if explicit_slug else normalize_company_name(clean_name)

    company = await company_repository.get_by_normalized_name(slug)
    if company is not None:
        if not _compare_company_names(company.name, clean_name):
            raise ValueError(
                f"Ambiguous company identity: Normalized slug '{slug}' already belongs to "
                f"existing company '{company.name}' (ID: {company.id}), but requested name is '{clean_name}'. "
                f"To prevent associating the Greenhouse board with the wrong company, please provide an explicit "
                f"distinct slug via --company-slug (e.g. '--company-slug <distinct-slug>'), "
                f"or use the exact name '{company.name}' if they represent the same corporate entity."
            )
        return company, False

    new_company = Company(
        id=uuid.uuid4(),
        name=clean_name,
        normalized_name=slug,
        is_verified=False,
        sector="UNKNOWN",
        business_model="UNKNOWN",
    )
    created_company = await company_repository.create(new_company)
    return created_company, True


async def onboard_greenhouse_mapping(
    session: AsyncSession,
    company_name: str,
    board_token: str,
    *,
    company_slug: str | None = None,
) -> tuple[Company, Source, CompanySource, bool]:
    """Onboard or resolve a Greenhouse company-source mapping idempotently.

    Reuses existing Source, Company, and CompanySource records if identities match.
    Enforces uniqueness, validates board token format, and prevents silent overwrites.
    Flushes to session; caller retains commit/rollback ownership.

    Args:
        session: Active database session.
        company_name: Hiring organization name.
        board_token: Public Greenhouse job board identifier.
        company_slug: Optional explicit normalized slug.

    Returns:
        tuple[Company, Source, CompanySource, bool]: (Company, Source, CompanySource, created_flag)

    Raises:
        ValueError: If board token is invalid, already assigned to another company,
            or if the company already has a different Greenhouse board token configured.
    """
    clean_token = validate_board_token(board_token)

    source_repo = SourceRepository(session)
    company_repo = CompanyRepository(session)
    cs_repo = CompanySourceRepository(session)

    source, _ = await get_or_create_greenhouse_source(source_repo)
    company, _ = await get_or_create_company(
        company_repo,
        company_name,
        company_slug=company_slug,
    )

    # 1. Verify this board token is not already mapped to a different company on Greenhouse
    existing_ats_mapping = await cs_repo.get_by_ats_identifier(source.id, clean_token)
    if existing_ats_mapping is not None:
        if existing_ats_mapping.company_id != company.id:
            raise ValueError(
                f"Greenhouse board token '{clean_token}' is already mapped to a different company "
                f"(Company ID: {existing_ats_mapping.company_id}). Cannot map to '{company.name}'."
            )
        return company, source, existing_ats_mapping, False

    # 2. Verify the company does not already have a different Greenhouse board token
    existing_company_mappings = await cs_repo.list_by_company_id(company.id)
    gh_mappings = [m for m in existing_company_mappings if m.source_id == source.id]
    if gh_mappings:
        for m in gh_mappings:
            if m.ats_identifier == clean_token:
                return company, source, m, False
        # Mapping exists but with a different board token
        configured_token = gh_mappings[0].ats_identifier
        raise ValueError(
            f"Company '{company.name}' already has Greenhouse board token '{configured_token}' configured. "
            f"Silently overwriting with '{clean_token}' is prohibited."
        )

    # 3. Create new CompanySource mapping
    new_company_source = CompanySource(
        id=uuid.uuid4(),
        company_id=company.id,
        source_id=source.id,
        ats_identifier=clean_token,
        discovery_method="MANUAL_OPERATOR",
        is_active=True,
        confidence=Decimal("1.00"),
    )
    created_cs = await cs_repo.create(new_company_source)
    return company, source, created_cs, True


def format_collection_summary(
    outcome: MappingCollectionOutcome,
    *,
    company_name: str | None = None,
    board_token: str | None = None,
) -> str:
    """Format a clear, operator-readable text summary of a collection run.

    Args:
        outcome: MappingCollectionOutcome from CollectionRunner.
        company_name: Optional override company name.
        board_token: Optional override board token.

    Returns:
        str: Multi-line formatted summary box.
    """
    disp_company = company_name or outcome.company_name or "Unknown Company"
    disp_token = board_token or outcome.ats_identifier or "Unknown Token"
    disp_run_id = str(outcome.collection_run_id) if outcome.collection_run_id else "None"
    disp_cs_id = str(outcome.company_source_id)

    duration = (outcome.finished_at - outcome.started_at).total_seconds()
    duration_str = f"{duration:.2f}s" if duration >= 0 else "N/A"

    lines = [
        "=" * 64,
        "              GREENHOUSE COLLECTION SUMMARY",
        "=" * 64,
        f"Company:             {disp_company}",
        "Platform:            GREENHOUSE",
        f"Board Token:         {disp_token}",
        f"Mapping ID:          {disp_cs_id}",
        f"Collection Run ID:   {disp_run_id}",
        f"Final Status:        {outcome.status}",
        "-" * 64,
        f"Records Discovered:  {outcome.records_discovered}",
        f"Records Ingested:    {outcome.records_ingested}",
        f"Duplicates Detected: {outcome.duplicates_detected}",
        f"Records Failed:      {outcome.records_failed}",
        "-" * 64,
        f"HTTP 2xx (Success):  {outcome.http_2xx_count}",
        f"HTTP 4xx (Client):   {outcome.http_4xx_count}",
        f"HTTP 5xx (Server):   {outcome.http_5xx_count}",
        f"Duration:            {duration_str}",
        f"Started At:          {outcome.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Finished At:         {outcome.finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
    ]

    if outcome.error_message:
        lines.append("-" * 64)
        lines.append(f"Diagnostic Error:    {outcome.error_message}")

    lines.append("=" * 64)
    return "\n".join(lines)


def format_collection_json(
    outcome: MappingCollectionOutcome,
    *,
    company_name: str | None = None,
    board_token: str | None = None,
) -> str:
    """Format collection outcome as JSON for machine-readable consumption."""
    duration = (outcome.finished_at - outcome.started_at).total_seconds()
    payload: dict[str, Any] = {
        "company_name": company_name or outcome.company_name,
        "platform": "GREENHOUSE",
        "board_token": board_token or outcome.ats_identifier,
        "mapping_id": str(outcome.company_source_id),
        "collection_run_id": str(outcome.collection_run_id) if outcome.collection_run_id else None,
        "status": outcome.status,
        "records_discovered": outcome.records_discovered,
        "records_ingested": outcome.records_ingested,
        "duplicates_detected": outcome.duplicates_detected,
        "records_failed": outcome.records_failed,
        "http_2xx_count": outcome.http_2xx_count,
        "http_4xx_count": outcome.http_4xx_count,
        "http_5xx_count": outcome.http_5xx_count,
        "duration_seconds": round(duration, 3),
        "started_at": outcome.started_at.isoformat(),
        "finished_at": outcome.finished_at.isoformat(),
        "error_message": outcome.error_message,
    }
    return json.dumps(payload, indent=2)


async def execute_greenhouse_collection(
    session: AsyncSession,
    company_source: CompanySource,
    *,
    company_name: str | None = None,
    output_json: bool = False,
    http_client: PoliteHttpClient | None = None,
) -> tuple[MappingCollectionOutcome, int]:
    """Execute collection pipeline on a configured Greenhouse mapping.

    Controls transaction boundary: commits session on completion (persisting
    either success or diagnostic failure CollectionRun records), or rolls back
    if a database-level error invalidates the transaction.

    Args:
        session: Active database session.
        company_source: CompanySource entity to harvest.
        company_name: Optional company display name for summary formatting.
        output_json: True to print JSON instead of formatted text.
        http_client: Optional polite HTTP client passed to collection runner.

    Returns:
        tuple[MappingCollectionOutcome, int]: (Outcome details, process exit code)
    """
    runner = CollectionRunner.from_session(session, http_client=http_client)

    try:
        outcome = await runner.run_mapping(company_source, run_type="MANUAL_TRIGGER")
        # Commit the transaction to persist RawJobs, Canonical Jobs, Decisions, and CollectionRun
        await session.commit()
    except SQLAlchemyError as db_exc:
        await session.rollback()
        logger.error("Database error during collection execution: %s", db_exc)
        print(
            "ERROR: Database error occurred during collection; transaction rolled back.",
            file=sys.stderr,
        )
        raise

    comp_name = company_name
    if comp_name is None:
        existing_comp = _get_loaded_relationship(company_source, "company")
        if existing_comp is not None:
            comp_name = getattr(existing_comp, "name", None)
        else:
            comp_repo = CompanyRepository(session)
            db_comp = await comp_repo.get_by_id(company_source.company_id)
            if db_comp:
                comp_name = db_comp.name

    # Print summary output
    if output_json:
        print(
            format_collection_json(
                outcome,
                company_name=comp_name,
                board_token=company_source.ats_identifier,
            )
        )
    else:
        print(
            format_collection_summary(
                outcome,
                company_name=comp_name,
                board_token=company_source.ats_identifier,
            )
        )

    # Determine exit code:
    # 0 for SUCCESS, EMPTY, or PARTIAL_SUCCESS (run completed successfully)
    # 1 for FAILED, UNSUPPORTED_SOURCE, SKIPPED_INACTIVE
    exit_code = 0 if outcome.status in (STATUS_SUCCESS, STATUS_EMPTY, STATUS_PARTIAL_SUCCESS) else 1
    return outcome, exit_code
