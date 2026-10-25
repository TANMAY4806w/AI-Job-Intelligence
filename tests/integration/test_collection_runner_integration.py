"""Live PostgreSQL integration tests for CollectionRunner.

Boundary: End-to-end integration tests using PostgreSQL 18 with mocked HTTP responses.
Validates:
1. A selected Greenhouse mapping flowing through CollectionRunner and JobIngestionService
   into PostgreSQL persistence models (RawJob, Job, JobSourceReference, VerificationRecord,
   FilterDecision, and CollectionRun).
2. CollectionRun telemetry, counts, and status persistence in PostgreSQL.
3. Multiple selected mappings flowing through run_mappings() with metric aggregation and error isolation.
4. Transaction rollback completely reverts all runner-initiated writes without leaving orphaned records.
5. Runner and repositories strictly operate within the caller's transaction boundary without commit().
"""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import GREENHOUSE_API_BASE
from app.orchestration.collection_runner import (
    CollectionRunner,
    CollectionRunnerResult,
    MappingCollectionOutcome,
)
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import RawJob
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.collection_run_repository import CollectionRunRepository
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import (
    CompanySourceRepository,
)
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.session import dispose_engine, get_sessionmaker

pytestmark = pytest.mark.integration

SAMPLE_GH_LISTING = {
    "jobs": [
        {
            "id": 9101,
            "title": "Junior Python Developer",
            "updated_at": "2026-03-01T10:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/acme-runner/jobs/9101",
            "location": {"name": "Bengaluru, Karnataka"},
        }
    ],
    "meta": {"total": 1},
}

SAMPLE_GH_DETAIL = {
    "id": 9101,
    "title": "Junior Python Developer",
    "updated_at": "2026-03-01T10:00:00Z",
    "first_published": "2026-02-28T09:00:00Z",
    "absolute_url": "https://boards.greenhouse.io/acme-runner/jobs/9101",
    "content": "<p>Build high-performance AI services using Python and PostgreSQL.</p>",
    "location": {"name": "Bengaluru, Karnataka"},
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 2, "name": "Bengaluru"}],
}

EMPTY_GH_LISTING = {
    "jobs": [],
    "meta": {"total": 0},
}


@pytest.mark.asyncio
async def test_collection_runner_end_to_end_integration() -> None:
    """Verify CollectionRunner executes full collection and persistence into PostgreSQL 18."""
    unique_suffix = uuid.uuid4().hex[:8]
    board_token = f"acme-{unique_suffix}"
    session_factory = get_sessionmaker()

    raw_listing_bytes = json.dumps(SAMPLE_GH_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_GH_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/9101":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    try:
        async with httpx.AsyncClient(transport=transport) as raw_client:
            http_client = PoliteHttpClient(client=raw_client)

            async with session_factory() as session:
                company_repo = CompanyRepository(session)
                source_repo = SourceRepository(session)
                cs_repo = CompanySourceRepository(session)
                run_repo = CollectionRunRepository(session)

                # 1. Resolve or create GREENHOUSE platform Source
                source = await source_repo.get_by_name("GREENHOUSE")
                if source is None:
                    source = await source_repo.create(
                        Source(
                            id=uuid.uuid4(),
                            source_name="GREENHOUSE",
                            source_type="ATS_API",
                            base_url="https://boards-api.greenhouse.io/v1/boards",
                            is_active=True,
                        )
                    )

                # 2. Seed canonical Company and CompanySource
                company = await company_repo.create(
                    Company(
                        id=uuid.uuid4(),
                        name=f"Acme Runner Corp {unique_suffix}",
                        normalized_name=f"acme-runner-corp-{unique_suffix}",
                    )
                )
                cs = await cs_repo.create(
                    CompanySource(
                        id=uuid.uuid4(),
                        company_id=company.id,
                        source_id=source.id,
                        ats_identifier=board_token,
                        discovery_method="AUTOMATED_PROBE",
                        is_active=True,
                    )
                )

                # 3. Instantiate CollectionRunner from active session
                runner = CollectionRunner.from_session(session, http_client=http_client)

                # 4. Execute collection for the single mapping
                outcome: MappingCollectionOutcome = await runner.run_mapping(
                    cs.id, run_type="MANUAL_TRIGGER"
                )

                # 5. Verify outcome structure
                assert outcome.is_success
                assert outcome.status == STATUS_SUCCESS
                assert outcome.records_discovered == 1
                assert outcome.records_ingested == 1
                assert outcome.records_failed == 0
                assert outcome.duplicates_detected == 0
                # 1 listing query + 1 detail query = 2 x 2xx
                assert outcome.http_2xx_count == 2
                assert outcome.collection_run_id is not None

                # 6. Verify CollectionRun persisted in PostgreSQL
                persisted_run = await run_repo.get_by_id(outcome.collection_run_id)
                assert persisted_run is not None
                assert persisted_run.source_id == source.id
                assert persisted_run.company_source_id == cs.id
                assert persisted_run.run_type == "MANUAL_TRIGGER"
                assert persisted_run.status == "COMPLETED"
                assert persisted_run.records_discovered == 1
                assert persisted_run.records_ingested == 1
                assert persisted_run.records_failed == 0
                assert persisted_run.http_2xx_count == 2
                assert persisted_run.telemetry is not None
                assert persisted_run.telemetry["collector_status"] == STATUS_SUCCESS
                assert persisted_run.telemetry["ats_identifier"] == board_token

                # 7. Verify downstream pipeline records
                # RawJob linked to CollectionRun
                stmt_raw = select(RawJob).where(RawJob.collection_run_id == persisted_run.id)
                res_raw = await session.execute(stmt_raw)
                raw_jobs = res_raw.scalars().all()
                assert len(raw_jobs) == 1
                raw_job = raw_jobs[0]
                assert raw_job.source_job_id == "9101"
                assert raw_job.raw_body == raw_detail_bytes

                # Canonical Job created
                stmt_job = select(Job).where(Job.company_id == company.id)
                res_job = await session.execute(stmt_job)
                jobs = res_job.scalars().all()
                assert len(jobs) == 1
                job = jobs[0]
                assert job.title == "Junior Python Developer"
                assert job.eligibility_status in ("ELIGIBLE", "REVIEW", "REJECTED", "EXPIRED")

                # VerificationRecord and FilterDecision created
                stmt_verif = select(VerificationRecord).where(VerificationRecord.job_id == job.id)
                res_verif = await session.execute(stmt_verif)
                assert len(res_verif.scalars().all()) >= 1

                stmt_dec = select(FilterDecision).where(FilterDecision.job_id == job.id)
                res_dec = await session.execute(stmt_dec)
                assert len(res_dec.scalars().all()) >= 1

                # CompanySource last_collected_at updated
                assert cs.last_collected_at is not None

                # 8. Clean up transaction via rollback
                await session.rollback()

    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_collection_runner_multiple_mappings_integration() -> None:
    """Verify CollectionRunner collects across multiple selected mappings in PostgreSQL 18."""
    unique_suffix = uuid.uuid4().hex[:8]
    board_1 = f"valid-{unique_suffix}"
    board_2 = f"empty-{unique_suffix}"
    session_factory = get_sessionmaker()

    raw_listing_bytes = json.dumps(SAMPLE_GH_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_GH_DETAIL).encode("utf-8")
    empty_listing_bytes = json.dumps(EMPTY_GH_LISTING).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_1}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_1}/jobs/9101":
            return httpx.Response(200, content=raw_detail_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_2}/jobs?content=true":
            return httpx.Response(200, content=empty_listing_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    try:
        async with httpx.AsyncClient(transport=transport) as raw_client:
            http_client = PoliteHttpClient(client=raw_client)

            async with session_factory() as session:
                company_repo = CompanyRepository(session)
                source_repo = SourceRepository(session)
                cs_repo = CompanySourceRepository(session)
                run_repo = CollectionRunRepository(session)

                # 1. Resolve or create Source
                source = await source_repo.get_by_name("GREENHOUSE")
                if source is None:
                    source = await source_repo.create(
                        Source(
                            id=uuid.uuid4(),
                            source_name="GREENHOUSE",
                            source_type="ATS_API",
                            base_url="https://boards-api.greenhouse.io/v1/boards",
                            is_active=True,
                        )
                    )

                # 2. Seed Companies and CompanySources
                company_1 = await company_repo.create(
                    Company(
                        id=uuid.uuid4(),
                        name=f"Company One {unique_suffix}",
                        normalized_name=f"company-one-{unique_suffix}",
                    )
                )
                cs_1 = await cs_repo.create(
                    CompanySource(
                        id=uuid.uuid4(),
                        company_id=company_1.id,
                        source_id=source.id,
                        ats_identifier=board_1,
                        discovery_method="AUTOMATED_PROBE",
                        is_active=True,
                    )
                )

                company_2 = await company_repo.create(
                    Company(
                        id=uuid.uuid4(),
                        name=f"Company Two {unique_suffix}",
                        normalized_name=f"company-two-{unique_suffix}",
                    )
                )
                cs_2 = await cs_repo.create(
                    CompanySource(
                        id=uuid.uuid4(),
                        company_id=company_2.id,
                        source_id=source.id,
                        ats_identifier=board_2,
                        discovery_method="AUTOMATED_PROBE",
                        is_active=True,
                    )
                )

                # 3. Instantiate Runner and run both mappings
                runner = CollectionRunner.from_session(session, http_client=http_client)
                result: CollectionRunnerResult = await runner.run_mappings(
                    [cs_1.id, cs_2.id], run_type="HIGH_FREQUENCY_ATS"
                )

                # 4. Verify aggregated results
                assert result.total_mappings_selected == 2
                assert result.total_mappings_processed == 2
                assert result.total_mappings_succeeded == 2
                assert result.total_mappings_failed == 0
                assert result.records_discovered == 1
                assert result.records_ingested == 1
                assert result.records_failed == 0
                # cs_1 (1 listing + 1 detail = 2) + cs_2 (1 listing = 1) = 3 x 2xx
                assert result.http_2xx_count == 3
                assert len(result.mapping_outcomes) == 2

                # 5. Verify individual mapping outcomes
                outcome_1 = result.mapping_outcomes[0]
                assert outcome_1.company_source_id == cs_1.id
                assert outcome_1.status == STATUS_SUCCESS
                assert outcome_1.records_ingested == 1

                outcome_2 = result.mapping_outcomes[1]
                assert outcome_2.company_source_id == cs_2.id
                assert outcome_2.status == STATUS_EMPTY
                assert outcome_2.records_ingested == 0

                # 6. Verify CollectionRun records in PostgreSQL
                run_1 = await run_repo.get_by_id(outcome_1.collection_run_id)
                assert run_1 is not None
                assert run_1.status == "COMPLETED"
                assert run_1.records_ingested == 1

                run_2 = await run_repo.get_by_id(outcome_2.collection_run_id)
                assert run_2 is not None
                assert run_2.status == "COMPLETED"
                assert run_2.records_ingested == 0

                # 7. Clean up
                await session.rollback()

    finally:
        await dispose_engine()


@pytest.mark.asyncio
async def test_collection_runner_rollback_isolation_integration() -> None:
    """Verify session.rollback() reverts all runner writes leaving zero orphaned records."""
    unique_suffix = uuid.uuid4().hex[:8]
    board_token = f"acme-rb-{unique_suffix}"
    session_factory = get_sessionmaker()

    raw_listing_bytes = json.dumps(SAMPLE_GH_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_GH_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/9101":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    created_run_id: uuid.UUID | None = None
    created_company_id: uuid.UUID = uuid.uuid4()

    try:
        async with httpx.AsyncClient(transport=transport) as raw_client:
            http_client = PoliteHttpClient(client=raw_client)

            # Session 1: Run collection and then roll back
            async with session_factory() as session:
                company_repo = CompanyRepository(session)
                source_repo = SourceRepository(session)
                cs_repo = CompanySourceRepository(session)

                source = await source_repo.get_by_name("GREENHOUSE")
                if source is None:
                    source = await source_repo.create(
                        Source(
                            id=uuid.uuid4(),
                            source_name="GREENHOUSE",
                            source_type="ATS_API",
                        )
                    )

                company = await company_repo.create(
                    Company(
                        id=created_company_id,
                        name=f"Rollback Test Corp {unique_suffix}",
                        normalized_name=f"rollback-test-corp-{unique_suffix}",
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

                runner = CollectionRunner.from_session(session, http_client=http_client)
                outcome = await runner.run_mapping(cs.id)
                assert outcome.is_success
                created_run_id = outcome.collection_run_id

                # Crucial: Roll back the entire session
                await session.rollback()

            # Session 2: Fresh query to verify zero records survived rollback
            async with session_factory() as verify_session:
                # Check CollectionRun
                if created_run_id:
                    res_run = await verify_session.get(CollectionRun, created_run_id)
                    assert res_run is None

                # Check Company
                res_comp = await verify_session.get(Company, created_company_id)
                assert res_comp is None

                # Check RawJobs
                stmt_raw = select(RawJob).where(RawJob.source_url.contains(board_token))
                res_raw = await verify_session.execute(stmt_raw)
                assert len(res_raw.scalars().all()) == 0

                # Check Canonical Jobs
                stmt_jobs = select(Job).where(Job.company_id == created_company_id)
                res_jobs = await verify_session.execute(stmt_jobs)
                assert len(res_jobs.scalars().all()) == 0

    finally:
        await dispose_engine()
