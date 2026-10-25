"""Live PostgreSQL integration tests for GreenhouseCollector.

Boundary: End-to-end integration tests using PostgreSQL 18 with mocked HTTP responses.
Validates:
1. End-to-end collection and ingestion lifecycle from public Greenhouse API responses
   into PostgreSQL persistence models (RawJob, Job, JobSourceReference, VerificationRecord, FilterDecision).
2. Exact wire byte preservation and cryptographic hash round-trip.
3. Transaction rollback reverts all collector-initiated writes without leaving orphaned records.
4. Collector operates strictly within caller transaction boundary (never calls commit).
"""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.collectors.base.collector import STATUS_SUCCESS
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import (
    GREENHOUSE_API_BASE,
    GreenhouseCollector,
)
from app.orchestration.ingestion_service import JobIngestionService
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import (
    CompanySourceRepository,
)
from app.persistence.repositories.filter_decision_repository import (
    FilterDecisionRepository,
)
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.persistence.session import dispose_engine, get_sessionmaker

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

SAMPLE_GH_LISTING = {
    "jobs": [
        {
            "id": 9001,
            "title": "Junior Python Developer",
            "updated_at": "2026-03-01T10:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/acme-integ/jobs/9001",
            "location": {"name": "Bengaluru, Karnataka"},
        }
    ],
    "meta": {"total": 1},
}

SAMPLE_GH_DETAIL = {
    "id": 9001,
    "title": "Junior Python Developer",
    "first_published": "2026-02-15T10:00:00Z",
    "updated_at": "2026-03-01T10:00:00Z",
    "content": (
        "<p>We are looking for a Junior Python Developer in Bengaluru to join our engineering team. "
        "Key requirements include Python 3, FastAPI, PostgreSQL, and building scalable backend services. "
        "Mentorship and growth opportunities provided.</p>"
    ),
    "absolute_url": "https://boards.greenhouse.io/acme-integ/jobs/9001",
    "location": {"name": "Bengaluru, Karnataka"},
    "departments": [{"id": 10, "name": "Engineering"}],
    "offices": [{"id": 20, "name": "Bengaluru Office", "location": "Bengaluru"}],
}


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_greenhouse_collector_end_to_end_integration() -> None:
    """Verify GreenhouseCollector executes full pipeline ingestion into PostgreSQL 18."""
    unique_suffix = uuid.uuid4().hex[:8]
    board_token = f"acme-{unique_suffix}"
    session_factory = get_sessionmaker()

    raw_listing_bytes = json.dumps(SAMPLE_GH_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_GH_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/9001":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)
            verif_repo = VerificationRecordRepository(session)
            decision_repo = FilterDecisionRepository(session)

            # 1. Seed Company, Source, and CompanySource
            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Integ Corp {unique_suffix}",
                    normalized_name=f"integ-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"GH_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=board_token,
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            # 2. Initialize Ingestion Service and Greenhouse Collector
            ingestion_service = JobIngestionService.from_session(session)
            async with httpx.AsyncClient(transport=transport) as raw_client:
                http_client = PoliteHttpClient(client=raw_client)
                collector = GreenhouseCollector(ingestion_service, http_client=http_client)

                # 3. Collect Company
                result = await collector.collect_company(cs, company)

            # 4. Assert Collector Result
            assert result.status == STATUS_SUCCESS
            assert result.is_success is True
            assert result.total_discovered == 1
            assert result.total_ingested == 1
            assert result.total_failed == 0
            assert len(result.ingestion_outcomes) == 1

            outcome = result.ingestion_outcomes[0]
            assert outcome.status == "PROCESSED_NEW"
            assert outcome.canonical_job is not None
            assert outcome.raw_job is not None
            assert outcome.source_reference is not None

            # 5. Verify PostgreSQL persistence
            persisted_raw = await raw_repo.get_by_id(outcome.raw_job.id)
            assert persisted_raw is not None
            assert bytes(persisted_raw.raw_body) == raw_detail_bytes
            assert persisted_raw.content_hash == hashlib.sha256(raw_detail_bytes).hexdigest()
            assert persisted_raw.source_job_id == "9001"
            assert persisted_raw.source_job_id_type == "NATIVE"

            persisted_job = await job_repo.get_by_id(outcome.canonical_job.id)
            assert persisted_job is not None
            assert persisted_job.title == "Junior Python Developer"
            assert persisted_job.normalized_title == "junior python developer"
            assert persisted_job.normalized_location == "Bengaluru"
            assert persisted_job.posted_at == datetime(2026, 2, 15, 10, 0, 0, tzinfo=UTC)
            assert persisted_raw.raw_payload["updated_at"] == "2026-03-01T10:00:00Z"
            assert persisted_raw.raw_payload["first_published"] == "2026-02-15T10:00:00Z"

            persisted_jsr = await jsr_repo.get_by_source_external_id(source.id, "9001")
            assert persisted_jsr is not None
            assert persisted_jsr.job_id == persisted_job.id
            assert persisted_jsr.raw_job_id == persisted_raw.id

            persisted_verif = await verif_repo.get_latest_by_job_id(persisted_job.id)
            assert persisted_verif is not None

            persisted_decision = await decision_repo.get_latest_by_job_id(persisted_job.id)
            assert persisted_decision is not None

    finally:
        pass


@pytest.mark.asyncio
async def test_greenhouse_collector_rollback_isolation_integration() -> None:
    """Verify session.rollback() completely reverts all writes performed by GreenhouseCollector."""
    unique_suffix = uuid.uuid4().hex[:8]
    board_token = f"rb-{unique_suffix}"
    session_factory = get_sessionmaker()

    raw_listing_bytes = json.dumps(SAMPLE_GH_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_GH_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/9001":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        source_repo = SourceRepository(session)
        cs_repo = CompanySourceRepository(session)

        company = await company_repo.create(
            Company(
                id=uuid.uuid4(),
                name=f"Rollback Corp {unique_suffix}",
                normalized_name=f"rollback-corp-{unique_suffix}",
            )
        )
        source = await source_repo.create(
            Source(
                id=uuid.uuid4(),
                source_name=f"RB_SRC_{unique_suffix.upper()}",
                source_type="ATS_API",
            )
        )
        cs = await cs_repo.create(
            CompanySource(
                id=uuid.uuid4(),
                company_id=company.id,
                source_id=source.id,
                ats_identifier=board_token,
                discovery_method="AUTOMATED_PROBE",
            )
        )

        ingestion_service = JobIngestionService.from_session(session)
        async with httpx.AsyncClient(transport=transport) as raw_client:
            http_client = PoliteHttpClient(client=raw_client)
            collector = GreenhouseCollector(ingestion_service, http_client=http_client)
            result = await collector.collect_company(cs, company)

        assert result.status == STATUS_SUCCESS
        raw_job_id = result.ingestion_outcomes[0].raw_job.id
        canonical_job_id = result.ingestion_outcomes[0].canonical_job.id

        # Roll back transaction
        await session.rollback()

    # In a fresh session, verify 0 rows exist
    async with session_factory() as fresh_session:
        raw_repo = RawJobRepository(fresh_session)
        job_repo = JobRepository(fresh_session)
        assert await raw_repo.get_by_id(raw_job_id) is None
        assert await job_repo.get_by_id(canonical_job_id) is None
