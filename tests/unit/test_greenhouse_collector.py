"""Unit tests for GreenhouseCollector and PoliteHttpClient.

Boundary: 100% isolated unit tests with zero external network access.
All HTTP responses and repository sessions are mocked.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import (
    GREENHOUSE_API_BASE,
    GreenhouseCollector,
    validate_board_token,
)
from app.normalization.service import JobNormalizationService
from app.orchestration.ingestion_service import (
    JobIngestionEnvelope,
    JobIngestionOutcome,
    JobIngestionService,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource

SAMPLE_LISTING_RESPONSE = {
    "jobs": [
        {
            "id": 101,
            "title": "Backend Python Engineer",
            "updated_at": "2026-03-01T12:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
            "location": {"name": "Bengaluru, India"},
        },
        {
            "id": 102,
            "title": "Frontend React Engineer",
            "updated_at": "2026-03-02T12:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
            "location": {"name": "Mumbai, India"},
        },
    ],
    "meta": {"total": 2},
}

SAMPLE_JOB_DETAIL_101 = {
    "id": 101,
    "title": "Backend Python Engineer",
    "first_published": "2026-02-15T10:00:00Z",
    "updated_at": "2026-03-01T12:00:00Z",
    "content": "<p>We are seeking a talented Python engineer with FastAPI and PostgreSQL expertise.</p>",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/101",
    "location": {"name": "Bengaluru, India"},
    "departments": [{"name": "Engineering"}],
    "offices": [{"name": "Bengaluru Office", "location": "Bengaluru"}],
}

SAMPLE_JOB_DETAIL_102 = {
    "id": 102,
    "title": "Frontend React Engineer",
    "first_published": "2026-02-20T08:00:00Z",
    "updated_at": "2026-03-02T12:00:00Z",
    "content": "<p>Looking for a React developer to build modern web interfaces.</p>",
    "absolute_url": "https://boards.greenhouse.io/acme/jobs/102",
    "location": {"name": "Mumbai, India"},
    "departments": [{"name": "Product"}],
    "offices": [],
}


@pytest.fixture
def mock_company() -> Company:
    return Company(
        id=uuid.uuid4(),
        name="Acme Technologies",
        normalized_name="acme-technologies",
    )


@pytest.fixture
def mock_company_source(mock_company: Company) -> CompanySource:
    return CompanySource(
        id=uuid.uuid4(),
        company_id=mock_company.id,
        source_id=uuid.uuid4(),
        ats_identifier="acme",
        discovery_method="AUTOMATED_PROBE",
    )


@pytest.fixture
def mock_ingestion_service() -> MagicMock:
    service = MagicMock(spec=JobIngestionService)
    service.ingest = AsyncMock(
        return_value=JobIngestionOutcome(
            status="PROCESSED_NEW",
            raw_job=MagicMock(),
            deduplication_result=MagicMock(),
            canonical_job=MagicMock(),
        )
    )
    return service


# ============================================================================
# 1. Board Token Validation Tests
# ============================================================================


def test_validate_board_token_valid() -> None:
    assert validate_board_token("acme") == "acme"
    assert validate_board_token("stripe-inc") == "stripe-inc"
    assert validate_board_token("brex_careers_1") == "brex_careers_1"
    assert validate_board_token("  uber  ") == "uber"


def test_validate_board_token_invalid_types_and_blanks() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        validate_board_token(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty string"):
        validate_board_token("")
    with pytest.raises(ValueError, match="blank or whitespace-only"):
        validate_board_token("   ")


def test_validate_board_token_overly_long() -> None:
    with pytest.raises(ValueError, match="maximum length of 128"):
        validate_board_token("a" * 129)


def test_validate_board_token_invalid_characters() -> None:
    with pytest.raises(ValueError, match="alphanumeric and may only contain hyphens"):
        validate_board_token("acme/jobs")
    with pytest.raises(ValueError, match="alphanumeric and may only contain hyphens"):
        validate_board_token("https://boards.greenhouse.io/acme")
    with pytest.raises(ValueError, match="alphanumeric and may only contain hyphens"):
        validate_board_token("acme?token=123")
    with pytest.raises(ValueError, match="alphanumeric and may only contain hyphens"):
        validate_board_token("../acme")


# ============================================================================
# 2. Collector Execution Tests
# ============================================================================


@pytest.mark.asyncio
async def test_successful_board_collection_and_envelope_creation(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify standard happy path: discovers job IDs, fetches detail per job, and constructs envelopes."""
    raw_listing_bytes = json.dumps(SAMPLE_LISTING_RESPONSE).encode("utf-8")
    raw_101_bytes = json.dumps(SAMPLE_JOB_DETAIL_101).encode("utf-8")
    raw_102_bytes = json.dumps(SAMPLE_JOB_DETAIL_102).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers

        if url_str == f"{GREENHOUSE_API_BASE}/acme/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/acme/jobs/101":
            return httpx.Response(200, content=raw_101_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/acme/jobs/102":
            return httpx.Response(200, content=raw_102_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        http_client = PoliteHttpClient(client=client)
        collector = GreenhouseCollector(mock_ingestion_service, http_client=http_client)

        result = await collector.collect_company(mock_company_source, mock_company)

        assert result.status == STATUS_SUCCESS
        assert result.is_success is True
        assert result.total_discovered == 2
        assert result.total_ingested == 2
        assert result.total_failed == 0
        assert len(result.failures) == 0
        assert len(result.ingestion_outcomes) == 2

        # Verify JobIngestionService calls
        assert mock_ingestion_service.ingest.call_count == 2
        envelopes: list[JobIngestionEnvelope] = [
            call.args[0] for call in mock_ingestion_service.ingest.call_args_list
        ]

        # Verify Envelope 1 (Job 101)
        env1 = envelopes[0]
        assert env1.source_id == mock_company_source.source_id
        assert env1.company_source_id == mock_company_source.id
        assert env1.company_id == mock_company.id
        assert env1.company_name == "Acme Technologies"
        assert env1.source_job_id == "101"
        assert env1.source_job_id_type == "NATIVE"
        assert env1.source_url == f"{GREENHOUSE_API_BASE}/acme/jobs/101"
        assert env1.application_url == "https://boards.greenhouse.io/acme/jobs/101"
        assert env1.payload_format == "JSON"
        assert env1.raw_body == raw_101_bytes
        assert env1.raw_payload == SAMPLE_JOB_DETAIL_101
        assert env1.content_hash == hashlib.sha256(raw_101_bytes).hexdigest()
        assert env1.extracted_data is not None
        assert env1.extracted_data["title"] == "Backend Python Engineer"
        assert "<p>We are seeking a talented Python engineer" in env1.extracted_data["description"]
        assert env1.extracted_data["location"] == "Bengaluru, India"
        assert env1.extracted_data["posted_at"] == "2026-02-15T10:00:00Z"
        assert env1.raw_payload["updated_at"] == "2026-03-01T12:00:00Z"
        assert env1.raw_payload["first_published"] == "2026-02-15T10:00:00Z"

        # Verify Envelope 2 (Job 102)
        env2 = envelopes[1]
        assert env2.source_job_id == "102"
        assert env2.raw_body == raw_102_bytes
        assert env2.raw_payload == SAMPLE_JOB_DETAIL_102
        assert env2.content_hash == hashlib.sha256(raw_102_bytes).hexdigest()


@pytest.mark.asyncio
async def test_empty_board_vs_failed_request(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify 200 OK with empty jobs array is marked EMPTY, while HTTP 404 is marked FAILED."""

    # 1. Test Genuine Empty Board
    async def empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps({"jobs": [], "meta": {"total": 0}}).encode("utf-8")
        )

    transport_empty = httpx.MockTransport(empty_handler)
    async with httpx.AsyncClient(transport=transport_empty) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result_empty = await collector.collect_company(mock_company_source, mock_company)
        assert result_empty.status == STATUS_EMPTY
        assert result_empty.is_empty is True
        assert result_empty.total_discovered == 0
        assert result_empty.total_ingested == 0
        assert result_empty.total_failed == 0
        assert mock_ingestion_service.ingest.call_count == 0

    # 2. Test 404 Board Not Found (Must NEVER be interpreted as empty)
    async def not_found_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"error": "Board not found"}')

    transport_404 = httpx.MockTransport(not_found_handler)
    async with httpx.AsyncClient(transport=transport_404) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result_404 = await collector.collect_company(mock_company_source, mock_company)
        assert result_404.status == STATUS_FAILED
        assert result_404.is_failed is True
        assert result_404.is_empty is False
        assert "HTTP 404" in (result_404.error_message or "")


@pytest.mark.asyncio
async def test_per_job_detail_failure_produces_partial_success(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that when 1 job detail fails (e.g. 404) and 1 succeeds, status is PARTIAL_SUCCESS."""
    raw_listing_bytes = json.dumps(SAMPLE_LISTING_RESPONSE).encode("utf-8")
    raw_101_bytes = json.dumps(SAMPLE_JOB_DETAIL_101).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/acme/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/acme/jobs/101":
            return httpx.Response(200, content=raw_101_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/acme/jobs/102":
            # Job 102 closed between listing and detail fetch
            return httpx.Response(404, content=b'{"error": "Job not found"}')
        return httpx.Response(500)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)

        assert result.status == STATUS_PARTIAL_SUCCESS
        assert result.is_partial is True
        assert result.total_discovered == 2
        assert result.total_ingested == 1
        assert result.total_failed == 1
        assert len(result.failures) == 1
        failure = result.failures[0]
        assert failure.source_job_id == "102"
        assert failure.http_status == 404
        assert failure.error_type == "HTTP_ERROR"


@pytest.mark.asyncio
async def test_all_job_details_fail_produces_failed_status(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that when all discovered job details fail, status is FAILED."""
    raw_listing_bytes = json.dumps(SAMPLE_LISTING_RESPONSE).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/acme/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(500, content=b"Server error")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service,
            http_client=PoliteHttpClient(client=client, max_retries=1, backoff_factor=0.01),
        )
        result = await collector.collect_company(mock_company_source, mock_company)

        assert result.status == STATUS_FAILED
        assert result.is_failed is True
        assert result.total_discovered == 2
        assert result.total_ingested == 0
        assert result.total_failed == 2
        assert len(result.failures) == 2


@pytest.mark.asyncio
async def test_malformed_json_responses_handled_safely(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify malformed JSON in listing fails board safely, while malformed detail records a failure."""

    # 1. Malformed Listing JSON
    async def bad_listing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>Not JSON</html>")

    transport_bad_listing = httpx.MockTransport(bad_listing_handler)
    async with httpx.AsyncClient(transport=transport_bad_listing) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_FAILED
        assert "Malformed JSON" in (result.error_message or "")

    # 2. Malformed Detail JSON
    async def bad_detail_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "jobs?content=true" in url_str:
            return httpx.Response(200, content=json.dumps({"jobs": [{"id": 999}]}).encode("utf-8"))
        return httpx.Response(200, content=b"invalid {json [bytes")

    transport_bad_detail = httpx.MockTransport(bad_detail_handler)
    async with httpx.AsyncClient(transport=transport_bad_detail) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_FAILED
        assert result.total_discovered == 1
        assert result.total_failed == 1
        assert result.failures[0].error_type == "PARSE_ERROR"


@pytest.mark.asyncio
async def test_missing_or_invalid_job_fields_handled_gracefully(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify discovered jobs missing ID or title are rejected as INVALID_DATA."""
    # Listing with missing 'id'
    listing_data = {"jobs": [{"title": "No ID Job"}, {"id": None}]}

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(listing_data).encode("utf-8"))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_FAILED
        assert result.total_discovered == 2
        assert result.total_failed == 2
        for f in result.failures:
            assert f.error_type == "INVALID_DATA"


@pytest.mark.asyncio
async def test_transport_error_or_timeout_on_board_listing(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify transport error or timeout during board listing produces FAILED result with diagnostics."""

    async def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("Connection timed out to Greenhouse")

    transport = httpx.MockTransport(error_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service,
            http_client=PoliteHttpClient(client=client, max_retries=1, backoff_factor=0.01),
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_FAILED
        assert "Transport error" in (result.error_message or "")


@pytest.mark.asyncio
async def test_ingestion_service_exception_captured_as_failure(
    mock_company: Company,
    mock_company_source: CompanySource,
) -> None:
    """Verify that if JobIngestionService raises an error, it is recorded in failures without crashing."""
    raw_listing_bytes = json.dumps({"jobs": [{"id": 101}]}).encode("utf-8")
    raw_101_bytes = json.dumps(SAMPLE_JOB_DETAIL_101).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_101_bytes)

    failing_ingestion_service = MagicMock(spec=JobIngestionService)
    failing_ingestion_service.ingest = AsyncMock(
        side_effect=RuntimeError("Database constraint error")
    )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            failing_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)

        assert result.status == STATUS_FAILED
        assert result.total_discovered == 1
        assert result.total_failed == 1
        assert result.failures[0].error_type == "INGESTION_ERROR"
        assert "Database constraint error" in result.failures[0].error_message


@pytest.mark.asyncio
async def test_deterministic_behavior_given_equivalent_mocked_responses(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that running collection on identical responses produces identical envelopes and hash."""
    raw_listing_bytes = json.dumps({"jobs": [{"id": 101}]}).encode("utf-8")
    raw_101_bytes = json.dumps(SAMPLE_JOB_DETAIL_101).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_101_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        res1 = await collector.collect_company(mock_company_source, mock_company)
        res2 = await collector.collect_company(mock_company_source, mock_company)

        assert res1.total_discovered == res2.total_discovered == 1
        assert res1.total_ingested == res2.total_ingested == 1

        env1: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args_list[0].args[0]
        env2: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args_list[1].args[0]

        assert env1.content_hash == env2.content_hash
        assert env1.source_job_id == env2.source_job_id == "101"
        assert env1.raw_body == env2.raw_body == raw_101_bytes


# ============================================================================
# 3. Polite HTTP Client Retries & Rate-Limit Backoff Tests
# ============================================================================


@pytest.mark.asyncio
async def test_polite_http_client_retries_transient_500() -> None:
    """Verify PoliteHttpClient retries on 500 and succeeds on subsequent 200."""
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(500, content=b"Internal Server Error")
        return httpx.Response(200, content=b'{"ok": true}')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as raw_client:
        client = PoliteHttpClient(client=raw_client, max_retries=3, backoff_factor=0.01)
        resp = await client.get("https://example.com/test")
        assert resp.status_code == 200
        assert attempts == 3


@pytest.mark.asyncio
async def test_polite_http_client_honors_retry_after() -> None:
    """Verify PoliteHttpClient parses Retry-After header on 429 response."""
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.02"}, content=b"Rate Limited")
        return httpx.Response(200, content=b'{"ok": true}')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as raw_client:
        client = PoliteHttpClient(client=raw_client, max_retries=2, backoff_factor=0.01)
        resp = await client.get("https://example.com/test")
        assert resp.status_code == 200
        assert attempts == 2


@pytest.mark.asyncio
async def test_no_fabricated_dates_salaries_or_experience(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify collector never fabricates salary, experience, or dates when missing from Greenhouse."""
    raw_listing_bytes = json.dumps({"jobs": [{"id": 303}]}).encode("utf-8")
    detail_without_dates_or_comp = {
        "id": 303,
        "title": "Data Analyst",
        # No updated_at, no location, no salary, no experience
        "content": "<p>Basic job description</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/303",
    }
    raw_detail_bytes = json.dumps(detail_without_dates_or_comp).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]
        assert env.extracted_data is not None
        assert env.extracted_data["title"] == "Data Analyst"
        assert env.extracted_data["location"] is None
        assert env.extracted_data["posted_at"] is None
        assert "salary" not in env.extracted_data
        assert "salary_min" not in env.extracted_data
        assert "experience" not in env.extracted_data


@pytest.mark.asyncio
async def test_missing_company_raises_value_error(
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify collect_company raises ValueError if company cannot be resolved."""
    collector = GreenhouseCollector(mock_ingestion_service)
    # mock_company_source has no company relationship loaded
    with pytest.raises(ValueError, match="Company entity must be provided"):
        await collector.collect_company(mock_company_source, company=None)


@pytest.mark.asyncio
async def test_location_fallback_to_office_when_location_null(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that if job location is null, collector extracts location from offices list."""
    raw_listing_bytes = json.dumps({"jobs": [{"id": 404}]}).encode("utf-8")
    detail_with_office = {
        "id": 404,
        "title": "DevOps Engineer",
        "location": None,
        "content": "<p>DevOps duties</p>",
        "offices": [{"name": "Pune Hub", "location": "Pune, Maharashtra"}],
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/404",
    }
    raw_detail_bytes = json.dumps(detail_with_office).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]
        assert env.extracted_data is not None
        assert env.extracted_data["location"] == "Pune, Maharashtra"


# ============================================================================
# 4. Publication-Date Mapping Regression Tests (first_published vs updated_at)
# ============================================================================


@pytest.mark.asyncio
async def test_first_published_and_updated_at_differ_derives_from_first_published(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that when first_published and updated_at differ, publication date derives strictly from first_published."""
    first_pub_str = "2026-02-10T09:30:00Z"
    updated_str = "2026-03-01T15:00:00Z"

    raw_listing_bytes = json.dumps({"jobs": [{"id": 501}]}).encode("utf-8")
    job_detail = {
        "id": 501,
        "title": "Senior AI Architect",
        "first_published": first_pub_str,
        "updated_at": updated_str,
        "content": "<p>Designing high-scale AI systems.</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/501",
        "location": {"name": "Bengaluru"},
    }
    raw_detail_bytes = json.dumps(job_detail).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]
        assert env.extracted_data is not None

        # 1. Extracted posted_at matches first_published, NOT updated_at
        assert env.extracted_data["posted_at"] == first_pub_str
        assert env.extracted_data["posted_at"] != updated_str

        # 2. Raw evidence preserves both fields unaltered
        assert env.raw_payload["first_published"] == first_pub_str
        assert env.raw_payload["updated_at"] == updated_str

        # 3. Normalization derives from first_published
        norm_service = JobNormalizationService()
        norm_job = norm_service.normalize(env.extracted_data)
        assert norm_job.posted_at == datetime(2026, 2, 10, 9, 30, 0, tzinfo=UTC)
        assert norm_job.posted_at != datetime(2026, 3, 1, 15, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_missing_first_published_remains_missing_even_with_updated_at(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that a missing first_published remains None, never falling back to updated_at or collected_at."""
    updated_str = "2026-03-01T15:00:00Z"

    raw_listing_bytes = json.dumps({"jobs": [{"id": 502}]}).encode("utf-8")
    job_detail = {
        "id": 502,
        "title": "ML Engineer",
        "first_published": None,  # EXPLICITLY MISSING / NULL
        "updated_at": updated_str,
        "content": "<p>ML modeling and deployment.</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/502",
        "location": {"name": "Hyderabad"},
    }
    raw_detail_bytes = json.dumps(job_detail).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]
        assert env.extracted_data is not None

        # 1. Extracted posted_at is None (unresolved), NOT updated_at or collected_at
        assert env.extracted_data["posted_at"] is None

        # 2. Raw evidence preserves updated_at as-is
        assert env.raw_payload["updated_at"] == updated_str
        assert env.raw_payload["first_published"] is None

        # 3. Normalization results in None posted_at
        norm_service = JobNormalizationService()
        norm_job = norm_service.normalize(env.extracted_data)
        assert norm_job.posted_at is None


@pytest.mark.asyncio
async def test_invalid_first_published_does_not_fall_back_to_updated_at(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify that an unparseable first_published string does not fall back to updated_at."""
    updated_str = "2026-03-01T15:00:00Z"

    raw_listing_bytes = json.dumps({"jobs": [{"id": 503}]}).encode("utf-8")
    job_detail = {
        "id": 503,
        "title": "Platform Engineer",
        "first_published": "not-a-valid-timestamp-value",  # MALFORMED DATE
        "updated_at": updated_str,
        "content": "<p>Kubernetes infrastructure.</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/503",
        "location": {"name": "Pune"},
    }
    raw_detail_bytes = json.dumps(job_detail).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]
        assert env.extracted_data is not None

        # 1. Extracted posted_at is None (left unresolved rather than falling back)
        assert env.extracted_data["posted_at"] is None

        # 2. Raw evidence preserves original malformed string for auditability
        assert env.raw_payload["first_published"] == "not-a-valid-timestamp-value"
        assert env.raw_payload["updated_at"] == updated_str

        # 3. Normalization safely produces None posted_at
        norm_service = JobNormalizationService()
        norm_job = norm_service.normalize(env.extracted_data)
        assert norm_job.posted_at is None


@pytest.mark.asyncio
async def test_original_response_bytes_and_source_evidence_remain_unchanged(
    mock_company: Company,
    mock_company_source: CompanySource,
    mock_ingestion_service: MagicMock,
) -> None:
    """Verify raw response bytes and parsed JSON correspond exactly to each other with matching content_hash."""
    job_detail_dict = {
        "id": 504,
        "title": "Fullstack Engineer &amp; Designer",
        "first_published": "2026-02-14T12:00:00Z",
        "updated_at": "2026-03-01T18:00:00Z",
        "content": "<p>High-fidelity byte test: &lt;tag&gt; \\u2014</p>",
        "absolute_url": "https://boards.greenhouse.io/acme/jobs/504",
        "location": {"name": "Mumbai"},
    }
    raw_detail_bytes = json.dumps(job_detail_dict, indent=2).encode("utf-8")
    raw_listing_bytes = json.dumps({"jobs": [{"id": 504}]}).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        if "jobs?content=true" in str(request.url):
            return httpx.Response(200, content=raw_listing_bytes)
        return httpx.Response(200, content=raw_detail_bytes)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GreenhouseCollector(
            mock_ingestion_service, http_client=PoliteHttpClient(client=client)
        )
        result = await collector.collect_company(mock_company_source, mock_company)
        assert result.status == STATUS_SUCCESS

        env: JobIngestionEnvelope = mock_ingestion_service.ingest.call_args.args[0]

        # Assert byte-for-byte exact equality
        assert env.raw_body == raw_detail_bytes
        assert env.content_hash == hashlib.sha256(raw_detail_bytes).hexdigest()

        # Assert parsed payload matches exact structure
        assert env.raw_payload == job_detail_dict
        assert env.raw_payload["first_published"] == "2026-02-14T12:00:00Z"
        assert env.raw_payload["updated_at"] == "2026-03-01T18:00:00Z"
