"""Integration tests for JobFilterDecisionService against live PostgreSQL 18.

Validates:
1. Persisting a filter decision and updating its associated canonical Job in PostgreSQL.
2. Resolving publication-date verification state from a live VerificationRecord in PostgreSQL.
3. Recording consecutive evaluations without losing historical FilterDecision audit rows.
4. Transaction rollback leaving neither the Job status update nor the FilterDecision persisted.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.orchestration.decision_service import JobFilterDecisionService
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.filter_decision_repository import FilterDecisionRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.persistence.session import dispose_engine, get_sessionmaker

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_decision_persistence_lifecycle_and_history_integration() -> None:
    """Verify live PostgreSQL persistence, VerificationRecord integration, and consecutive history."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Test Corp {unique_suffix}"
    job_title = f"AI Engineer {unique_suffix}"
    base_posted_at = datetime.now(UTC) - timedelta(days=2)

    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        job_repo = JobRepository(session)
        verif_repo = VerificationRecordRepository(session)
        decision_repo = FilterDecisionRepository(session)

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
            remote_type="ON_SITE",
            description="Lead generative AI development and RAG pipelines.",
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

        # 2. Add Authoritative VerificationRecord
        v_record = VerificationRecord(
            job_id=job_id,
            verification_run_at=datetime.now(UTC),
            source_verified=True,
            jd_verified=True,
            application_url_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            posting_date_verified=True,  # Authoritative verified publication date
            verification_confidence=Decimal("0.95"),
            passed=True,
            audit_trail={"method": "postgres_integration_test"},
        )
        await verif_repo.create(v_record)

        # 3. Instantiate Service & Evaluate (Unsupplied verification state maps to VerificationRecord)
        service = JobFilterDecisionService.from_session(session)

        outcome_1 = await service.evaluate_and_persist(
            job_id,
            reference_time=base_posted_at + timedelta(days=1),
        )

        assert outcome_1.job.eligibility_status == "ELIGIBLE"
        assert outcome_1.job.role_priority == "P1"
        assert outcome_1.job.relevance_status == "TARGET"
        assert outcome_1.decision.final_status == "ELIGIBLE"
        assert outcome_1.decision.evaluation_context["posting_date_verified"] is True

        # Query FilterDecision from PostgreSQL session
        decisions_1 = await decision_repo.list_by_job_id(job_id)
        assert len(decisions_1) == 1
        assert decisions_1[0].final_status == "ELIGIBLE"

        # 4. Consecutive Evaluation (Aging to EXPIRED) Preserving History
        outcome_2 = await service.evaluate_and_persist(
            job_id,
            reference_time=base_posted_at + timedelta(days=8),
        )

        assert outcome_2.job.eligibility_status == "EXPIRED"
        assert outcome_2.job.rejection_reason == "JOB_TOO_OLD"
        assert outcome_2.decision.final_status == "EXPIRED"

        # Verify both decisions exist in PostgreSQL (history preserved, not overwritten)
        decisions_2 = await decision_repo.list_by_job_id(job_id)
        assert len(decisions_2) == 2
        # Latest first
        assert decisions_2[0].final_status == "EXPIRED"
        assert "JOB_TOO_OLD" in decisions_2[0].reason_codes
        # Prior decision retained
        assert decisions_2[1].final_status == "ELIGIBLE"

        # Roll back transaction to leave zero persisted test records
        await session.rollback()

    # 5. Verify clean database state after rollback
    async with session_factory() as verify_session:
        res = await verify_session.execute(select(Job).where(Job.title == job_title))
        assert res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_decision_persistence_rollback_isolation_integration() -> None:
    """Verify transaction rollback leaves neither Job status update nor FilterDecision persisted."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Rollback Corp {unique_suffix}"
    job_title = f"AI Engineer {unique_suffix}"
    base_posted_at = datetime.now(UTC) - timedelta(days=2)

    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        job_repo = JobRepository(session)

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
            remote_type="ON_SITE",
            description="AI Engineer vacancy.",
            experience_raw="0-2 years",
            experience_min_years=Decimal("0.0"),
            experience_max_years=Decimal("2.0"),
            salary_min=Decimal("1000000.00"),
            salary_max=Decimal("1500000.00"),
            salary_currency="INR",
            salary_period="ANNUAL",
            posted_at=base_posted_at,
            collected_at=datetime.now(UTC),
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

        service = JobFilterDecisionService.from_session(session)

        # Run evaluation with explicit posting_date_verified=True
        outcome = await service.evaluate_and_persist(
            job_id,
            posting_date_verified=True,
            reference_time=base_posted_at + timedelta(days=1),
        )

        assert outcome.job.eligibility_status == "ELIGIBLE"
        assert outcome.decision.final_status == "ELIGIBLE"

        # Explicitly roll back the transaction
        await session.rollback()

    # In a new session, confirm neither the Job nor FilterDecision exists
    async with session_factory() as verify_session:
        res_job = await verify_session.execute(select(Job).where(Job.title == job_title))
        assert res_job.scalar_one_or_none() is None

        res_decision = await verify_session.execute(
            select(FilterDecision).where(FilterDecision.job_id == job_id)
        )
        assert res_decision.scalars().all() == []
