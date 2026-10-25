"""Unit tests for JobFilterDecisionService and persistence repositories.

Boundary: Offline tests using mocked repositories and AsyncSession.
Validates:
1. Mapping rule result into FilterDecision model fields and audit context.
2. Updating canonical Job status for all 4 operational states (ELIGIBLE, REVIEW, REJECTED, EXPIRED).
3. Preserving distinct role priority and eligibility status (orthogonality).
4. Passing verification state explicitly without deriving from ingestion timestamps.
5. Authoritative VerificationRecord lookup when verification state is unsupplied.
6. Repeated evaluations creating separate historical decisions (preserving audit history).
7. Strict transactional behavior: ensuring zero calls to session.commit().
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.orchestration.decision_service import (
    JobFilterDecisionOutcome,
    JobFilterDecisionService,
)
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.repositories.filter_decision_repository import FilterDecisionRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.rules.engine import JobFilterRuleEngine
from app.rules.schemas import (
    DEFAULT_RULE_VERSION,
    JobFilterResult,
)

T_REF = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


def _make_mock_job(
    *,
    job_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    title: str = "AI Engineer",
    normalized_title: str = "ai engineer",
    location: str | None = "Pune, Maharashtra",
    normalized_location: str | None = "Pune",
    remote_type: str = "ON_SITE",
    experience_raw: str | None = "0-2 years",
    experience_min_years: Decimal | None = Decimal("0.0"),
    experience_max_years: Decimal | None = Decimal("2.0"),
    salary_min: Decimal | None = Decimal("600000.00"),
    salary_max: Decimal | None = Decimal("900000.00"),
    salary_currency: str = "INR",
    salary_period: str = "ANNUAL",
    posted_at: datetime | None = None,
    collected_at: datetime | None = None,
    role_priority: str = "P1",
    relevance_status: str = "TARGET",
    eligibility_status: str = "NEW",
    rejection_reason: str | None = None,
    review_reason: str | None = None,
) -> Job:
    """Create an in-memory Job instance for unit testing."""
    job = Job()
    job.id = job_id or uuid.uuid4()
    job.company_id = company_id or uuid.uuid4()
    job.company_name = "Acme Corp"
    job.title = title
    job.normalized_title = normalized_title
    job.location = location
    job.normalized_location = normalized_location
    job.employment_type = "FULL_TIME"
    job.remote_type = remote_type
    job.description = "Build ML and LLM pipelines using FastAPI and PyTorch."
    job.experience_raw = experience_raw
    job.experience_min_years = experience_min_years
    job.experience_max_years = experience_max_years
    job.salary_min = salary_min
    job.salary_max = salary_max
    job.salary_currency = salary_currency
    job.salary_period = salary_period
    job.posted_at = posted_at or (T_REF - timedelta(days=1))
    job.collected_at = collected_at or T_REF
    job.updated_at = T_REF
    job.role_priority = role_priority
    job.relevance_status = relevance_status
    job.eligibility_status = eligibility_status
    job.rejection_reason = rejection_reason
    job.review_reason = review_reason
    job.source_verified = True
    job.jd_verified = True
    job.company_verified = True
    job.location_verified = True
    job.experience_verified = True
    job.salary_verified = True
    job.verification_confidence = Decimal("1.00")
    return job


def _make_mock_verification_record(
    job_id: uuid.UUID,
    *,
    posting_date_verified: bool = True,
    run_at: datetime | None = None,
) -> VerificationRecord:
    """Create an in-memory VerificationRecord instance for unit testing."""
    rec = VerificationRecord()
    rec.id = uuid.uuid4()
    rec.job_id = job_id
    rec.verification_run_at = run_at or T_REF
    rec.source_verified = True
    rec.jd_verified = True
    rec.application_url_verified = True
    rec.company_verified = True
    rec.location_verified = True
    rec.experience_verified = True
    rec.salary_verified = True
    rec.posting_date_verified = posting_date_verified
    rec.verification_confidence = Decimal("0.95")
    rec.passed = True
    rec.audit_trail = {"source": "unit_test"}
    return rec


# =============================================================================
# 1. Mapping Rule Result into FilterDecision Model & Audit Trail
# =============================================================================


@pytest.mark.asyncio
async def test_mapping_rule_result_into_filter_decision() -> None:
    """Verify JobFilterDecisionService correctly projects JobFilterResult into FilterDecision."""
    job = _make_mock_job(
        title="GenAI Engineer",
        normalized_title="genai engineer",
        location="Bengaluru, Karnataka",
        normalized_location="Bengaluru",
        salary_min=Decimal("800000.00"),
        salary_max=Decimal("1200000.00"),
        posted_at=T_REF - timedelta(days=2),
    )

    mock_session = AsyncMock()
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.session = mock_session
    job_repo.get_by_id.return_value = job

    # When update_status is called, simulate in-place attribute updates
    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.session = mock_session

    async def mock_decision_create(d: FilterDecision) -> FilterDecision:
        return d

    decision_repo.create.side_effect = mock_decision_create

    verif_repo = AsyncMock(spec=VerificationRecordRepository)

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
        verification_record_repository=verif_repo,
    )

    outcome = await service.evaluate_and_persist(
        job.id,
        posting_date_verified=True,
        reference_time=T_REF,
    )

    # 1. Output typing
    assert isinstance(outcome, JobFilterDecisionOutcome)
    assert outcome.job is job
    assert isinstance(outcome.decision, FilterDecision)
    assert isinstance(outcome.result, JobFilterResult)

    # 2. FilterDecision attribute mapping
    decision = outcome.decision
    assert decision.job_id == job.id
    assert decision.rule_version == DEFAULT_RULE_VERSION
    assert decision.final_status == "ELIGIBLE"
    assert decision.role_priority == "P1"
    assert decision.reason_codes == []
    assert isinstance(decision.evaluation_context, dict)
    assert decision.evaluation_context["title"] == "GenAI Engineer"
    assert decision.evaluation_context["matched_role"] == "genai engineer"
    assert decision.evaluation_context["posting_date_verified"] is True
    assert decision.evaluated_at is not None

    # 3. Decision repository was called
    decision_repo.create.assert_awaited_once_with(decision)

    # 4. Job status updates
    assert outcome.job.eligibility_status == "ELIGIBLE"
    assert outcome.job.role_priority == "P1"
    assert outcome.job.relevance_status == "TARGET"
    assert outcome.job.rejection_reason is None
    assert outcome.job.review_reason is None


# =============================================================================
# 2. Updating Job Status Correctly for All 4 Operational Outcomes
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setup_kwargs,expected_status,expected_priority,expected_rej,expected_rev",
    [
        # ELIGIBLE: P1 GenAI Engineer, 0-2y, Pune, ₹6 LPA, posted 1d ago, verified=True
        (
            {
                "title": "GenAI Engineer",
                "normalized_title": "genai engineer",
                "location": "Pune",
                "normalized_location": "Pune",
                "experience_min_years": Decimal("0.0"),
                "experience_max_years": Decimal("2.0"),
                "salary_min": Decimal("600000.00"),
                "salary_max": Decimal("800000.00"),
                "posted_at": T_REF - timedelta(days=1),
                "posting_date_verified": True,
            },
            "ELIGIBLE",
            "P1",
            None,
            None,
        ),
        # REVIEW: Unknown salary on Python developer in Bengaluru (P3 role)
        (
            {
                "title": "Python Developer",
                "normalized_title": "python developer",
                "location": "Bengaluru",
                "normalized_location": "Bengaluru",
                "experience_min_years": Decimal("0.0"),
                "experience_max_years": Decimal("1.0"),
                "salary_min": None,
                "salary_max": None,
                "posted_at": T_REF - timedelta(days=1),
                "posting_date_verified": True,
            },
            "REVIEW",
            "P3",
            None,
            "SALARY_UNKNOWN",
        ),
        # REJECTED: Senior / High experience (3-5 years)
        (
            {
                "title": "Senior AI Engineer",
                "normalized_title": "senior ai engineer",
                "location": "Bengaluru",
                "normalized_location": "Bengaluru",
                "experience_min_years": Decimal("3.0"),
                "experience_max_years": Decimal("5.0"),
                "salary_min": Decimal("1500000.00"),
                "salary_max": Decimal("2500000.00"),
                "posted_at": T_REF - timedelta(days=1),
                "posting_date_verified": True,
            },
            "REJECTED",
            "P1",
            "EXPERIENCE_TOO_HIGH",
            None,
        ),
        # EXPIRED: Posted 8 days ago (> 5 days active window) (P1 role)
        (
            {
                "title": "AI Engineer",
                "normalized_title": "ai engineer",
                "location": "Hyderabad",
                "normalized_location": "Hyderabad",
                "experience_min_years": Decimal("0.0"),
                "experience_max_years": Decimal("2.0"),
                "salary_min": Decimal("600000.00"),
                "salary_max": Decimal("900000.00"),
                "posted_at": T_REF - timedelta(days=8),
                "posting_date_verified": True,
            },
            "EXPIRED",
            "P1",
            "JOB_TOO_OLD",
            None,
        ),
    ],
)
async def test_updating_job_status_correctly_for_all_four_outcomes(
    setup_kwargs: dict[str, Any],
    expected_status: str,
    expected_priority: str,
    expected_rej: str | None,
    expected_rev: str | None,
) -> None:
    """Verify Job operational status fields are correctly set for ELIGIBLE, REVIEW, REJECTED, EXPIRED."""
    p_verif = setup_kwargs.pop("posting_date_verified")
    job = _make_mock_job(**setup_kwargs)

    mock_session = AsyncMock()
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.session = mock_session
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.create.side_effect = lambda d: d

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    outcome = await service.evaluate_and_persist(
        job,
        posting_date_verified=p_verif,
        reference_time=T_REF,
    )

    assert outcome.job.eligibility_status == expected_status
    assert outcome.job.role_priority == expected_priority
    assert outcome.job.rejection_reason == expected_rej
    assert outcome.job.review_reason == expected_rev

    assert outcome.decision.final_status == expected_status
    assert outcome.decision.role_priority == expected_priority


# =============================================================================
# 3. Preserving Distinct Role Priority and Eligibility Status (Orthogonality)
# =============================================================================


@pytest.mark.asyncio
async def test_preserving_distinct_role_priority_and_eligibility_status() -> None:
    """Verify high-priority roles (P1) rejected by experience retain P1 role_priority."""
    # Senior AI Engineer with min 4 years: Role is P1, but eligibility is REJECTED (EXPERIENCE_TOO_HIGH)
    job = _make_mock_job(
        title="Senior AI Engineer",
        normalized_title="senior ai engineer",
        location="Bengaluru",
        normalized_location="Bengaluru",
        experience_min_years=Decimal("4.0"),
        experience_max_years=Decimal("7.0"),
        salary_min=Decimal("2000000.00"),
        salary_max=Decimal("3000000.00"),
        posted_at=T_REF - timedelta(days=1),
    )

    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.create.side_effect = lambda d: d

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    outcome = await service.evaluate_and_persist(
        job,
        posting_date_verified=True,
        reference_time=T_REF,
    )

    # Orthogonality guarantee: role_priority is P1, but eligibility is REJECTED
    assert outcome.job.role_priority == "P1"
    assert outcome.job.eligibility_status == "REJECTED"
    assert outcome.job.rejection_reason == "EXPERIENCE_TOO_HIGH"

    assert outcome.decision.role_priority == "P1"
    assert outcome.decision.final_status == "REJECTED"


# =============================================================================
# 4. Publication-Date Verification Gating & Never Derived from collected_at
# =============================================================================


@pytest.mark.asyncio
async def test_verification_state_never_derived_from_collected_at() -> None:
    """Confirm recent collected_at cannot qualify a job as fresh when publication date is unverified."""
    # Job has recent collected_at (today), but posted_at is None
    job = _make_mock_job(
        title="Backend Engineer",
        normalized_title="backend engineer",
        location="Pune",
        normalized_location="Pune",
        posted_at=None,
        collected_at=T_REF,
    )

    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.create.side_effect = lambda d: d

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    # When posting_date_verified is None (unresolved), it must route to REVIEW
    outcome = await service.evaluate_and_persist(
        job,
        posting_date_verified=None,
        reference_time=T_REF,
    )

    assert outcome.job.eligibility_status == "REVIEW"
    assert outcome.job.review_reason == "POSTED_DATE_SUSPICIOUS"
    assert outcome.decision.final_status == "REVIEW"
    assert "POSTED_DATE_SUSPICIOUS" in outcome.decision.reason_codes


@pytest.mark.asyncio
async def test_publication_date_unverified_flag_routes_to_review() -> None:
    """Confirm posting_date_verified=False explicitly routes to REVIEW even with fresh posted_at."""
    job = _make_mock_job(
        title="AI Engineer",
        normalized_title="ai engineer",
        location="Bengaluru",
        normalized_location="Bengaluru",
        salary_min=Decimal("800000.00"),
        salary_max=Decimal("1200000.00"),
        posted_at=T_REF - timedelta(days=1),  # Stated as 1 day ago
    )

    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.create.side_effect = lambda d: d

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    # Explicit False verification
    outcome = await service.evaluate_and_persist(
        job,
        posting_date_verified=False,
        reference_time=T_REF,
    )

    assert outcome.job.eligibility_status == "REVIEW"
    assert outcome.job.review_reason == "POSTED_DATE_SUSPICIOUS"
    assert outcome.decision.final_status == "REVIEW"


# =============================================================================
# 5. Authoritative VerificationRecord Lookup
# =============================================================================


@pytest.mark.asyncio
async def test_authoritative_verification_record_lookup_when_unsupplied() -> None:
    """Verify service resolves posting_date_verified from VerificationRecordRepository when unsupplied."""
    job = _make_mock_job(
        title="AI Engineer",
        normalized_title="ai engineer",
        location="Bengaluru",
        normalized_location="Bengaluru",
        salary_min=Decimal("800000.00"),
        salary_max=Decimal("1200000.00"),
        posted_at=T_REF - timedelta(days=1),
    )

    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_repo = AsyncMock(spec=FilterDecisionRepository)
    decision_repo.create.side_effect = lambda d: d

    verif_repo = AsyncMock(spec=VerificationRecordRepository)

    # 1. Verification record exists with posting_date_verified = True
    v_record_true = _make_mock_verification_record(job.id, posting_date_verified=True)
    verif_repo.get_latest_by_job_id.return_value = v_record_true

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
        verification_record_repository=verif_repo,
    )

    # Do not supply posting_date_verified (_UNSET default)
    outcome_verified = await service.evaluate_and_persist(job, reference_time=T_REF)
    assert outcome_verified.job.eligibility_status == "ELIGIBLE"
    assert outcome_verified.decision.final_status == "ELIGIBLE"

    # 2. Verification record exists with posting_date_verified = False
    v_record_false = _make_mock_verification_record(job.id, posting_date_verified=False)
    verif_repo.get_latest_by_job_id.return_value = v_record_false

    outcome_failed = await service.evaluate_and_persist(job, reference_time=T_REF)
    assert outcome_failed.job.eligibility_status == "REVIEW"
    assert outcome_failed.job.review_reason == "POSTED_DATE_SUSPICIOUS"

    # 3. No verification record exists (returns None) -> unresolved -> REVIEW
    verif_repo.get_latest_by_job_id.return_value = None

    outcome_unresolved = await service.evaluate_and_persist(job, reference_time=T_REF)
    assert outcome_unresolved.job.eligibility_status == "REVIEW"
    assert outcome_unresolved.job.review_reason == "POSTED_DATE_SUSPICIOUS"


# =============================================================================
# 6. Repeated Evaluations Creating Separate Historical Decisions
# =============================================================================


@pytest.mark.asyncio
async def test_repeated_evaluations_create_separate_historical_decisions() -> None:
    """Verify multiple evaluations of the same job append new FilterDecision rows without overwriting."""
    job = _make_mock_job(
        title="AI Engineer",
        normalized_title="ai engineer",
        location="Pune",
        normalized_location="Pune",
        posted_at=T_REF - timedelta(days=1),
    )

    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = job

    async def mock_update_status(j: Job, updates: dict[str, Any]) -> Job:
        for k, v in updates.items():
            setattr(j, k, v)
        return j

    job_repo.update_status.side_effect = mock_update_status

    decision_history: list[FilterDecision] = []
    decision_repo = AsyncMock(spec=FilterDecisionRepository)

    async def mock_create_decision(d: FilterDecision) -> FilterDecision:
        decision_history.append(d)
        return d

    decision_repo.create.side_effect = mock_create_decision

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    # First evaluation at T_REF: verified True -> ELIGIBLE
    outcome_1 = await service.evaluate_and_persist(
        job,
        posting_date_verified=True,
        reference_time=T_REF,
    )

    # Second evaluation at T_REF + 10 days: verified True -> EXPIRED (JOB_TOO_OLD)
    outcome_2 = await service.evaluate_and_persist(
        job,
        posting_date_verified=True,
        reference_time=T_REF + timedelta(days=10),
    )

    assert len(decision_history) == 2
    assert decision_history[0] is outcome_1.decision
    assert decision_history[1] is outcome_2.decision

    assert outcome_1.decision.final_status == "ELIGIBLE"
    assert outcome_2.decision.final_status == "EXPIRED"

    # Distinct record identity and timestamps
    assert outcome_1.decision is not outcome_2.decision
    assert outcome_2.job.eligibility_status == "EXPIRED"


# =============================================================================
# 7. Strict Transactional Boundary: Zero Calls to session.commit()
# =============================================================================


@pytest.mark.asyncio
async def test_service_and_repositories_never_call_session_commit() -> None:
    """Verify that neither repository operations nor service orchestration invoke session.commit()."""
    mock_session = AsyncMock()
    mock_session.add = MagicMock()

    job = _make_mock_job()
    job_repo = JobRepository(mock_session)
    decision_repo = FilterDecisionRepository(mock_session)
    verif_repo = VerificationRecordRepository(mock_session)

    # Pre-mock session methods
    mock_session.get.return_value = job
    mock_session.execute.return_value.scalar_one_or_none.return_value = None

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
        verification_record_repository=verif_repo,
    )

    outcome = await service.evaluate_and_persist(
        job.id,
        posting_date_verified=True,
        reference_time=T_REF,
    )

    assert outcome.job.eligibility_status == "ELIGIBLE"

    # Confirm session.flush was called to surface database constraints
    assert mock_session.flush.call_count >= 1

    # STRICT ASSERTION: session.commit must NEVER be called
    assert mock_session.commit.call_count == 0


# =============================================================================
# 8. Error Handling: Non-Existent Job ID
# =============================================================================


@pytest.mark.asyncio
async def test_job_not_found_raises_value_error() -> None:
    """Verify attempting to evaluate a non-existent job UUID raises ValueError."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_id.return_value = None

    decision_repo = AsyncMock(spec=FilterDecisionRepository)

    service = JobFilterDecisionService(
        job_repository=job_repo,
        filter_decision_repository=decision_repo,
    )

    missing_uuid = uuid.uuid4()
    with pytest.raises(ValueError, match=f"Job with id {missing_uuid} not found"):
        await service.evaluate_and_persist(missing_uuid)


# =============================================================================
# 9. Factory Method and Dependency Injection
# =============================================================================


def test_from_session_factory_initialization() -> None:
    """Verify JobFilterDecisionService.from_session initializes bound repositories properly."""
    mock_session = MagicMock()
    custom_engine = JobFilterRuleEngine(rule_version="V1.1-custom")

    service = JobFilterDecisionService.from_session(
        mock_session,
        rule_engine=custom_engine,
    )

    assert isinstance(service.job_repo, JobRepository)
    assert service.job_repo.session is mock_session
    assert isinstance(service.decision_repo, FilterDecisionRepository)
    assert service.decision_repo.session is mock_session
    assert isinstance(service.verification_repo, VerificationRecordRepository)
    assert service.verification_repo.session is mock_session
    assert service.rule_engine is custom_engine


# =============================================================================
# 10. Rules Engine Independence from Database Layer
# =============================================================================


def test_pure_rules_engine_imports_without_database_modules() -> None:
    """Verify that importing app.rules does not expose or import database dependencies."""
    import app.rules

    # Ensure app.rules exports pure rule engine symbols
    rules_symbols = dir(app.rules)
    assert "JobFilterRuleEngine" in rules_symbols
    assert "evaluate_job_filter" in rules_symbols
    assert "JobFilterInput" in rules_symbols
    assert "JobFilterResult" in rules_symbols

    # Ensure orchestration/database service is NOT exported from app.rules
    assert "JobFilterDecisionService" not in rules_symbols
    assert "JobFilterDecisionOutcome" not in rules_symbols
    assert not hasattr(app.rules, "session")
    assert not hasattr(app.rules, "JobRepository")
    assert not hasattr(app.rules, "FilterDecisionRepository")
