"""Integration tests for JobDeduplicationService against live PostgreSQL 18.

Validates:
1. Stage 1 exact source external ID duplicate resolution via live ORM relations.
2. Stage 2 ATS application URL normalization and multi-match conflict detection in PostgreSQL.
3. Stage 3 composite matching against canonical jobs using PostgreSQL partial indexes and date proximity.
4. Provenance link recording (JobSourceReference) without transaction commit.
5. Strict transactional rollback leaving zero lingering records.
"""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationCandidate,
    DeduplicationStage,
)
from app.deduplication.service import JobDeduplicationService
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
async def test_deduplication_service_live_stages_and_provenance_integration() -> None:
    """Verify JobDeduplicationService Stages 1, 2, 3 and provenance links in PostgreSQL."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id_1 = f"gh-job-{unique_suffix}"
    app_url_1 = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id_1}"
    normalized_title = f"senior ai engineer {unique_suffix}"
    normalized_location = "bengaluru"
    base_posted_at = datetime.now(UTC) - timedelta(days=2)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)
            dedup_service = JobDeduplicationService.from_session(session)

            # 1. Seed Company & Source
            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Dedup Test Company {unique_suffix}",
                    normalized_name=f"dedup-test-company-{unique_suffix}",
                )
            )
            source_gh = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"DEDUP_GH_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            source_lever = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"DEDUP_LEVER_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs_gh = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source_gh.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            raw_body = b'{"title": "Senior AI Engineer"}'
            raw_job_1 = await raw_repo.create(
                RawJob(
                    id=uuid.uuid4(),
                    company_source_id=cs_gh.id,
                    source_job_id=ext_job_id_1,
                    source_job_id_type="NATIVE",
                    source_url=app_url_1,
                    raw_body=raw_body,
                    raw_payload={"title": "Senior AI Engineer"},
                    content_hash=hashlib.sha256(raw_body).hexdigest(),
                )
            )

            # 2. Seed Canonical Job 1
            canonical_job_1 = await job_repo.create(
                Job(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    company_name=company.name,
                    title="Senior AI Engineer",
                    normalized_title=normalized_title,
                    location="Bengaluru, Karnataka, India",
                    normalized_location=normalized_location,
                    description="Build cutting-edge agentic workflows.",
                    role_priority="P1",
                    relevance_status="RELEVANT",
                    eligibility_status="NEW",
                    posted_at=base_posted_at,
                    application_url=app_url_1,
                    source_verified=True,
                    jd_verified=True,
                    company_verified=True,
                    location_verified=True,
                    experience_verified=True,
                    salary_verified=False,
                    verification_confidence=Decimal("0.90"),
                )
            )

            # 3. Seed Primary JobSourceReference for Canonical Job 1
            await jsr_repo.create(
                JobSourceReference(
                    id=uuid.uuid4(),
                    job_id=canonical_job_1.id,
                    raw_job_id=raw_job_1.id,
                    source_id=source_gh.id,
                    source_job_id=ext_job_id_1,
                    source_job_id_type="NATIVE",
                    source_url=app_url_1,
                    application_url=app_url_1,
                    posted_at=base_posted_at,
                    is_primary=True,
                )
            )

            # -----------------------------------------------------------------
            # TEST A: Stage 1 Exact Match (Platform Source + External ID)
            # -----------------------------------------------------------------
            cand_stage_1 = DeduplicationCandidate(
                source_id=source_gh.id,
                source_job_id=ext_job_id_1,
                source_job_id_type="NATIVE",
                source_url=app_url_1,
                application_url=app_url_1,
                company_id=company.id,
                company_name=company.name,
                title="Senior AI Engineer",
                normalized_title=normalized_title,
                location="Bengaluru",
                normalized_location=normalized_location,
                posted_at=base_posted_at,
            )

            res_1 = await dedup_service.deduplicate(cand_stage_1)
            assert res_1.action == DeduplicationAction.MATCH
            assert res_1.stage == DeduplicationStage.STAGE_1_EXACT_SOURCE_ID
            assert res_1.confidence == 1.0
            assert res_1.canonical_job_id == canonical_job_1.id

            # -----------------------------------------------------------------
            # TEST B: Stage 2 URL Match (Different Source, Same ATS URL + UTM params)
            # -----------------------------------------------------------------
            cand_stage_2 = DeduplicationCandidate(
                source_id=source_lever.id,  # Different platform source
                source_job_id=f"lever-{unique_suffix}",
                source_job_id_type="NATIVE",
                source_url=f"https://aggregator.com/jobs/{unique_suffix}",
                # Application URL contains tracking parameters to strip
                application_url=f"{app_url_1}?utm_source=aggregator&utm_campaign=hires",
                company_id=company.id,
                company_name=company.name,
                title="Senior AI Engineer (Repost)",
                normalized_title=f"unrelated title {unique_suffix}",  # Title differs
                location="Bengaluru",
                normalized_location=normalized_location,
                posted_at=base_posted_at,
            )

            res_2 = await dedup_service.deduplicate(cand_stage_2)
            assert res_2.action == DeduplicationAction.MATCH
            assert res_2.stage == DeduplicationStage.STAGE_2_EXACT_URL
            assert res_2.confidence == 0.98
            assert res_2.canonical_job_id == canonical_job_1.id

            # -----------------------------------------------------------------
            # TEST C: Stage 3 Composite Match (Company + Title + Location + <= 14d)
            # -----------------------------------------------------------------
            cand_stage_3 = DeduplicationCandidate(
                source_id=source_lever.id,
                source_job_id=f"lever-stage3-{unique_suffix}",
                source_job_id_type="NATIVE",
                source_url=f"https://aggregator.com/jobs/diff-{unique_suffix}",
                application_url=f"https://custom-portal.com/apply/{unique_suffix}",  # Non-matching URL
                company_id=company.id,
                company_name=company.name,
                title="Senior AI Engineer",
                normalized_title=normalized_title,  # Same normalized title
                location="Bengaluru, Karnataka",
                normalized_location=normalized_location,  # Same normalized location
                posted_at=base_posted_at + timedelta(days=4),  # 4 days delta (<= 14 days)
            )

            res_3 = await dedup_service.deduplicate(cand_stage_3)
            assert res_3.action == DeduplicationAction.MATCH
            assert res_3.stage == DeduplicationStage.STAGE_3_COMPOSITE
            assert res_3.confidence == 0.90
            assert res_3.canonical_job_id == canonical_job_1.id

            # -----------------------------------------------------------------
            # TEST D: Stage 3 Out-of-Window (> 14d) falls through to NEW
            # -----------------------------------------------------------------
            cand_old = DeduplicationCandidate(
                source_id=source_lever.id,
                source_job_id=f"lever-old-{unique_suffix}",
                source_job_id_type="NATIVE",
                source_url=f"https://aggregator.com/jobs/old-{unique_suffix}",
                application_url=f"https://custom-portal.com/apply/old-{unique_suffix}",
                company_id=company.id,
                company_name=company.name,
                title="Senior AI Engineer",
                normalized_title=normalized_title,
                location="Bengaluru",
                normalized_location=normalized_location,
                posted_at=base_posted_at - timedelta(days=30),  # 30 days delta (> 14 days)
            )

            res_old = await dedup_service.deduplicate(cand_old)
            assert res_old.action == DeduplicationAction.NEW
            assert res_old.stage == DeduplicationStage.NONE
            assert res_old.confidence == 0.0
            assert res_old.canonical_job_id is None

            # -----------------------------------------------------------------
            # TEST E: Provenance Linking without Commit & Idempotent Re-observation
            # -----------------------------------------------------------------
            new_ref = await dedup_service.record_provenance_link(
                job_id=canonical_job_1.id,
                raw_job_id=raw_job_1.id,
                source_id=source_lever.id,
                source_job_id=f"provenance-link-{unique_suffix}",
                source_url=f"https://aggregator.com/jobs/prov-{unique_suffix}",
                source_job_id_type="NATIVE",
                application_url=app_url_1,
                is_primary=False,
            )
            assert new_ref.id is not None
            assert new_ref.job_id == canonical_job_1.id

            # Repeated call for same source and external ID idempotently refreshes
            repeated_ref = await dedup_service.record_provenance_link(
                job_id=canonical_job_1.id,
                raw_job_id=raw_job_1.id,
                source_id=source_lever.id,
                source_job_id=f"provenance-link-{unique_suffix}",
                source_url=f"https://aggregator.com/jobs/prov-{unique_suffix}",
                source_job_id_type="NATIVE",
                application_url=app_url_1,
                is_primary=False,
            )
            assert repeated_ref.id == new_ref.id

            # Cross-job collision raises descriptive ValueError without Postgres IntegrityError
            another_job_id = uuid.uuid4()
            with pytest.raises(ValueError, match="already linked"):
                await dedup_service.record_provenance_link(
                    job_id=another_job_id,
                    raw_job_id=raw_job_1.id,
                    source_id=source_lever.id,
                    source_job_id=f"provenance-link-{unique_suffix}",
                    source_url=f"https://aggregator.com/jobs/prov-{unique_suffix}",
                )

            # Roll back everything to leave zero persisted test data
            await session.rollback()

    except Exception:
        raise

    # 4. Verify clean database state after rollback
    async with session_factory() as verify_session:
        res = await verify_session.execute(
            select(Job).where(Job.normalized_title == normalized_title)
        )
        assert res.scalar_one_or_none() is None
