"""Integration tests for JobVerificationService and PostgreSQL verification records.

Boundary: Live PostgreSQL 18 integration tests.
Validates:
1. Persisting a VerificationRecord with all dimension outcomes and audit trail.
2. Consecutive verification runs append separate historical records without overwriting.
3. The latest-record query returns the correct record under verification_run_at DESC ordering.
4. The VerificationRecord outcome is seamlessly consumed by JobFilterDecisionService.
5. Transaction rollback cleanly isolates tests without leaving test records behind.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.orchestration.decision_service import JobFilterDecisionService
from app.orchestration.verification_service import JobVerificationService
from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.persistence.session import dispose_engine, get_sessionmaker
from app.verification.schemas import JobVerificationInput

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

SAMPLE_JD = (
    "We are seeking an enthusiastic AI Software Engineer to join our growing team in Bengaluru. "
    "You will design, develop, and deploy scalable LLM applications, RAG pipelines, and agentic workflows. "
    "Key requirements include strong Python skills, familiarity with FastAPI, PostgreSQL, and modern AI frameworks. "
    "This is a full-time role offering great learning opportunities, mentorship, and impactful work."
)


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_verification_persistence_lifecycle_and_history_integration() -> None:
    """Verify live PostgreSQL persistence, append-only history, and consumption by JobFilterDecisionService."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Verif Test Corp {unique_suffix}"
    job_title = f"AI Engineer {unique_suffix}"
    base_posted_at = datetime.now(UTC) - timedelta(days=2)

    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        job_repo = JobRepository(session)
        verif_repo = VerificationRecordRepository(session)

        # 1. Setup: Create Company and Canonical Job
        company = Company(
            name=company_name,
            normalized_name=company_name.lower(),
            sector="IT",
            business_model="PRODUCT",
        )
        created_company = await company_repo.create(company)

        job = Job(
            company_id=created_company.id,
            company_name=created_company.name,
            title=job_title,
            normalized_title="ai engineer",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            remote_type="HYBRID",
            description=SAMPLE_JD,
            experience_raw="0-2 years",
            experience_min_years=Decimal("0.0"),
            experience_max_years=Decimal("2.0"),
            salary_min=Decimal("1200000.00"),
            salary_max=Decimal("1800000.00"),
            salary_currency="INR",
            salary_period="ANNUAL",
            posted_at=base_posted_at,
            collected_at=datetime.now(UTC),
            role_priority="P1",
            relevance_status="TARGET",
            eligibility_status="NEW",
            source_verified=False,
            jd_verified=False,
            company_verified=False,
            location_verified=False,
            experience_verified=False,
            salary_verified=False,
            verification_confidence=Decimal("0.00"),
        )
        created_job = await job_repo.create(job)
        job_id = created_job.id

        # 2. Instantiate JobVerificationService and execute First Verification Run
        verif_service = JobVerificationService.from_session(session)

        evidence_1 = JobVerificationInput(
            job_id=job_id,
            source_name="greenhouse",
            source_type="ATS",
            is_source_approved=True,
            payload_format_valid=True,
            description=SAMPLE_JD,
            http_status_code=200,
            is_page_active=True,
            application_url="https://boards.greenhouse.io/company/jobs/101",
            application_url_status=200,
            application_url_reachable=True,
            company_name=created_company.name,
            is_company_registry_verified=True,
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            remote_type="HYBRID",
            experience_min_years=Decimal("0.0"),
            experience_max_years=Decimal("2.0"),
            salary_min=Decimal("1200000.00"),
            salary_max=Decimal("1800000.00"),
            posted_at=base_posted_at,
            collected_at=datetime.now(UTC),
            publication_date_corroborated=True,
        )

        run_time_1 = datetime.now(UTC) - timedelta(minutes=10)
        outcome_1 = await verif_service.evaluate_and_persist(
            created_job,
            evidence=evidence_1,
            verification_run_at=run_time_1,
        )

        assert outcome_1.record.id is not None
        assert outcome_1.record.job_id == job_id
        assert outcome_1.record.source_verified is True
        assert outcome_1.record.jd_verified is True
        assert outcome_1.record.company_verified is True
        assert outcome_1.record.location_verified is True
        assert outcome_1.record.experience_verified is True
        assert outcome_1.record.salary_verified is True
        assert outcome_1.record.posting_date_verified is True
        assert outcome_1.record.passed is True
        assert outcome_1.record.verification_confidence == Decimal("1.00")
        assert outcome_1.job.verification_confidence == Decimal("1.00")
        assert outcome_1.job.source_verified is True

        # 3. Consecutive Verification Run (e.g. Periodic Re-verification with unstated salary)
        # Verifies append-only preservation of prior records
        evidence_2 = evidence_1.model_copy(
            update={
                "salary_min": None,
                "salary_max": None,
            }
        )
        run_time_2 = datetime.now(UTC)
        outcome_2 = await verif_service.evaluate_and_persist(
            created_job,
            evidence=evidence_2,
            verification_run_at=run_time_2,
        )

        assert outcome_2.record.id != outcome_1.record.id
        assert outcome_2.record.salary_verified is False
        assert outcome_2.record.verification_confidence == Decimal("0.95")
        assert outcome_2.job.verification_confidence == Decimal("0.95")

        # 4. Query Historical Verification Records via Repository
        all_records = await verif_repo.list_by_job_id(job_id)
        assert len(all_records) == 2
        # Ordered by verification_run_at DESC: record 2 is first, record 1 is second
        assert all_records[0].id == outcome_2.record.id
        assert all_records[1].id == outcome_1.record.id

        latest_record = await verif_repo.get_latest_by_job_id(job_id)
        assert latest_record is not None
        assert latest_record.id == outcome_2.record.id
        assert latest_record.posting_date_verified is True

        # 5. Integration with JobFilterDecisionService
        # The filter service must automatically consume latest_record.posting_date_verified
        filter_service = JobFilterDecisionService.from_session(session)
        decision_outcome = await filter_service.evaluate_and_persist(
            created_job,
            reference_time=base_posted_at + timedelta(days=1),
        )

        assert decision_outcome.job.eligibility_status == "ELIGIBLE"
        assert decision_outcome.decision.final_status == "ELIGIBLE"
        assert decision_outcome.decision.evaluation_context["posting_date_verified"] is True

        # Roll back all test artifacts cleanly
        await session.rollback()


@pytest.mark.asyncio
async def test_verification_rollback_isolation_integration() -> None:
    """Verify that uncommitted verification records do not persist after rollback."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Rollback Corp {unique_suffix}"
    job_title = f"AI Engineer {unique_suffix}"

    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        job_repo = JobRepository(session)
        verif_service = JobVerificationService.from_session(session)

        company = Company(
            name=company_name,
            normalized_name=company_name.lower(),
            sector="IT",
            business_model="PRODUCT",
        )
        created_company = await company_repo.create(company)

        job = Job(
            company_id=created_company.id,
            company_name=created_company.name,
            title=job_title,
            normalized_title="ai engineer",
            description=SAMPLE_JD,
            remote_type="ON_SITE",
            role_priority="P1",
            relevance_status="TARGET",
            eligibility_status="NEW",
            source_verified=True,
            jd_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            verification_confidence=Decimal("1.00"),
        )
        created_job = await job_repo.create(job)
        job_id = created_job.id

        await verif_service.evaluate_and_persist(created_job)

        # Explicitly roll back transaction
        await session.rollback()

    # In a fresh session, verify that neither the job nor verification record exist
    async with session_factory() as verify_session:
        v_repo = VerificationRecordRepository(verify_session)
        j_repo = JobRepository(verify_session)

        persisted_job = await j_repo.get_by_id(job_id)
        assert persisted_job is None

        records = await v_repo.list_by_job_id(job_id)
        assert len(records) == 0
