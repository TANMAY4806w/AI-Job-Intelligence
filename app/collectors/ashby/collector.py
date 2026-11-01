"""Ashby Public ATS Collector.

Boundary: Harvests un-mutated external job postings from public Ashby Job Board APIs:
1. Validates configured Ashby board token (organization name) from CompanySource.
2. Discovers active job postings via public GET endpoint https://api.ashbyhq.com/posting-api/job-board/{organizationName}?includeCompensation=true.
3. Preserves exact raw payload bytes and SHA-256 cryptographic hash for each job observation.
4. Constructs JobIngestionEnvelope and passes it through JobIngestionService.
5. Zero filtering: NEVER filters by title, experience, or salary. All vacancies pass to pipeline.
6. Public GET endpoints only; zero credentials or authentication required.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    BaseCollector,
    CollectorResult,
    JobCollectionFailure,
)
from app.collectors.base.http import PoliteHttpClient
from app.normalization.dates import parse_publication_date
from app.orchestration.ingestion_service import (
    JobIngestionEnvelope,
    JobIngestionOutcome,
    JobIngestionService,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source
from app.verification.evaluator import APPROVED_SOURCE_TYPES
from app.verification.schemas import JobVerificationInput

logger = logging.getLogger(__name__)

ASHBY_API_BASE = "https://api.ashbyhq.com/posting-api/job-board"
BOARD_TOKEN_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_board_token(token: str | None) -> str:
    """Validate Ashby board token format.

    Args:
        token: Configured ATS identifier string (e.g. 'openai', 'ramp', 'linear').

    Returns:
        str: Sanitized and validated token.

    Raises:
        ValueError: If token is missing, blank, overly long, or contains invalid characters.
    """
    if not token or not isinstance(token, str):
        raise ValueError("Ashby board token must be a non-empty string.")

    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Ashby board token cannot be blank or whitespace-only.")

    if len(cleaned) > 128:
        raise ValueError(
            f"Ashby board token exceeds maximum length of 128 characters: {len(cleaned)}"
        )

    if not BOARD_TOKEN_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid Ashby board token '{cleaned}'. "
            "Tokens must be alphanumeric and may only contain hyphens or underscores."
        )

    return cleaned


class AshbyCollector(BaseCollector):
    """Collector adapter for the public Ashby Job Board API."""

    def __init__(
        self,
        ingestion_service: JobIngestionService,
        *,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Initialize Ashby collector.

        Args:
            ingestion_service: JobIngestionService instance for pipeline processing.
            http_client: Optional PoliteHttpClient instance; defaults to standard polite client.
        """
        super().__init__(ingestion_service, http_client=http_client)

    async def collect_company(
        self,
        company_source: CompanySource,
        company: Company | None = None,
        source: Source | None = None,
        *,
        collection_run_id: uuid.UUID | None = None,
    ) -> CollectorResult:
        """Harvest all active job postings for a configured Ashby company source.

        Args:
            company_source: Configured CompanySource mapping with Ashby ats_identifier.
            company: Associated Company entity (if omitted, extracted from company_source.company).
            source: Associated Source catalog entity (if omitted, extracted from company_source.source).
            collection_run_id: Optional collection run context UUID.

        Returns:
            CollectorResult: Aggregate outcome including ingested jobs and diagnostic failures.

        Raises:
            ValueError: If company_source is missing required attributes or board token is invalid.
        """
        started_at = datetime.now(UTC)

        # 1. Resolve Company entity
        resolved_company = company or getattr(company_source, "company", None)
        if resolved_company is None:
            raise ValueError(
                "Company entity must be provided or accessible on company_source.company."
            )

        # 2. Resolve Source entity and derive explicit source approval from catalog
        resolved_source = source or getattr(company_source, "source", None)
        source_name: str | None = None
        source_type: str | None = None
        is_source_approved: bool | None = None

        if resolved_source is not None:
            source_name = resolved_source.source_name
            source_type = resolved_source.source_type
            s_name_clean = (source_name or "").strip().lower()
            is_supported_platform = s_name_clean in APPROVED_SOURCE_TYPES
            if (
                resolved_source.is_active
                and getattr(company_source, "is_active", True)
                and is_supported_platform
            ):
                is_source_approved = True
            else:
                is_source_approved = False

        # 3. Validate Board Token
        board_token = validate_board_token(company_source.ats_identifier)

        # 4. Construct Discovery Endpoint
        listing_url = f"{ASHBY_API_BASE}/{board_token}?includeCompensation=true"

        # 5. Fetch Public Board Listing
        try:
            response = await self.http_client.get(listing_url)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.error("Failed to fetch Ashby board '%s': %s", board_token, exc)
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message=f"Transport error fetching Ashby board '{board_token}': {exc}",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if response.status_code != 200:
            logger.error(
                "Ashby board '%s' returned HTTP %d",
                board_token,
                response.status_code,
            )
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message=(
                    f"Ashby board listing returned HTTP {response.status_code}: {response.text[:200]}"
                ),
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        try:
            board_data = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message=f"Malformed JSON from Ashby board listing: {exc}",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if not isinstance(board_data, dict) or "jobs" not in board_data:
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message="Ashby board listing response missing top-level 'jobs' list",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        discovered_jobs = board_data.get("jobs", [])
        if not isinstance(discovered_jobs, list):
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message="Unexpected non-list type for 'jobs' in Ashby board response",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        total_discovered = len(discovered_jobs)
        if total_discovered == 0:
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_EMPTY,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        board_response_hash = hashlib.sha256(response.content).hexdigest()

        # 6. Parse and Ingest Individual Jobs
        ingestion_outcomes: list[JobIngestionOutcome] = []
        failures: list[JobCollectionFailure] = []

        for job_payload in discovered_jobs:
            if not isinstance(job_payload, dict):
                failures.append(
                    JobCollectionFailure(
                        source_job_id=None,
                        source_url=listing_url,
                        error_type="INVALID_DATA",
                        error_message=f"Discovered job entry is not a dictionary: {type(job_payload)}",
                    )
                )
                continue

            raw_id = job_payload.get("id")
            if raw_id is None:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=None,
                        source_url=listing_url,
                        error_type="INVALID_DATA",
                        error_message="Discovered job missing required 'id' attribute",
                    )
                )
                continue

            source_job_id = str(raw_id).strip()
            if not source_job_id:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=None,
                        source_url=listing_url,
                        error_type="INVALID_DATA",
                        error_message="Discovered job has empty 'id' attribute",
                    )
                )
                continue

            source_url = (
                job_payload.get("jobUrl")
                or f"https://jobs.ashbyhq.com/{board_token}/{source_job_id}"
            )
            app_url = job_payload.get("applyUrl") or source_url

            title = job_payload.get("title")
            if not title or not isinstance(title, str) or not title.strip():
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=source_url,
                        error_type="INVALID_DATA",
                        error_message="Job detail missing required non-empty 'title' string",
                    )
                )
                continue

            # Verify listed status: unlisted jobs cannot enter the public feed as ordinary listed jobs
            is_listed = job_payload.get("isListed")
            if is_listed is False:
                logger.debug(
                    "Skipping unlisted job '%s' (%s) for Ashby board '%s'",
                    source_job_id,
                    title,
                    board_token,
                )
                continue

            # Serialize exact individual job payload to UTF-8 bytes for RawJob persistence
            raw_body = json.dumps(job_payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            content_hash = hashlib.sha256(raw_body).hexdigest()

            # Extract location
            location_str = self._extract_location(job_payload)

            # Preserve description (HTML preferred, plaintext fallback)
            description_str = (
                job_payload.get("descriptionHtml") or job_payload.get("descriptionPlain") or ""
            )

            # Extract publication date
            published_at_raw = job_payload.get("publishedAt")
            posted_at_dt = None
            if published_at_raw:
                try:
                    posted_at_dt = parse_publication_date(published_at_raw)
                except (ValueError, TypeError):
                    posted_at_dt = None

            collected_now = datetime.now(UTC)

            # Derive workplace and remote work classification
            remote_type_val = self._derive_remote_type(job_payload)

            # Verification observations
            verification_evidence = JobVerificationInput(
                source_id=company_source.source_id,
                source_name=source_name,
                source_type=source_type,
                is_source_approved=is_source_approved,
                payload_format_valid=True,
                description=description_str,
                http_status_code=response.status_code,
                is_page_active=True,
                application_url=app_url,
                source_url=source_url,
                company_name=resolved_company.name,
                company_id=resolved_company.id,
                location=location_str,
                posted_at=posted_at_dt,
                collected_at=collected_now,
            )

            envelope = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=job_payload,
                source_id=company_source.source_id,
                company_source_id=company_source.id,
                company_id=resolved_company.id,
                company_name=resolved_company.name,
                source_job_id=source_job_id,
                source_job_id_type="NATIVE",
                source_url=source_url,
                application_url=app_url,
                payload_format="JSON",
                collected_at=collected_now,
                collection_run_id=collection_run_id,
                http_status=response.status_code,
                content_hash=content_hash,
                extracted_data={
                    "title": title.strip(),
                    "description": description_str,
                    "location": location_str,
                    "posted_at": published_at_raw,
                    "application_url": app_url,
                    "remote_type": remote_type_val,
                    "employment_type": job_payload.get("employmentType"),
                    "is_listed": is_listed,
                    "workplace_type": job_payload.get("workplaceType"),
                    "board_wire_hash": board_response_hash,
                    "provenance_mode": "ASHBY_OBJECT_SERIALIZED",
                },
                verification_evidence=verification_evidence,
            )

            try:
                outcome = await self.ingestion_service.ingest(envelope)
                ingestion_outcomes.append(outcome)
            except Exception as exc:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=source_url,
                        error_type="INGESTION_ERROR",
                        error_message=f"Pipeline ingestion failed: {exc}",
                    )
                )

        # 7. Compute aggregate outcome
        finished_at = datetime.now(UTC)
        total_ingested = len(ingestion_outcomes)
        total_failed = len(failures)

        if total_ingested > 0 and total_failed == 0:
            status = STATUS_SUCCESS
        elif total_ingested > 0 and total_failed > 0:
            status = STATUS_PARTIAL_SUCCESS
        elif total_ingested == 0 and total_failed > 0:
            status = STATUS_FAILED
        else:
            status = STATUS_EMPTY

        return CollectorResult(
            company_source_id=company_source.id,
            source_id=company_source.source_id,
            company_id=resolved_company.id,
            ats_identifier=board_token,
            status=status,
            total_discovered=total_discovered,
            total_ingested=total_ingested,
            total_failed=total_failed,
            ingestion_outcomes=ingestion_outcomes,
            failures=failures,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _extract_location(self, job_data: dict[str, Any]) -> str | None:
        """Extract geographic location string from Ashby job dictionary.

        Args:
            job_data: Raw Ashby job posting dictionary.

        Returns:
            str | None: Cleaned geographic location string or None if absent.
        """
        loc = job_data.get("location")
        if loc and isinstance(loc, str) and loc.strip():
            return loc.strip()

        # Check address postalAddress dictionary
        address = job_data.get("address")
        if isinstance(address, dict):
            postal = address.get("postalAddress")
            if isinstance(postal, dict):
                parts = [
                    postal.get("addressLocality"),
                    postal.get("addressRegion"),
                    postal.get("addressCountry"),
                ]
                valid_parts = [str(p).strip() for p in parts if p and str(p).strip()]
                if valid_parts:
                    return ", ".join(valid_parts)

        # Do not fabricate geographic location from workplaceType or isRemote
        return None

    def _derive_remote_type(self, job_data: dict[str, Any]) -> str | None:
        """Derive remote work classification from Ashby workplaceType and isRemote.

        Guarantees:
        - Accurately distinguishes OnSite, Remote, and Hybrid.
        - Does NOT treat isRemote=true alone as proof of fully remote work if workplaceType is Hybrid.
        - Preserves missing values as None rather than fabricating defaults.

        Args:
            job_data: Raw Ashby job posting dictionary.

        Returns:
            str | None: Standardized remote type ('REMOTE', 'HYBRID', 'ON_SITE') or None.
        """
        wp = job_data.get("workplaceType")
        if isinstance(wp, str) and wp.strip():
            wp_clean = wp.strip().lower()
            if wp_clean in ("remote", "wfh", "work from home"):
                return "REMOTE"
            elif wp_clean in ("hybrid", "partially remote"):
                return "HYBRID"
            elif wp_clean in ("onsite", "on-site", "in-office", "in office"):
                return "ON_SITE"

        # If workplaceType is absent, check isRemote boolean flag
        is_remote = job_data.get("isRemote")
        if is_remote is True:
            return "REMOTE"
        elif is_remote is False:
            return "ON_SITE"

        return None
