"""Unit tests for JobVerificationService and persistence orchestration.

Boundary: Unit testing with mocks.
Verifies:
- Caller-controlled transactions (no commit inside service or repositories).
- Persistence of VerificationRecord with all schema fields.
- Preservation of prior verification records across repeated runs.
- Integration between JobVerificationService and repositories.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.orchestration.verification_service import (
    JobVerificationOutcome,
    JobVerificationService,
)
from app.persistence.models.decision import VerificationRecord
from app.persistence.models.job import Job
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.verification.evaluator import JobVerificationEvaluator
from app.verification.schemas import JobVerificationInput

SAMPLE_JD = (
    "We are seeking an enthusiastic AI Software Engineer to join our growing team in Bengaluru. "
    "You will design, develop, and deploy scalable LLM applications, RAG pipelines, and agentic workflows. "
    "Key requirements include strong Python skills, familiarity with FastAPI, PostgreSQL, and modern AI frameworks. "
    "This is a full-time role offering great learning opportunities, mentorship, and impactful work."
)


@pytest.fixture
def base_evidence() -> JobVerificationInput:
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    return JobVerificationInput(
        source_name="greenhouse",
        source_type="ATS",
        is_source_approved=True,
        payload_format_valid=True,
        description=SAMPLE_JD,
        http_status_code=200,
        is_page_active=True,
        application_url="https://boards.greenhouse.io/company/jobs/123",
        application_url_status=200,
        application_url_reachable=True,
        company_name="Acme AI",
        is_company_registry_verified=True,
        location="Bengaluru, Karnataka, India",
        normalized_location="Bengaluru",
        remote_type="HYBRID",
        experience_min_years=Decimal("0.0"),
        experience_max_years=Decimal("2.0"),
        salary_min=Decimal("800000.00"),
        salary_max=Decimal("1200000.00"),
        posted_at=now - timedelta(days=1),
        collected_at=now,
        publication_date_corroborated=True,
    )


@pytest.mark.asyncio
async def test_service_from_session_factory():
    """Verify from_session binds repositories to caller session."""
    mock_session = AsyncMock()
    service = JobVerificationService.from_session(mock_session)

    assert isinstance(service.verification_repo, VerificationRecordRepository)
    assert service.verification_repo.session is mock_session
    assert isinstance(service.job_repo, JobRepository)
    assert service.job_repo.session is mock_session
    assert isinstance(service.evaluator, JobVerificationEvaluator)


@pytest.mark.asyncio
async def test_service_evaluate_and_persist_success(base_evidence: JobVerificationInput):
    """Verify end-to-end evaluation and record creation."""
    mock_verification_repo = MagicMock(spec=VerificationRecordRepository)
    mock_job_repo = MagicMock(spec=JobRepository)

    job_id = uuid.uuid4()
    fake_job = MagicMock(spec=Job)
    fake_job.id = job_id
    fake_job.description = SAMPLE_JD

    persisted_rec = VerificationRecord(
        id=uuid.uuid4(),
        job_id=job_id,
        verification_run_at=datetime.now(UTC),
        source_verified=True,
        jd_verified=True,
        application_url_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        posting_date_verified=True,
        verification_confidence=Decimal("1.00"),
        passed=True,
        failure_reasons=None,
        review_reasons=None,
        audit_trail={},
    )

    mock_verification_repo.create = AsyncMock(return_value=persisted_rec)
    mock_job_repo.update_verification_status = AsyncMock(return_value=fake_job)

    service = JobVerificationService(
        verification_record_repository=mock_verification_repo,
        job_repository=mock_job_repo,
    )

    outcome = await service.evaluate_and_persist(fake_job, evidence=base_evidence)

    assert isinstance(outcome, JobVerificationOutcome)
    assert outcome.record is persisted_rec
    assert outcome.result.passed is True
    assert outcome.job is fake_job

    mock_verification_repo.create.assert_awaited_once()
    mock_job_repo.update_verification_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_and_repositories_never_call_session_commit(
    base_evidence: JobVerificationInput,
):
    """Verify that service and repositories never call session.commit()."""
    mock_session = AsyncMock()
    v_repo = VerificationRecordRepository(mock_session)
    j_repo = JobRepository(mock_session)

    service = JobVerificationService(
        verification_record_repository=v_repo,
        job_repository=j_repo,
    )

    job_id = uuid.uuid4()
    fake_job = MagicMock(spec=Job)
    fake_job.id = job_id
    fake_job.description = SAMPLE_JD

    mock_session.add = MagicMock()
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()

    await service.evaluate_and_persist(fake_job, evidence=base_evidence)

    # session.commit() MUST NEVER be called
    mock_session.commit.assert_not_called()
    assert mock_session.flush.await_count >= 1


@pytest.mark.asyncio
async def test_service_missing_job_without_evidence_raises():
    """Verify that evaluating an unresolvable job ID without evidence raises ValueError."""
    mock_verification_repo = MagicMock(spec=VerificationRecordRepository)
    mock_job_repo = MagicMock(spec=JobRepository)
    mock_job_repo.get_by_id = AsyncMock(return_value=None)

    service = JobVerificationService(
        verification_record_repository=mock_verification_repo,
        job_repository=mock_job_repo,
    )

    missing_id = uuid.uuid4()
    with pytest.raises(ValueError, match="Cannot evaluate verification"):
        await service.evaluate_and_persist(missing_id, evidence=None)


@pytest.mark.asyncio
async def test_service_list_history_and_get_latest():
    """Verify history listing and latest query pass-through."""
    mock_verification_repo = MagicMock(spec=VerificationRecordRepository)
    mock_verification_repo.list_by_job_id = AsyncMock(return_value=[])
    mock_verification_repo.get_latest_by_job_id = AsyncMock(return_value=None)

    service = JobVerificationService(
        verification_record_repository=mock_verification_repo,
    )

    job_id = uuid.uuid4()
    history = await service.list_history(job_id)
    latest = await service.get_latest(job_id)

    assert history == []
    assert latest is None
    mock_verification_repo.list_by_job_id.assert_awaited_once_with(job_id, limit=100, offset=0)
    mock_verification_repo.get_latest_by_job_id.assert_awaited_once_with(job_id)
