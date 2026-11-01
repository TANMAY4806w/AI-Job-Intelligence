"""Lever Public ATS Collector.

Boundary: Harvests un-mutated external job postings from public Lever Job Board APIs:
1. Validates configured Lever board token (company site slug) from CompanySource.
2. Discovers active job postings via public GET endpoint https://api.lever.co/v0/postings/{company}?mode=json.
3. Retrieves individual job-detail responses so that exact response bytes correspond
   to that specific job observation.
4. Constructs JobIngestionEnvelope preserving exact raw bytes and passes it through
   JobIngestionService.
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

LEVER_API_BASE = "https://api.lever.co/v0/postings"
DEFAULT_LEVER_PAGE_SIZE = 100
MAX_LEVER_PAGES = 50
BOARD_TOKEN_REGEX = re.compile(r"^[a-zA-Z0-9_\-]+$")


def validate_board_token(token: str | None) -> str:
    """Validate Lever board token format.

    Args:
        token: Configured ATS identifier string (e.g. 'spotify', 'palantir').

    Returns:
        str: Sanitized and validated token.

    Raises:
        ValueError: If token is missing, blank, overly long, or contains invalid characters.
    """
    if not token or not isinstance(token, str):
        raise ValueError("Lever board token must be a non-empty string.")

    cleaned = token.strip()
    if not cleaned:
        raise ValueError("Lever board token cannot be blank or whitespace-only.")

    if len(cleaned) > 128:
        raise ValueError(
            f"Lever board token exceeds maximum length of 128 characters: {len(cleaned)}"
        )

    if not BOARD_TOKEN_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid Lever board token '{cleaned}'. "
            "Tokens must be alphanumeric and may only contain hyphens or underscores."
        )

    return cleaned


class LeverCollector(BaseCollector):
    """Collector adapter for the public Lever Postings API."""

    def __init__(
        self,
        ingestion_service: JobIngestionService,
        *,
        http_client: PoliteHttpClient | None = None,
        page_size: int = DEFAULT_LEVER_PAGE_SIZE,
        max_pages: int = MAX_LEVER_PAGES,
    ) -> None:
        """Initialize Lever collector.

        Args:
            ingestion_service: JobIngestionService instance for pipeline processing.
            http_client: Optional PoliteHttpClient instance; defaults to standard polite client.
            page_size: Page size for skip/limit Lever queries.
            max_pages: Maximum pages to retrieve per collection cycle.
        """
        super().__init__(ingestion_service, http_client=http_client)
        self.page_size = page_size
        self.max_pages = max_pages

    async def collect_company(
        self,
        company_source: CompanySource,
        company: Company | None = None,
        source: Source | None = None,
        *,
        collection_run_id: uuid.UUID | None = None,
    ) -> CollectorResult:
        """Harvest all active job postings for a configured Lever company source.

        Args:
            company_source: Configured CompanySource mapping with Lever ats_identifier.
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
        board_url = f"{LEVER_API_BASE}/{board_token}"

        # 4. Fetch Public Board Listing with Pagination (skip / limit)
        skip = 0
        limit = self.page_size
        pages_processed = 0
        board_data: list[dict[str, Any]] = []
        seen_job_ids: set[str] = set()
        failures: list[JobCollectionFailure] = []
        is_truncated = False

        while True:
            listing_url = f"{LEVER_API_BASE}/{board_token}?mode=json&skip={skip}&limit={limit}"
            try:
                response = await self.http_client.get(listing_url)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                logger.error(
                    "Failed to fetch Lever board '%s' at skip=%d: %s",
                    board_token,
                    skip,
                    exc,
                )
                if pages_processed == 0:
                    return CollectorResult(
                        company_source_id=company_source.id,
                        source_id=company_source.source_id,
                        company_id=resolved_company.id,
                        ats_identifier=board_token,
                        status=STATUS_FAILED,
                        total_discovered=0,
                        total_ingested=0,
                        total_failed=0,
                        error_message=f"Transport error fetching Lever board '{board_token}': {exc}",
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                    )
                else:
                    logger.warning(
                        "Pagination halted early on page %d for Lever board '%s' due to transport error: %s",
                        pages_processed + 1,
                        board_token,
                        exc,
                    )
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=None,
                            source_url=listing_url,
                            error_type="PAGINATION_TRANSPORT_ERROR",
                            error_message=f"Pagination halted on page {pages_processed + 1}: {exc}",
                        )
                    )
                    break

            if response.status_code != 200:
                logger.error(
                    "Lever board '%s' returned HTTP %d at skip=%d",
                    board_token,
                    response.status_code,
                    skip,
                )
                if pages_processed == 0:
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
                            f"Lever board listing returned HTTP {response.status_code}: {response.text[:200]}"
                        ),
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                    )
                else:
                    logger.warning(
                        "Pagination stopped due to HTTP %d on page %d for Lever board '%s'",
                        response.status_code,
                        pages_processed + 1,
                        board_token,
                    )
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=None,
                            source_url=listing_url,
                            http_status=response.status_code,
                            error_type="PAGINATION_HTTP_ERROR",
                            error_message=f"HTTP {response.status_code} on page {pages_processed + 1}",
                        )
                    )
                    break

            try:
                page_items = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                if pages_processed == 0:
                    return CollectorResult(
                        company_source_id=company_source.id,
                        source_id=company_source.source_id,
                        company_id=resolved_company.id,
                        ats_identifier=board_token,
                        status=STATUS_FAILED,
                        total_discovered=0,
                        total_ingested=0,
                        total_failed=0,
                        error_message=f"Malformed JSON from Lever board listing: {exc}",
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                    )
                else:
                    logger.warning(
                        "Malformed JSON on page %d for Lever board '%s'",
                        pages_processed + 1,
                        board_token,
                    )
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=None,
                            source_url=listing_url,
                            error_type="PAGINATION_PARSE_ERROR",
                            error_message=f"Malformed JSON on page {pages_processed + 1}: {exc}",
                        )
                    )
                    break

            if not isinstance(page_items, list):
                if pages_processed == 0:
                    return CollectorResult(
                        company_source_id=company_source.id,
                        source_id=company_source.source_id,
                        company_id=resolved_company.id,
                        ats_identifier=board_token,
                        status=STATUS_FAILED,
                        total_discovered=0,
                        total_ingested=0,
                        total_failed=0,
                        error_message="Lever board listing response is not a top-level JSON array",
                        started_at=started_at,
                        finished_at=datetime.now(UTC),
                    )
                else:
                    break

            if not page_items:
                break

            for item in page_items:
                if isinstance(item, dict):
                    jid = str(item.get("id") or "").strip()
                    if jid and jid in seen_job_ids:
                        continue
                    if jid:
                        seen_job_ids.add(jid)
                board_data.append(item)

            pages_processed += 1
            if len(page_items) < limit:
                break

            skip += len(page_items)
            if pages_processed >= self.max_pages:
                if len(page_items) == limit:
                    is_truncated = True
                    logger.warning(
                        "Reached maximum allowed pages (%d) for Lever board '%s'; more jobs may exist (truncated harvest)",
                        self.max_pages,
                        board_token,
                    )
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=None,
                            source_url=board_url,
                            error_type="TRUNCATED_HARVEST",
                            error_message=(
                                f"Collection truncated at max pages limit ({self.max_pages} pages, "
                                f"{len(board_data)} jobs). Additional jobs may exist on Lever board."
                            ),
                        )
                    )
                break

        logger.info(
            "Lever harvest for '%s': %d unique jobs discovered across %d page(s)",
            board_token,
            len(board_data),
            pages_processed,
        )

        total_discovered = len(board_data)
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

        # 6. Fetch Individual Job Details & Ingest
        ingestion_outcomes: list[JobIngestionOutcome] = []

        for job_summary in board_data:
            if not isinstance(job_summary, dict):
                failures.append(
                    JobCollectionFailure(
                        source_job_id=None,
                        source_url=board_url,
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
                        source_url=board_url,
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
                        source_url=board_url,
                        error_type="INVALID_DATA",
                        error_message="Discovered job has empty 'id' attribute",
                    )
                )
                continue

            detail_url = f"{LEVER_API_BASE}/{board_token}/{source_job_id}?mode=json"

            # Fetch individual job detail response to preserve exact HTTP wire bytes
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
                        error_message=f"Network error fetching Lever job detail: {exc}",
                    )
                )
                continue

            if detail_resp.status_code != 200:
                # If single-job endpoint is not available (e.g. 404), fallback to using summary dict from list
                if detail_resp.status_code == 404:
                    raw_payload = job_summary
                    raw_body = json.dumps(
                        job_summary, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    detail_status = 200
                else:
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=source_job_id,
                            source_url=detail_url,
                            http_status=detail_resp.status_code,
                            error_type="HTTP_ERROR",
                            error_message=f"HTTP {detail_resp.status_code} fetching Lever job detail",
                        )
                    )
                    continue
            else:
                raw_body = detail_resp.content
                try:
                    raw_payload = detail_resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    failures.append(
                        JobCollectionFailure(
                            source_job_id=source_job_id,
                            source_url=detail_url,
                            error_type="PARSE_ERROR",
                            error_message=f"Failed to parse JSON Lever job detail: {exc}",
                        )
                    )
                    continue
                detail_status = detail_resp.status_code

            if not isinstance(raw_payload, dict):
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="PARSE_ERROR",
                        error_message=f"Job detail response is not a JSON object: {type(raw_payload)}",
                    )
                )
                continue

            # Lever uses 'text' for job title
            title = raw_payload.get("text")
            if not title or not isinstance(title, str) or not title.strip():
                failures.append(
                    JobCollectionFailure(
                        source_job_id=source_job_id,
                        source_url=detail_url,
                        error_type="INVALID_DATA",
                        error_message="Job detail missing required non-empty 'text' (title) string",
                    )
                )
                continue

            # Extract location string from categories or country
            location_str = self._extract_location(raw_payload)

            # Preserve description (HTML or plain)
            description_str = (
                raw_payload.get("description") or raw_payload.get("descriptionPlain") or ""
            )

            # Extract application URL and source URL
            app_url = raw_payload.get("applyUrl") or raw_payload.get("hostedUrl")
            source_url = raw_payload.get("hostedUrl") or detail_url

            # Extract publication timestamp string from createdAt (epoch ms)
            created_at_val = raw_payload.get("createdAt")
            posted_at_dt = None
            if created_at_val is not None:
                try:
                    posted_at_dt = parse_publication_date(created_at_val)
                except (ValueError, TypeError):
                    posted_at_dt = None

            collected_now = datetime.now(UTC)

            # Construct verified evidence observations from collector harvest
            verification_evidence = JobVerificationInput(
                source_id=company_source.source_id,
                source_name=source_name,
                source_type=source_type,
                is_source_approved=is_source_approved,
                payload_format_valid=True,
                description=description_str,
                http_status_code=detail_status,
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
                raw_payload=raw_payload,
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
                http_status=detail_status,
                content_hash=hashlib.sha256(raw_body).hexdigest(),
                extracted_data={
                    "title": title.strip(),
                    "description": description_str,
                    "location": location_str,
                    "posted_at": posted_at_dt.isoformat() if posted_at_dt else None,
                    "application_url": app_url,
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

        if total_ingested > 0 and total_failed == 0 and not is_truncated:
            status = STATUS_SUCCESS
        elif total_ingested > 0 and (total_failed > 0 or is_truncated):
            status = STATUS_PARTIAL_SUCCESS
        elif total_ingested == 0 and total_failed > 0:
            status = STATUS_FAILED
        else:
            status = STATUS_EMPTY

        agg_error_msg = None
        if is_truncated:
            agg_error_msg = (
                f"Harvest truncated at maximum page limit ({self.max_pages} pages, "
                f"{total_discovered} jobs). Additional jobs may exist on Lever board."
            )

        return CollectorResult(
            company_source_id=company_source.id,
            source_id=company_source.source_id,
            company_id=resolved_company.id,
            ats_identifier=board_token,
            status=status,
            total_discovered=total_discovered,
            total_ingested=total_ingested,
            total_failed=total_failed,
            error_message=agg_error_msg,
            ingestion_outcomes=ingestion_outcomes,
            failures=failures,
            started_at=started_at,
            finished_at=finished_at,
        )

    def _extract_location(self, job_data: dict[str, Any]) -> str | None:
        """Extract location string from Lever categories dictionary.

        Args:
            job_data: Raw Lever job posting dictionary.

        Returns:
            str | None: Cleaned location string or None if absent.
        """
        categories = job_data.get("categories")
        if isinstance(categories, dict):
            loc = categories.get("location")
            if loc and isinstance(loc, str) and loc.strip():
                return loc.strip()

        # Check country as fallback
        country = job_data.get("country")
        if country and isinstance(country, str) and country.strip():
            return country.strip()

        # Check workplaceType (e.g. 'remote')
        wp = job_data.get("workplaceType")
        if wp and isinstance(wp, str) and wp.strip():
            return wp.strip()

        return None
