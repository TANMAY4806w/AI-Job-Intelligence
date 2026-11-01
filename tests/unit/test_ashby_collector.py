"""Unit tests for AshbyCollector.

Boundary: 100% isolated unit tests with zero external network access.
All HTTP responses and repository sessions are mocked.
"""

import hashlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.collectors.ashby.collector import (
    ASHBY_API_BASE,
    AshbyCollector,
    validate_board_token,
)
from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.orchestration.ingestion_service import (
    JobIngestionEnvelope,
    JobIngestionOutcome,
    JobIngestionService,
)
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source

SAMPLE_ASHBY_RESPONSE = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "ashby-job-1",
            "title": "Staff Backend Engineer, Python",
            "department": "Engineering",
            "team": "Core",
            "location": "San Francisco, CA",
            "isListed": True,
            "isRemote": False,
            "workplaceType": "Hybrid",
            "descriptionHtml": "<p>Design high-throughput distributed systems in Python and Go.</p>",
            "publishedAt": "2026-03-01T15:30:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-job-1",
            "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-job-1/apply",
            "address": {
                "postalAddress": {
                    "addressLocality": "San Francisco",
                    "addressRegion": "CA",
                    "addressCountry": "USA",
                }
            },
        },
        {
            "id": "ashby-job-2",
            "title": "AI Research Scientist - GenAI",
            "department": "Research",
            "team": "Foundation Models",
            "location": None,
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Remote",
            "descriptionHtml": "<p>Advance multimodal LLM pre-training and alignment algorithms.</p>",
            "publishedAt": "2026-03-02T10:00:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-job-2",
            "applyUrl": "https://jobs.ashbyhq.com/acme/ashby-job-2/apply",
            "address": {
                "postalAddress": {
                    "addressLocality": "Bengaluru",
                    "addressRegion": "Karnataka",
                    "addressCountry": "India",
                }
            },
        },
    ],
}


def _build_test_entities():
    company = Company(
        id=uuid.uuid4(),
        name="Acme AI",
        normalized_name="acme_ai",
        website="https://acme.ai",
        is_verified=True,
    )
    source = Source(
        id=uuid.uuid4(),
        source_name="Ashby",
        source_type="ATS",
        base_url="https://api.ashbyhq.com/posting-api/job-board",
        is_active=True,
    )
    company_source = CompanySource(
        id=uuid.uuid4(),
        company_id=company.id,
        source_id=source.id,
        ats_identifier="acme-ai",
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
    assert validate_board_token("openai") == "openai"
    assert validate_board_token("ramp_global") == "ramp_global"
    assert validate_board_token("linear-app") == "linear-app"


def test_validate_board_token_invalid():
    with pytest.raises(ValueError, match="non-empty string"):
        validate_board_token("")
    with pytest.raises(ValueError, match="blank or whitespace-only"):
        validate_board_token("   ")
    with pytest.raises(ValueError, match="Invalid Ashby board token"):
        validate_board_token("openai/jobs")
    with pytest.raises(ValueError, match="exceeds maximum length"):
        validate_board_token("a" * 129)


# =========================================================================
# Ashby Collection Tests
# =========================================================================


@pytest.mark.asyncio
async def test_ashby_collect_company_successful():
    company, source, company_source = _build_test_entities()

    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json=SAMPLE_ASHBY_RESPONSE,
            request=httpx.Request("GET", f"{ASHBY_API_BASE}/acme-ai?includeCompensation=true"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_SUCCESS
    assert result.total_discovered == 2
    assert result.total_ingested == 2
    assert result.total_failed == 0
    assert len(result.ingestion_outcomes) == 2
    assert mock_ingestion.ingest.call_count == 2

    # Verify first envelope
    call_args_list = mock_ingestion.ingest.call_args_list
    env_1: JobIngestionEnvelope = call_args_list[0][0][0]
    assert env_1.source_job_id == "ashby-job-1"
    assert env_1.company_id == company.id
    assert env_1.source_id == source.id
    assert env_1.application_url == "https://jobs.ashbyhq.com/acme/ashby-job-1/apply"
    assert env_1.verification_evidence.location == "San Francisco, CA"

    # Verify second envelope (address fallback)
    env_2: JobIngestionEnvelope = call_args_list[1][0][0]
    assert env_2.source_job_id == "ashby-job-2"
    assert env_2.verification_evidence.location == "Bengaluru, Karnataka, India"


@pytest.mark.asyncio
async def test_ashby_collect_company_empty_board():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": []},
            request=httpx.Request("GET", f"{ASHBY_API_BASE}/acme-ai?includeCompensation=true"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_EMPTY
    assert result.total_discovered == 0
    assert result.total_ingested == 0
    assert result.total_failed == 0
    mock_ingestion.ingest.assert_not_called()


@pytest.mark.asyncio
async def test_ashby_collect_company_http_error():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            404,
            text="Not Found",
            request=httpx.Request("GET", f"{ASHBY_API_BASE}/acme-ai?includeCompensation=true"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert result.total_discovered == 0
    assert "returned HTTP 404" in result.error_message


@pytest.mark.asyncio
async def test_ashby_collect_company_missing_jobs_key():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1"},  # Missing 'jobs' key
            request=httpx.Request("GET", f"{ASHBY_API_BASE}/acme-ai?includeCompensation=true"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert "missing top-level 'jobs' list" in result.error_message


@pytest.mark.asyncio
async def test_ashby_collect_company_missing_title():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)

    invalid_job = {"id": "job-missing-title", "title": "   "}
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": [invalid_job]},
            request=httpx.Request("GET", f"{ASHBY_API_BASE}/acme-ai?includeCompensation=true"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_FAILED
    assert result.total_discovered == 1
    assert result.total_ingested == 0
    assert result.total_failed == 1
    assert result.failures[0].error_type == "INVALID_DATA"
    assert "missing required non-empty 'title'" in result.failures[0].error_message


@pytest.mark.asyncio
async def test_ashby_collect_company_raw_body_integrity():
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_http = MagicMock(spec=PoliteHttpClient)

    job_data = SAMPLE_ASHBY_RESPONSE["jobs"][0]
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": [job_data]},
            request=httpx.Request("GET", "url"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    await collector.collect_company(company_source)

    env: JobIngestionEnvelope = mock_ingestion.ingest.call_args[0][0]
    expected_bytes = json.dumps(job_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert env.raw_body == expected_bytes
    assert env.content_hash == hashlib.sha256(expected_bytes).hexdigest()
    assert env.raw_payload == job_data
    assert "board_wire_hash" in env.extracted_data
    assert env.extracted_data["provenance_mode"] == "ASHBY_OBJECT_SERIALIZED"


@pytest.mark.asyncio
async def test_ashby_unlisted_jobs_are_filtered():
    """Verify that unlisted jobs (isListed=false) cannot enter public feed as ordinary listed jobs."""
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    listed_job = {
        "id": "listed-job-1",
        "title": "Public Engineer",
        "isListed": True,
        "descriptionHtml": "<p>Public job</p>",
    }
    unlisted_job = {
        "id": "unlisted-job-2",
        "title": "Confidential Executive",
        "isListed": False,
        "descriptionHtml": "<p>Private job</p>",
    }

    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": [listed_job, unlisted_job]},
            request=httpx.Request("GET", "https://api.ashbyhq.com/posting-api/job-board/acme-ai"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    result = await collector.collect_company(company_source)

    assert result.status == STATUS_SUCCESS
    assert mock_ingestion.ingest.call_count == 1
    ingested_id = mock_ingestion.ingest.call_args[0][0].source_job_id
    assert ingested_id == "listed-job-1"


@pytest.mark.asyncio
async def test_ashby_workplace_type_and_remote_classification():
    """Verify workplaceType correctly distinguishes OnSite, Remote and Hybrid (isRemote alone is not remote)."""
    company, source, company_source = _build_test_entities()
    mock_ingestion = AsyncMock(spec=JobIngestionService)
    mock_outcome = MagicMock(spec=JobIngestionOutcome)
    mock_ingestion.ingest = AsyncMock(return_value=mock_outcome)

    jobs = [
        # Hybrid despite isRemote=True
        {
            "id": "job-hybrid",
            "title": "Hybrid Engineer",
            "isListed": True,
            "workplaceType": "Hybrid",
            "isRemote": True,
            "descriptionHtml": "<p>Hybrid job</p>",
        },
        # OnSite
        {
            "id": "job-onsite",
            "title": "Onsite Engineer",
            "isListed": True,
            "workplaceType": "OnSite",
            "isRemote": False,
            "descriptionHtml": "<p>Onsite job</p>",
        },
        # Remote via isRemote fallback
        {
            "id": "job-remote-flag",
            "title": "Remote Engineer",
            "isListed": True,
            "isRemote": True,
            "descriptionHtml": "<p>Remote job</p>",
        },
        # Missing remains missing
        {
            "id": "job-missing-wp",
            "title": "Unknown Engineer",
            "isListed": True,
            "descriptionHtml": "<p>Unknown job</p>",
        },
    ]

    mock_http = MagicMock(spec=PoliteHttpClient)
    mock_http.get = AsyncMock(
        return_value=httpx.Response(
            200,
            json={"apiVersion": "1", "jobs": jobs},
            request=httpx.Request("GET", "https://api.ashbyhq.com/posting-api/job-board/acme-ai"),
        )
    )

    collector = AshbyCollector(mock_ingestion, http_client=mock_http)
    await collector.collect_company(company_source)

    assert mock_ingestion.ingest.call_count == 4
    envelopes = [call[0][0] for call in mock_ingestion.ingest.call_args_list]

    # Job 1: Hybrid wins over isRemote=True
    assert envelopes[0].extracted_data["remote_type"] == "HYBRID"
    assert envelopes[0].extracted_data["workplace_type"] == "Hybrid"

    # Job 2: OnSite
    assert envelopes[1].extracted_data["remote_type"] == "ON_SITE"

    # Job 3: Remote flag fallback
    assert envelopes[2].extracted_data["remote_type"] == "REMOTE"

    # Job 4: Missing remains missing
    assert envelopes[3].extracted_data["remote_type"] is None
    assert envelopes[3].extracted_data["workplace_type"] is None
