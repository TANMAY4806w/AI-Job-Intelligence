"""Greenhouse Public ATS Collector (V1).

Boundary: Harvests un-mutated external job listings from public Greenhouse Job Board APIs:
1. Validates configured Greenhouse board token from CompanySource.
2. Discovers active job posts via public board listing GET endpoint.
3. Retrieves individual job-detail responses so that exact response bytes correspond
   to that specific job observation.
4. Constructs JobIngestionEnvelope preserving exact raw bytes and passes it through
   JobIngestionService.
5. Zero filtering: NEVER filters by title, experience, or salary.
6. Public GET endpoints only; zero credentials, cookies, or login required.
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
from app.persistence.models.source import CompanySource

logger = logging.getLogger(__name__)

GREENHOUSE_API_BASE = "https://boards-api.greenhouse.io/v1/boards"
BOARD_TOKEN_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_board_token(token: str | None) -> str:
    """Validate Greenhouse board token format.

    Args:
        token: Configured ATS identifier string.

    Returns:
        str: Sanitized and validated token.

    Raises:
        ValueError: If token is missing, blank, overly long, or contains invalid characters.
    """
    if not token or not isinstance(token, str):
        raise ValueError("Greenhouse board token must be a non-empty string.")

    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Greenhouse board token cannot be blank or whitespace-only.")

    if len(cleaned) > 128:
        raise ValueError(
            f"Greenhouse board token exceeds maximum length of 128 characters: {len(cleaned)}"
        )

    if not BOARD_TOKEN_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid Greenhouse board token '{cleaned}'. "
            "Tokens must be alphanumeric and may only contain hyphens or underscores."
        )

    return cleaned


class GreenhouseCollector(BaseCollector):
    """Collector adapter for the public Greenhouse Job Board API."""

    def __init__(
        self,
        ingestion_service: JobIngestionService,
        *,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Initialize Greenhouse collector.

        Args:
            ingestion_service: JobIngestionService instance for pipeline processing.
            http_client: Optional PoliteHttpClient instance; defaults to standard polite client.
        """
        super().__init__(ingestion_service, http_client=http_client)

    async def collect_company(
        self,
        company_source: CompanySource,
        company: Company | None = None,
        *,
        collection_run_id: uuid.UUID | None = None,
    ) -> CollectorResult:
        """Harvest all active job postings for a configured Greenhouse company source.

        Args:
            company_source: Configured CompanySource mapping with Greenhouse ats_identifier.
            company: Associated Company entity (if omitted, extracted from company_source.company).
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

        # 2. Validate Board Token
        board_token = validate_board_token(company_source.ats_identifier)

        # 3. Construct Discovery Endpoint
        listing_url = f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true"

        # 4. Fetch Public Board Listing
        try:
            response = await self.http_client.get(listing_url)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            logger.error("Failed to fetch Greenhouse board '%s': %s", board_token, exc)
            return CollectorResult(
                company_source_id=company_source.id,
                source_id=company_source.source_id,
                company_id=resolved_company.id,
                ats_identifier=board_token,
                status=STATUS_FAILED,
                total_discovered=0,
                total_ingested=0,
                total_failed=0,
                error_message=f"Transport error fetching Greenhouse board '{board_token}': {exc}",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if response.status_code != 200:
            logger.error(
                "Greenhouse board '%s' returned HTTP %d",
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
                    f"Greenhouse board listing returned HTTP {response.status_code}: {response.text[:200]}"
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
                error_message=f"Malformed JSON from Greenhouse board listing: {exc}",
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
                error_message="Greenhouse board listing response missing top-level 'jobs' list",
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
                error_message="Unexpected non-list type for 'jobs' in Greenhouse board response",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        total_discovered = len(discovered_jobs)
        if total_discovered == 0:
            # Genuine empty job board
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

        # 5. Fetch Individual Job Details & Ingest
        ingestion_outcomes: list[JobIngestionOutcome] = []
        failures: list[JobCollectionFailure] = []

        for job_summary in discovered_jobs:
            if not isinstance(job_summary, dict):
                failures.append(
                    JobCollectionFailure(
                        source_job_id=None,
                        source_url=listing_url,
                        error_type="INVALID_DATA",
                        error_message=f"Discovered job entry is not a dictionary: {type(job_summary)}",
                    )
                )
                continue

            raw_id = job_summary.get("id")
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

            detail_url = f"{GREENHOUSE_API_BASE}/{board_token}/jobs/{source_job_id}"

            # Fetch individual job detail response
            try:
                detail_resp = await self.http_client.get(detail_url)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="TIMEOUT"
                        if isinstance(exc, httpx.TimeoutException)
                        else "HTTP_ERROR",
                        error_message=f"Network error fetching job detail: {exc}",
                    )
                )
                continue

            if detail_resp.status_code != 200:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        http_status=detail_resp.status_code,
                        error_type="HTTP_ERROR",
                        error_message=f"HTTP {detail_resp.status_code} fetching job detail",
                    )
                )
                continue

            raw_body = detail_resp.content
            try:
                job_detail = detail_resp.json()
            except (json.JSONDecodeError, ValueError) as exc:
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="PARSE_ERROR",
                        error_message=f"Failed to parse JSON job detail response: {exc}",
                    )
                )
                continue

            if not isinstance(job_detail, dict):
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="PARSE_ERROR",
                        error_message=f"Job detail response is not a JSON object: {type(job_detail)}",
                    )
                )
                continue

            title = job_detail.get("title")
            if not title or not isinstance(title, str) or not title.strip():
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="INVALID_DATA",
                        error_message="Job detail missing required non-empty 'title' string",
                    )
                )
                continue

            # Extract location string
            location_str = self._extract_location(job_detail)

            # Preserve exact HTML description string returned inside JSON
            description_str = job_detail.get("content") or ""

            # Extract application URL
            app_url = job_detail.get("absolute_url") or job_summary.get("absolute_url")

            # Extract publication timestamp string strictly from first_published
            posted_at_raw = self._extract_publication_date(job_detail)

            envelope = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=job_detail,
                source_id=company_source.source_id,
                company_source_id=company_source.id,
                company_id=resolved_company.id,
                company_name=resolved_company.name,
                source_job_id=source_job_id,
                source_job_id_type="NATIVE",
                source_url=detail_url,
                application_url=app_url,
                payload_format="JSON",
                collected_at=datetime.now(UTC),
                collection_run_id=collection_run_id,
                http_status=detail_resp.status_code,
                content_hash=hashlib.sha256(raw_body).hexdigest(),
                extracted_data={
                    "title": title.strip(),
                    "description": description_str,
                    "location": location_str,
                    "posted_at": posted_at_raw,
                    "application_url": app_url,
                },
            )

            # Pass envelope to JobIngestionService
            try:
                outcome = await self.ingestion_service.ingest(envelope)
                ingestion_outcomes.append(outcome)
            except Exception as exc:
                logger.error("Ingestion failed for Greenhouse job %s: %s", source_job_id, exc)
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="INGESTION_ERROR",
                        error_message=f"Ingestion service error: {exc}",
                    )
                )

        # 6. Aggregate Status
        total_ingested = len(ingestion_outcomes)
        total_failed = len(failures)

        if total_failed == 0 and total_ingested > 0:
            final_status = STATUS_SUCCESS
        elif total_ingested > 0 and total_failed > 0:
            final_status = STATUS_PARTIAL_SUCCESS
        else:
            final_status = STATUS_FAILED

        return CollectorResult(
            company_source_id=company_source.id,
            source_id=company_source.source_id,
            company_id=resolved_company.id,
            ats_identifier=board_token,
            status=final_status,
            total_discovered=total_discovered,
            total_ingested=total_ingested,
            total_failed=total_failed,
            ingestion_outcomes=ingestion_outcomes,
            failures=failures,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    def _extract_location(self, job_detail: dict[str, Any]) -> str | None:
        """Extract location string from Greenhouse location object or field."""
        raw_loc = job_detail.get("location")
        if isinstance(raw_loc, dict):
            name = raw_loc.get("name")
            return name.strip() if name and isinstance(name, str) and name.strip() else None
        if isinstance(raw_loc, str) and raw_loc.strip():
            return raw_loc.strip()

        # Check offices list if location is missing
        offices = job_detail.get("offices", [])
        if isinstance(offices, list) and offices:
            first_office = offices[0]
            if isinstance(first_office, dict):
                loc = first_office.get("location") or first_office.get("name")
                if loc and isinstance(loc, str) and loc.strip():
                    return loc.strip()

        return None

    def _extract_publication_date(self, job_detail: dict[str, Any]) -> str | None:
        """Extract publication timestamp strictly from Greenhouse first_published field.

        Boundary invariants:
        1. Uses first_published as the publication-date input.
        2. Preserves updated_at in the raw source response (job_detail/raw_payload);
           never reinterprets updated_at as the original publication date.
        3. If first_published is missing, empty, or cannot be parsed as a valid date,
           returns None. Does NOT fall back to updated_at or collected_at.
        4. Leaves the publication date unresolved so existing verification and
           filtering rules handle it deterministically.
        """
        raw_published = job_detail.get("first_published")
        if not raw_published:
            return None

        # Verify whether raw_published is parseable into a valid date/timestamp
        try:
            parsed = parse_publication_date(raw_published)
            if parsed is not None:
                return raw_published if isinstance(raw_published, str) else parsed.isoformat()
        except (ValueError, TypeError):
            logger.warning(
                "Invalid first_published timestamp '%s' in Greenhouse job detail; leaving unresolved",
                raw_published,
            )
            return None

        return None
