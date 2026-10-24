"""Integration tests for asynchronous persistence repositories.

Executes real SQL transactions against the live local PostgreSQL 18 instance.
Validates:
1. Repository entity persistence (Company, Source, CompanySource, RawJob, Job, JobSourceReference).
2. Complex locator queries matching PostgreSQL partial unique indexes.
3. Raw job payload immutability across JSONB serialization.
4. Stage 1, 2, and 3 deduplication queries and provenance links.
5. Strict transactional rollback leaving zero persisted test data.
"""

import asyncio
import hashlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.session import dispose_engine, get_sessionmaker

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_company_and_source_repositories_integration() -> None:
    """Verify Company and Source repositories create, retrieve, and roll back."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Repo Integration Employer {unique_suffix}"
    normalized_name = f"repo-employer-{unique_suffix}"
    source_name = f"REPO_SRC_{unique_suffix.upper()}"

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)

            # Create Company
            company = Company(
                id=uuid.uuid4(),
                name=company_name,
                normalized_name=normalized_name,
                sector="IT",
                business_model="PRODUCT",
                is_verified=True,
                confidence=Decimal("1.00"),
            )
            created_company = await company_repo.create(company)
            assert created_company.id == company.id

            # Create Source
            source = Source(
                id=uuid.uuid4(),
                source_name=source_name,
                source_type="ATS_API",
                is_active=True,
                rate_limit_rpm=90,
            )
            created_source = await source_repo.create(source)
            assert created_source.id == source.id

            # Clear session cache to force real database SELECT
            session.expunge_all()

            # Retrieve Company
            by_id = await company_repo.get_by_id(company.id)
            assert by_id is not None
            assert by_id.name == company_name
            assert by_id.normalized_name == normalized_name
            assert by_id.is_verified is True

            by_slug = await company_repo.get_by_normalized_name(normalized_name)
            assert by_slug is not None
            assert by_slug.id == company.id

            # Retrieve Source
            src_by_id = await source_repo.get_by_id(source.id)
            assert src_by_id is not None
            assert src_by_id.source_name == source_name

            src_by_name = await source_repo.get_by_name(source_name)
            assert src_by_name is not None
            assert src_by_name.id == source.id

            # Roll back transaction
            await session.rollback()

        # Verify zero test data persisted
        async with session_factory() as verify_session:
            verify_company_repo = CompanyRepository(verify_session)
            verify_source_repo = SourceRepository(verify_session)

            assert await verify_company_repo.get_by_id(company.id) is None
            assert await verify_source_repo.get_by_id(source.id) is None
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and latest schema migrations are applied. Details: {exc}"
        )


@pytest.mark.asyncio
async def test_company_source_repository_locators_integration() -> None:
    """Verify CompanySource locator queries matching PostgreSQL partial unique indexes."""
    unique_suffix = uuid.uuid4().hex[:8]
    normalized_name = f"cs-employer-{unique_suffix}"
    source_name = f"CS_SRC_{unique_suffix.upper()}"
    ats_token = f"board-{unique_suffix}"
    career_url = f"https://careers.example.com/{unique_suffix}"

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"CS Employer {unique_suffix}",
                    normalized_name=normalized_name,
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=source_name,
                    source_type="ATS_API",
                )
            )

            # Mapping with ATS token
            cs_ats = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=ats_token,
                    discovery_method="AUTOMATED_PROBE",
                    confidence=Decimal("1.00"),
                )
            )

            # Mapping with career URL
            source2 = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"CS_SRC2_{unique_suffix.upper()}",
                    source_type="CUSTOM_SCRAPER",
                )
            )
            cs_url = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source2.id,
                    career_url=career_url,
                    discovery_method="SEARCH_FALLBACK",
                    confidence=Decimal("0.85"),
                )
            )

            session.expunge_all()

            # Query via partial unique index 1: ats_identifier
            found_ats = await cs_repo.get_by_locator(
                company.id, source.id, ats_identifier=ats_token
            )
            assert found_ats is not None
            assert found_ats.id == cs_ats.id
            assert found_ats.ats_identifier == ats_token

            # Query via operational index: idx_company_sources_ats
            found_by_token = await cs_repo.get_by_ats_identifier(source.id, ats_token)
            assert found_by_token is not None
            assert found_by_token.id == cs_ats.id

            # Query list_by_ats_identifier
            all_by_token = await cs_repo.list_by_ats_identifier(source.id, ats_token)
            assert len(all_by_token) == 1
            assert all_by_token[0].id == cs_ats.id

            # Bare locator call must raise ValueError
            with pytest.raises(ValueError, match="At least one locator"):
                await cs_repo.get_by_locator(company.id, source.id)

            # Query via partial unique index 2: career_url (ats_identifier IS NULL)
            found_url = await cs_repo.get_by_locator(company.id, source2.id, career_url=career_url)
            assert found_url is not None
            assert found_url.id == cs_url.id
            assert found_url.career_url == career_url

            # Roll back
            await session.rollback()

        # Verify rollback
        async with session_factory() as verify_session:
            verify_cs_repo = CompanySourceRepository(verify_session)
            assert await verify_cs_repo.get_by_id(cs_ats.id) is None
            assert await verify_cs_repo.get_by_id(cs_url.id) is None
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and latest schema migrations are applied. Details: {exc}"
        )


@pytest.mark.asyncio
async def test_raw_job_repository_immutability_integration() -> None:
    """Verify RawJob append-only persistence, exact BYTEA wire preservation, and JSONB queries."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"ext-gh-{unique_suffix}"
    raw_wire_bytes = (
        b'{"id":"'
        + ext_job_id.encode("utf-8")
        + b'","title":"Staff Backend Engineer","nested":{"team":"Infrastructure","active":true,"count":42},"skills":["Python","PostgreSQL","AsyncIO"]}'
    )
    payload_content_hash = hashlib.sha256(raw_wire_bytes).hexdigest()
    complex_payload = {
        "id": ext_job_id,
        "title": "Staff Backend Engineer",
        "nested": {"team": "Infrastructure", "active": True, "count": 42},
        "skills": ["Python", "PostgreSQL", "AsyncIO"],
    }

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Raw Employer {unique_suffix}",
                    normalized_name=f"raw-employer-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"RAW_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            raw_job = RawJob(
                id=uuid.uuid4(),
                company_source_id=cs.id,
                source_job_id=ext_job_id,
                source_job_id_type="NATIVE",
                source_url=f"https://boards.greenhouse.io/jobs/{ext_job_id}",
                raw_body=raw_wire_bytes,
                raw_payload=complex_payload,
                content_hash=payload_content_hash,
                http_status=200,
            )
            created_raw = await raw_repo.create(raw_job)
            assert created_raw.id == raw_job.id

            session.expunge_all()

            # Retrieve latest by compound index
            reloaded = await raw_repo.get_latest_by_company_source_and_external_id(
                cs.id, ext_job_id
            )
            assert reloaded is not None
            assert reloaded.source_job_id == ext_job_id
            assert reloaded.source_job_id_type == "NATIVE"
            assert reloaded.content_hash == payload_content_hash
            # Verify exact wire byte preservation
            assert reloaded.raw_body == raw_wire_bytes
            assert hashlib.sha256(reloaded.raw_body).hexdigest() == reloaded.content_hash
            # Verify raw payload integrity
            assert reloaded.raw_payload == complex_payload
            assert reloaded.raw_payload["nested"]["team"] == "Infrastructure"

            # Retrieve by content hash
            by_hash = await raw_repo.get_by_content_hash(payload_content_hash)
            assert len(by_hash) == 1
            assert by_hash[0].id == raw_job.id

            # Verify HTML career-page format persistence and exact wire round-trip
            html_bytes = b"<!DOCTYPE html><html><head><title>Careers</title></head><body><h1>Principal ML Engineer</h1></body></html>"
            html_hash = hashlib.sha256(html_bytes).hexdigest()
            html_payload = {"title": "Principal ML Engineer", "extracted_from": "html"}
            html_raw_job = RawJob(
                id=uuid.uuid4(),
                company_source_id=cs.id,
                source_job_id=f"html-{unique_suffix}",
                source_job_id_type="NATIVE",
                source_url=f"https://careers.example.com/jobs/html-{unique_suffix}",
                payload_format="HTML",
                raw_body=html_bytes,
                raw_payload=html_payload,
                content_hash=html_hash,
                http_status=200,
            )
            created_html = await raw_repo.create(html_raw_job)
            assert created_html.id == html_raw_job.id

            # Verify repository boundary rejects inconsistent payload in active transaction
            inconsistent_raw = RawJob(
                id=uuid.uuid4(),
                company_source_id=cs.id,
                source_job_id=f"bad-{unique_suffix}",
                source_job_id_type="NATIVE",
                source_url=f"https://boards.greenhouse.io/jobs/bad-{unique_suffix}",
                payload_format="JSON",
                raw_body=raw_wire_bytes,
                raw_payload={"tampered": "content"},  # Does not match raw_wire_bytes
                content_hash=payload_content_hash,
                http_status=200,
            )
            with pytest.raises(
                ValueError, match="raw_job.raw_payload does not match parsed JSON from raw_body"
            ):
                await raw_repo.create(inconsistent_raw)

            # Expunge and reload HTML raw job from PostgreSQL to test round-trip
            session.expunge(html_raw_job)
            reloaded_html = await raw_repo.get_by_id(html_raw_job.id)
            assert reloaded_html is not None
            assert reloaded_html.payload_format == "HTML"
            assert reloaded_html.raw_body == html_bytes
            assert reloaded_html.raw_payload == html_payload
            assert hashlib.sha256(reloaded_html.raw_body).hexdigest() == reloaded_html.content_hash

            await session.rollback()

        # Verify rollback
        async with session_factory() as verify_session:
            verify_raw_repo = RawJobRepository(verify_session)
            assert await verify_raw_repo.get_by_id(raw_job.id) is None
            assert await verify_raw_repo.get_by_id(html_raw_job.id) is None
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and latest schema migrations are applied. Details: {exc}"
        )


@pytest.mark.asyncio
async def test_job_and_provenance_link_repositories_integration() -> None:
    """Verify Job and JobSourceReference creation, deduplication queries, and rollback."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"job-ext-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"
    normalized_title = f"backend engineer {unique_suffix}"
    normalized_location = "mumbai"

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Canonical Employer {unique_suffix}",
                    normalized_name=f"canonical-employer-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"JOB_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )
            raw_body_bytes = b'{"title": "Backend Engineer"}'
            raw_job = await raw_repo.create(
                RawJob(
                    id=uuid.uuid4(),
                    company_source_id=cs.id,
                    source_job_id=ext_job_id,
                    source_job_id_type="NATIVE",
                    source_url=app_url,
                    raw_body=raw_body_bytes,
                    raw_payload={"title": "Backend Engineer"},
                    content_hash=hashlib.sha256(raw_body_bytes).hexdigest(),
                )
            )

            # Create canonical Job
            job = await job_repo.create(
                Job(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    company_name=company.name,
                    title="Backend Engineer",
                    normalized_title=normalized_title,
                    location="Mumbai, Maharashtra, India",
                    normalized_location=normalized_location,
                    description="Design high-performance database services.",
                    role_priority="P1",
                    relevance_status="RELEVANT",
                    eligibility_status="NEW",
                    application_url=app_url,
                    source_verified=True,
                    jd_verified=True,
                    company_verified=True,
                    location_verified=True,
                    experience_verified=True,
                    salary_verified=False,
                    verification_confidence=Decimal("0.95"),
                )
            )

            # Create provenance link JobSourceReference
            jsr = await jsr_repo.create(
                JobSourceReference(
                    id=uuid.uuid4(),
                    job_id=job.id,
                    raw_job_id=raw_job.id,
                    source_id=source.id,
                    source_job_id=ext_job_id,
                    source_job_id_type="NATIVE",
                    source_url=app_url,
                    application_url=app_url,
                    is_primary=True,
                )
            )

            session.expunge_all()

            # Stage 1 Deduplication Lookup: by source ID and external source job ID
            matched_stage1 = await job_repo.get_by_source_reference(source.id, ext_job_id)
            assert matched_stage1 is not None
            assert matched_stage1.id == job.id

            # Stage 2 Deduplication Lookup: by application URL
            matched_stage2 = await job_repo.get_by_application_url(app_url)
            assert matched_stage2 is not None
            assert matched_stage2.id == job.id

            # Stage 3 Deduplication Lookup: composite candidate lookup
            candidates = await job_repo.find_by_composite_attributes(
                company.id,
                normalized_title=normalized_title,
                normalized_location=normalized_location,
            )
            assert len(candidates) == 1
            assert candidates[0].id == job.id

            # JobSourceReference operations
            ref_by_source = await jsr_repo.get_by_source_external_id(source.id, ext_job_id)
            assert ref_by_source is not None
            assert ref_by_source.id == jsr.id
            assert ref_by_source.is_primary is True

            job_refs = await jsr_repo.list_by_job_id(job.id)
            assert len(job_refs) == 1
            assert job_refs[0].id == jsr.id

            raw_refs = await jsr_repo.list_by_raw_job_id(raw_job.id)
            assert len(raw_refs) == 1
            assert raw_refs[0].id == jsr.id

            # Roll back
            await session.rollback()

        # Verify zero test data persisted
        async with session_factory() as verify_session:
            verify_job_repo = JobRepository(verify_session)
            verify_jsr_repo = JobSourceReferenceRepository(verify_session)
            assert await verify_job_repo.get_by_id(job.id) is None
            assert await verify_jsr_repo.get_by_id(jsr.id) is None
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and latest schema migrations are applied. Details: {exc}"
        )
