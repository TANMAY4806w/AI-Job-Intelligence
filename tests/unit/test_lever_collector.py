"""Unit tests for LeverCollector.

Boundary: 100% isolated unit tests with zero external network access.
All HTTP responses and repository sessions are mocked.
"""

import hashlib
import json
import uuid
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
from app.collectors.lever.collector import (
    LEVER_API_BASE,
    LeverCollector,
    validate_board_token,
)
from app.orchestration.ingestion_service import (
    JobIngestionEnvelope,
    JobIngestionOutcome,
    JobIngestionService,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source

SAMPLE_LEVER_LISTING = [
    {
        "id": "lever-job-1",
        "text": "Senior Python Backend Engineer",
        "createdAt": 1708000000000,
        "hostedUrl": "https://jobs.lever.co/acme/lever-job-1",
        "applyUrl": "https://jobs.lever.co/acme/lever-job-1/apply",
        "categories": {
            "commitment": "Full time",
            "department": "Engineering",
            "location": "Bengaluru, India",
            "team": "Backend",
        },
        "description": "<p>Build scalable Python services using FastAPI.</p>",
        "workplaceType": "hybrid",
        "country": "India",
    },
    {
        "id": "lever-job-2",
        "text": "Machine Learning Platform Engineer",
        "createdAt": 1708100000000,
        "hostedUrl": "https://jobs.lever.co/acme/lever-job-2",
        "applyUrl": "https://jobs.lever.co/acme/lever-job-2/apply",
        "categories": {
            "commitment": "Full time",
            "department": "AI",
            "location": "San Francisco, CA",
            "team": "Platform",
        },
        "description": "<p>Scale distributed model training pipelines.</p>",
        "workplaceType": "remote",
        "country": "US",
    },
]

SAMPLE_LEVER_DETAIL_1 = {
    "id": "lever-job-1",
    "text": "Senior Python Backend Engineer",
    "createdAt": 1708000000000,
    "hostedUrl": "https://jobs.lever.co/acme/lever-job-1",
    "applyUrl": "https://jobs.lever.co/acme/lever-job-1/apply",
    "categories": {
        "commitment": "Full time",
        "department": "Engineering",
        "location": "Bengaluru, India",
        "team": "Backend",
    },
    "description": "<p>Build scalable Python services using FastAPI.</p>",
    "workplaceType": "hybrid",
    "country": "India",
}

SAMPLE_LEVER_DETAIL_2 = {
    "id": "lever-job-2",
    "text": "Machine Learning Platform Engineer",
    "createdAt": 1708100000000,
    "hostedUrl": "https://jobs.lever.co/acme/lever-job-2",
    "applyUrl": "https://jobs.lever.co/acme/lever-job-2/apply",
    "categories": {
        "commitment": "Full time",
        "department": "AI",
        "location": "San Francisco, CA",
        "team": "Platform",
    },
    "description": "<p>Scale distributed model training pipelines.</p>",
    "workplaceType": "remote",
    "country": "US",
}


def _build_test_entities():
    company = Company(
        id=uuid.uuid4(),
        name="Acme Corp",
        normalized_name="acme_corp",
        website="https://acme.example.com",
        is_verified=True,
    )
    source = Source(
        id=uuid.uuid4(),
        source_name="Lever",
        source_type="ATS",
        base_url="https://api.lever.co/v0/postings",
        is_active=True,
    )
    company_source = CompanySource(
        id=uuid.uuid4(),
        company_id=company.id,
        source_id=source.id,
        ats_identifier="acme",
        discovery_method="MANUAL_REGISTRY",
        is_active=True,
    )
    company_source.company = company
    company_source.source = source
    return company, source, company_source


# =========================================================================
# Token Validation Tests
# =========================================================================


def test_validate_board_token_valid():
    assert validate_board_token("palantir") == "palantir"
    assert validate_board_token("spotify_global") == "spotify_global"
    assert validate_board_token("acme-tech-1") == "acme-tech-1"


def test_validate_board_token_invalid():
    with pytest.raises(ValueError, match="non-empty string"):
        validate_board_token("")
    with pytest.raises(ValueError, match="blank or whitespace-only"):
        validate_board_token("   ")
    with pytest.raises(ValueError, match="Invalid Lever board token"):
        validate_board_token("acme/jobs")
    with pytest.raises(ValueError, match="exceeds maximum length"):
        validate_board_token("a" * 129)


# =========================================================================
# Lever Collection Tests
# =========================================================================


@pytest.mark.asyncio
async def test_lever_collect_company_successful():
    company, source, company_source = _build_test_entities()

    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    mock_http = MagicMock(spec=PoliteHttpClient)

    async def mock_get(url, *args, **kwargs):
        if url.startswith(f"{LEVER_API_BASE}/acme") and "lever-job" not in url:
            return httpx.Response(200, json=SAMPLE_LEVER_LISTING, request=httpx.Request("GET", url))
        elif "lever-job-1" in url:
            return httpx.Response(
                200, json=SAMPLE_LEVER_DETAIL_1, request=httpx.Request("GET", url)
            )
        elif "lever-job-2" in url:
            return httpx.Response(
                200, json=SAMPLE_LEVER_DETAIL_2, request=httpx.Request("GET", url)
            )
        return httpx.Response(404, request=httpx.Request("GET", url))

    mock_http.get = AsyncMock(side_effect=mock_get)

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_SUCCESS
    assert result.total_discovered == 2
    assert result.total_ingested == 2
    assert result.total_failed == 0
    assert len(result.ingestion_outcomes) == 2
    assert mock_ingestion.ingest.call_count == 2

    # Verify envelope passed to ingest
    call_args_list = mock_ingestion.ingest.call_args_list
    env_1: JobIngestionEnvelope = call_args_list[0][0][0]
    assert env_1.source_job_id == "lever-job-1"
    assert env_1.company_id == company.id
    assert env_1.source_id == source.id
    assert env_1.application_url == "https://jobs.lever.co/acme/lever-job-1/apply"
    assert env_1.verification_evidence.location == "Bengaluru, India"


@pytest.mark.asyncio
async def test_lever_collect_company_empty_board():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json=[],
            request=httpx.Request("GET", "https://api.lever.co/v0/postings/acme?mode=json"),
        )
    )

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_EMPTY
    assert result.total_discovered == 0
    assert result.total_ingested == 0
    assert result.total_failed == 0
    mock_ingestion.ingest.assert_not_called()


@pytest.mark.asyncio
async def test_lever_collect_company_http_error():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            404,
            text="Not Found",
            request=httpx.Request("GET", "https://api.lever.co/v0/postings/acme?mode=json"),
        )
    )

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert result.total_discovered == 0
    assert "returned HTTP 404" in result.error_message


@pytest.mark.asyncio
async def test_lever_collect_company_malformed_json():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            text="<html>Invalid JSON</html>",
            request=httpx.Request("GET", "https://api.lever.co/v0/postings/acme?mode=json"),
        )
    )

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert "Malformed JSON" in result.error_message


@pytest.mark.asyncio
async def test_lever_collect_company_partial_failure():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)
    mock_http = MagicMock(spec=PoliteHttpClient)

    async def mock_get(url, *args, **kwargs):
        if url.startswith(f"{LEVER_API_BASE}/acme") and "lever-job" not in url:
            return httpx.Response(200, json=SAMPLE_LEVER_LISTING, request=httpx.Request("GET", url))
        elif "lever-job-1" in url:
            return httpx.Response(
                200, json=SAMPLE_LEVER_DETAIL_1, request=httpx.Request("GET", url)
            )
        elif "lever-job-2" in url:
            # Job 2 detail returns server error
            return httpx.Response(500, text="Internal Error", request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    mock_http.get = AsyncMock(side_effect=mock_get)

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_PARTIAL_SUCCESS
    assert result.total_discovered == 2
    assert result.total_ingested == 1
    assert result.total_failed == 1
    assert len(result.failures) == 1
    assert result.failures[0].error_type == "HTTP_ERROR"


@pytest.mark.asyncio
async def test_lever_collect_company_missing_title():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)

    invalid_job = {"id": "job-no-title", "text": "   "}
    resp_listing = httpx.Response(
        200,
        json=[invalid_job],
        request=httpx.Request("GET", "https://api.lever.co/v0/postings/acme?mode=json"),
    )
    resp_detail = httpx.Response(
        200,
        json=invalid_job,
        request=httpx.Request(
            "GET", "https://api.lever.co/v0/postings/acme/job-no-title?mode=json"
        ),
    )
    mock_http.get = AsyncMock(side_effect=[resp_listing, resp_detail])

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert result.total_discovered == 1
    assert result.total_ingested == 0
    assert result.total_failed == 1
    assert result.failures[0].error_type == "INVALID_DATA"
    assert "missing required non-empty 'text'" in result.failures[0].error_message


@pytest.mark.asyncio
async def test_lever_collect_company_raw_body_integrity():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)

    detail_bytes = json.dumps(SAMPLE_LEVER_DETAIL_1).encode("utf-8")
    resp_listing = httpx.Response(
        200, json=[SAMPLE_LEVER_LISTING[0]], request=httpx.Request("GET", "url")
    )
    resp_detail = httpx.Response(
        200,
        content=detail_bytes,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "detail_url"),
    )

    mock_http.get = AsyncMock(side_effect=[resp_listing, resp_detail])

    collector = LeverCollector(mock_ingestion, http_client=mock_http)
    await collector.collect_company(company_source)

    env: JobIngestionEnvelope = mock_ingestion.ingest.call_args[0][0]
    assert env.raw_body == detail_bytes
    assert env.content_hash == hashlib.sha256(detail_bytes).hexdigest()
    assert env.raw_payload == SAMPLE_LEVER_DETAIL_1


@pytest.mark.asyncio
async def test_lever_collect_company_pagination_multi_page():
    """Verify skip/limit pagination retrieves all pages and prevents duplicates across pages."""
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    job_p1_1 = {"id": "page-job-1", "text": "Engineer 1"}
    job_p1_2 = {"id": "page-job-2", "text": "Engineer 2"}
    job_p2_dup = {"id": "page-job-2", "text": "Engineer 2 Duplicate"}
    job_p2_3 = {"id": "page-job-3", "text": "Engineer 3"}

    mock_http = MagicMock(spec=PoliteHttpClient)

    async def mock_get(url, *args, **kwargs):
        if "skip=0" in url:
            return httpx.Response(200, json=[job_p1_1, job_p1_2], request=httpx.Request("GET", url))
        elif "skip=2" in url:
            # Page 2 contains a duplicate from page 1 and a new job
            return httpx.Response(
                200, json=[job_p2_dup, job_p2_3], request=httpx.Request("GET", url)
            )
        elif "skip=4" in url:
            # Terminal empty page indicating no further jobs
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))
        elif "page-job-1" in url:
            return httpx.Response(200, json=job_p1_1, request=httpx.Request("GET", url))
        elif "page-job-2" in url:
            return httpx.Response(200, json=job_p1_2, request=httpx.Request("GET", url))
        elif "page-job-3" in url:
            return httpx.Response(200, json=job_p2_3, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    mock_http.get = AsyncMock(side_effect=mock_get)

    collector = LeverCollector(mock_ingestion, http_client=mock_http, page_size=2)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_SUCCESS
    assert result.total_discovered == 3  # Deduped across pages (job-1, job-2, job-3)
    assert result.total_ingested == 3
    assert result.total_failed == 0
    assert mock_ingestion.ingest.call_count == 3
    ingested_ids = [call[0][0].source_job_id for call in mock_ingestion.ingest.call_args_list]
    assert ingested_ids == ["page-job-1", "page-job-2", "page-job-3"]


@pytest.mark.asyncio
async def test_lever_collect_company_pagination_max_pages_truncated():
    """Verify reaching max_pages with full page yields PARTIAL_SUCCESS and TRUNCATED_HARVEST failure."""
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    # With max_pages=2 and page_size=2:
    # Page 1 (skip=0): 2 items
    # Page 2 (skip=2): 2 items (full page -> additional jobs may exist)
    job_p1_1 = {"id": "page-job-1", "text": "Engineer 1"}
    job_p1_2 = {"id": "page-job-2", "text": "Engineer 2"}
    job_p2_1 = {"id": "page-job-3", "text": "Engineer 3"}
    job_p2_2 = {"id": "page-job-4", "text": "Engineer 4"}

    mock_http = MagicMock(spec=PoliteHttpClient)

    async def mock_get(url, *args, **kwargs):
        if "skip=0" in url:
            return httpx.Response(200, json=[job_p1_1, job_p1_2], request=httpx.Request("GET", url))
        elif "skip=2" in url:
            return httpx.Response(200, json=[job_p2_1, job_p2_2], request=httpx.Request("GET", url))
        elif "page-job-" in url:
            jid = url.split("/")[-1].split("?")[0]
            return httpx.Response(
                200, json={"id": jid, "text": f"Title {jid}"}, request=httpx.Request("GET", url)
            )
        return httpx.Response(404, request=httpx.Request("GET", url))

    mock_http.get = AsyncMock(side_effect=mock_get)

    collector = LeverCollector(mock_ingestion, http_client=mock_http, page_size=2, max_pages=2)
    result = await collector.collect_company(company_source)

    # Must NOT report complete success when truncated
    assert result.status == STATUS_PARTIAL_SUCCESS
    assert result.total_discovered == 4
    assert result.total_ingested == 4
    assert result.total_failed >= 1
    assert result.error_message is not None
    assert "truncated at maximum page limit" in result.error_message

    # Check diagnostic failure record
    trunc_failures = [f for f in result.failures if f.error_type == "TRUNCATED_HARVEST"]
    assert len(trunc_failures) == 1
    assert "truncated at max pages limit (2 pages, 4 jobs)" in trunc_failures[0].error_message
