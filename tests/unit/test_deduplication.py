"""Unit tests for deterministic three-stage job deduplication service.

Validates:
1. Deduplication schemas, result properties, and candidate factories.
2. Pure in-memory matching helpers and 14-day publication date proximity logic.
3. Seniority preservation in Stage 3 title comparisons.
4. Stage 1 exact source external ID lookups and platform namespace isolation.
5. Stage 2 ATS URL normalization, parameter stripping, and multi-match conflict detection.
6. Stage 3 composite matching, missing date / ambiguous location handling, and multi-match conflicts.
7. Stage priority order and machine-readable evidence audit trail.
8. Provenance link creation without transaction commit.

Zero live database dependency: 100% offline async unit tests.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.deduplication.matching import (
    STAGE_3_MAX_DAYS_WINDOW,
    check_date_proximity,
    evaluate_stage_3_candidate,
    normalize_datetime_tz,
)
from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationCandidate,
    DeduplicationResult,
    DeduplicationStage,
)
from app.deduplication.service import JobDeduplicationService
from app.normalization.schemas import NormalizedJobData
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)


def _make_mock_job(
    *,
    job_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    company_name: str = "Test Company",
    title: str = "Software Engineer",
    normalized_title: str = "software engineer",
    location: str | None = "Mumbai, Maharashtra, India",
    normalized_location: str | None = "mumbai",
    posted_at: datetime | None = None,
    application_url: str | None = "https://boards.greenhouse.io/acme/jobs/123",
) -> Job:
    """Helper creating a detached Job model instance for unit tests."""
    return Job(
        id=job_id or uuid.uuid4(),
        company_id=company_id or uuid.uuid4(),
        company_name=company_name,
        title=title,
        normalized_title=normalized_title,
        location=location,
        normalized_location=normalized_location,
        description="Write high-performance backend systems.",
        role_priority="P1",
        relevance_status="RELEVANT",
        eligibility_status="NEW",
        posted_at=posted_at or datetime(2026, 10, 10, 12, 0, tzinfo=UTC),
        application_url=application_url,
    )


# =============================================================================
# 1. Schemas & Factories
# =============================================================================


def test_deduplication_result_properties() -> None:
    """Verify DeduplicationResult convenience boolean properties."""
    matched = DeduplicationResult(
        action=DeduplicationAction.MATCH,
        stage=DeduplicationStage.STAGE_1_EXACT_SOURCE_ID,
        confidence=1.0,
        canonical_job_id=uuid.uuid4(),
    )
    assert matched.is_duplicate is True
    assert matched.is_ambiguous is False
    assert matched.is_new is False

    ambiguous = DeduplicationResult(
        action=DeduplicationAction.AMBIGUOUS,
        stage=DeduplicationStage.STAGE_3_COMPOSITE,
        confidence=0.90,
        review_reason="DUPLICATE_AMBIGUOUS",
    )
    assert ambiguous.is_duplicate is False
    assert ambiguous.is_ambiguous is True
    assert ambiguous.is_new is False

    new_job = DeduplicationResult(
        action=DeduplicationAction.NEW,
        stage=DeduplicationStage.NONE,
        confidence=0.0,
    )
    assert new_job.is_duplicate is False
    assert new_job.is_ambiguous is False
    assert new_job.is_new is True


def test_candidate_from_normalized_factory() -> None:
    """Verify DeduplicationCandidate.from_normalized creates candidate properly."""
    norm = NormalizedJobData(
        title="AI Engineer",
        normalized_title="ai engineer",
        company_name="Tech Corp",
        location="Bengaluru",
        normalized_location="bengaluru",
        description="Build LLM applications.",
        application_url="https://jobs.lever.co/techcorp/456",
        posted_at=datetime(2026, 10, 15, 10, 0, tzinfo=UTC),
    )
    source_id = uuid.uuid4()
    company_id = uuid.uuid4()
    raw_job_id = uuid.uuid4()

    candidate = DeduplicationCandidate.from_normalized(
        norm,
        source_id=source_id,
        source_job_id="lever_456",
        source_url="https://jobs.lever.co/techcorp/456",
        company_id=company_id,
        raw_job_id=raw_job_id,
        source_job_id_type="NATIVE",
    )

    assert candidate.source_id == source_id
    assert candidate.source_job_id == "lever_456"
    assert candidate.company_id == company_id
    assert candidate.company_name == "Tech Corp"
    assert candidate.normalized_title == "ai engineer"
    assert candidate.normalized_location == "bengaluru"
    assert candidate.raw_job_id == raw_job_id
    assert candidate.application_url == "https://jobs.lever.co/techcorp/456"


# =============================================================================
# 2. Pure In-Memory Matching & Date Proximity
# =============================================================================


def test_normalize_datetime_tz() -> None:
    """Verify timezone normalization handles naive and aware datetimes."""
    assert normalize_datetime_tz(None) is None

    naive = datetime(2026, 10, 10, 12, 0)
    aware = normalize_datetime_tz(naive)
    assert aware is not None
    assert aware.tzinfo == UTC

    already_aware = datetime(2026, 10, 10, 17, 30, tzinfo=UTC)
    normalized = normalize_datetime_tz(already_aware)
    assert normalized == already_aware


def test_check_date_proximity() -> None:
    """Verify the 14-day publication date proximity window logic."""
    assert STAGE_3_MAX_DAYS_WINDOW == 14
    base = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)

    # Identical date
    assert check_date_proximity(base, base) is True

    # 10 days difference (within 14 days)
    assert check_date_proximity(base, base + timedelta(days=10)) is True
    assert check_date_proximity(base, base - timedelta(days=10)) is True

    # Exactly 14 days difference (boundary inclusive)
    assert check_date_proximity(base, base + timedelta(days=14)) is True
    assert check_date_proximity(base, base - timedelta(days=14)) is True

    # 14 days and 1 second difference (strictly exceeds 14 days)
    assert check_date_proximity(base, base + timedelta(days=14, seconds=1)) is False
    assert check_date_proximity(base, base - timedelta(days=14, seconds=1)) is False

    # 14 days and 1 hour (strictly exceeds 14 days)
    assert check_date_proximity(base, base + timedelta(days=14, hours=1)) is False
    assert check_date_proximity(base, base - timedelta(days=14, hours=1)) is False

    # 15 days difference
    assert check_date_proximity(base, base + timedelta(days=15)) is False
    assert check_date_proximity(base, base - timedelta(days=15)) is False

    # 30 days difference
    assert check_date_proximity(base, base + timedelta(days=30)) is False

    # Future-dated candidate (e.g. 25 days into the future)
    assert check_date_proximity(base + timedelta(days=25), base) is False

    # Naive versus timezone-aware timestamps (handled without TypeError)
    naive_date = datetime(2026, 10, 10, 12, 0)
    aware_date = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    assert check_date_proximity(naive_date, aware_date) is True
    assert check_date_proximity(aware_date, naive_date) is True

    naive_far = datetime(2026, 9, 10, 12, 0)
    assert check_date_proximity(naive_far, aware_date) is False
    assert check_date_proximity(aware_date, naive_far) is False

    # Indeterminate cases (missing timestamps)
    assert check_date_proximity(base, None) is None
    assert check_date_proximity(None, base) is None
    assert check_date_proximity(None, None) is None


def test_evaluate_stage_3_candidate_matrix() -> None:
    """Test full matrix of Stage 3 candidate evaluations in-memory."""
    company_id = uuid.uuid4()
    base_date = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    cand = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="test_1",
        source_url="https://example.com/job/1",
        company_id=company_id,
        company_name="Acme",
        title="Senior AI Engineer",
        normalized_title="senior ai engineer",
        location="Bengaluru, India",
        normalized_location="bengaluru",
        posted_at=base_date,
    )

    # 1. Exact match across all 4 criteria
    matched_job = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date + timedelta(days=5),
    )
    status, ev = evaluate_stage_3_candidate(cand, matched_job)
    assert status == "MATCH"
    assert ev["reason"] == "all_criteria_satisfied"

    # 2. Company ID mismatch
    other_company_job = _make_mock_job(
        company_id=uuid.uuid4(),
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date,
    )
    status, ev = evaluate_stage_3_candidate(cand, other_company_job)
    assert status == "MISMATCH"
    assert ev["reason"] == "company_id_mismatch"

    # 3. Seniority preservation: "Senior AI Engineer" vs "Junior AI Engineer"
    junior_job = _make_mock_job(
        company_id=company_id,
        normalized_title="junior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date,
    )
    status, ev = evaluate_stage_3_candidate(cand, junior_job)
    assert status == "MISMATCH"
    assert ev["reason"] == "normalized_title_mismatch"

    # 4. Location mismatch: "bengaluru" vs "mumbai"
    mumbai_job = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="mumbai",
        posted_at=base_date,
    )
    status, ev = evaluate_stage_3_candidate(cand, mumbai_job)
    assert status == "MISMATCH"
    assert ev["reason"] == "location_mismatch"

    # 5. Date exceeds 14-day window (25 days delta)
    old_job = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date - timedelta(days=25),
    )
    status, ev = evaluate_stage_3_candidate(cand, old_job)
    assert status == "MISMATCH"
    assert ev["reason"] == "date_exceeds_window"

    # 6. Missing publication date on candidate
    cand_no_date = cand.model_copy(update={"posted_at": None})
    status, ev = evaluate_stage_3_candidate(cand_no_date, matched_job)
    assert status == "AMBIGUOUS"
    assert ev["reason"] == "missing_publication_date"

    # 7. Missing publication date on existing job
    job_no_date = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=None,
    )
    job_no_date.posted_at = None
    status, ev = evaluate_stage_3_candidate(cand, job_no_date)
    assert status == "AMBIGUOUS"
    assert ev["reason"] == "missing_publication_date"

    # 8. Ambiguous / unresolvable location on incoming candidate
    cand_no_loc = cand.model_copy(update={"normalized_location": None})
    status, ev = evaluate_stage_3_candidate(cand_no_loc, matched_job)
    assert status == "AMBIGUOUS"
    assert ev["reason"] == "ambiguous_location"

    # 9. Boundary: Exactly 14 days earlier
    job_14d_earlier = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date - timedelta(days=14),
    )
    status, ev = evaluate_stage_3_candidate(cand, job_14d_earlier)
    assert status == "MATCH"
    assert ev["reason"] == "all_criteria_satisfied"

    # 10. Boundary: Exactly 14 days later
    job_14d_later = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date + timedelta(days=14),
    )
    status, ev = evaluate_stage_3_candidate(cand, job_14d_later)
    assert status == "MATCH"
    assert ev["reason"] == "all_criteria_satisfied"

    # 11. Boundary: 14 days and 1 second earlier (> 14 days window)
    job_14d_1s_earlier = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date - timedelta(days=14, seconds=1),
    )
    status, ev = evaluate_stage_3_candidate(cand, job_14d_1s_earlier)
    assert status == "MISMATCH"
    assert ev["reason"] == "date_exceeds_window"

    # 12. Boundary: 14 days and 1 second later (> 14 days window)
    job_14d_1s_later = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date + timedelta(days=14, seconds=1),
    )
    status, ev = evaluate_stage_3_candidate(cand, job_14d_1s_later)
    assert status == "MISMATCH"
    assert ev["reason"] == "date_exceeds_window"

    # 13. Future-dated candidate (> 14 days delta cannot bypass check)
    job_future = _make_mock_job(
        company_id=company_id,
        normalized_title="senior ai engineer",
        normalized_location="bengaluru",
        posted_at=base_date + timedelta(days=35),
    )
    status, ev = evaluate_stage_3_candidate(cand, job_future)
    assert status == "MISMATCH"
    assert ev["reason"] == "date_exceeds_window"

    # 14. Naive timestamp on incoming candidate normalized to UTC without error
    cand_naive = cand.model_copy(update={"posted_at": datetime(2026, 10, 10, 12, 0)})
    status, ev = evaluate_stage_3_candidate(cand_naive, matched_job)
    assert status == "MATCH"
    assert ev["reason"] == "all_criteria_satisfied"


# =============================================================================
# 3. Stage 1 Deduplication (Exact Platform External ID)
# =============================================================================


@pytest.mark.asyncio
async def test_stage_1_exact_source_id_match() -> None:
    """Verify Stage 1 matches on identical source_id and source_job_id."""
    job_repo = AsyncMock(spec=JobRepository)
    jsr_repo = AsyncMock(spec=JobSourceReferenceRepository)

    existing_job = _make_mock_job()
    job_repo.get_by_source_reference.return_value = existing_job

    service = JobDeduplicationService(
        job_repository=job_repo,
        job_source_reference_repository=jsr_repo,
    )

    source_id = uuid.uuid4()
    candidate = DeduplicationCandidate(
        source_id=source_id,
        source_job_id="gh_98765",
        source_job_id_type="NATIVE",
        source_url="https://boards.greenhouse.io/acme/jobs/98765",
        application_url="https://boards.greenhouse.io/acme/jobs/98765",
        company_id=existing_job.company_id,
        company_name="Acme",
        title="Backend Engineer",
        normalized_title="backend engineer",
        location="Bengaluru",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_1_EXACT_SOURCE_ID
    assert result.confidence == 1.0
    assert result.canonical_job_id == existing_job.id
    assert result.matched_job is existing_job
    assert result.is_duplicate is True
    assert result.evidence["stage_1"]["match"] is True

    # Verification: Stage 2 and Stage 3 queries must not have been executed
    job_repo.find_all_by_application_url.assert_not_called()
    job_repo.find_by_composite_attributes.assert_not_called()


@pytest.mark.asyncio
async def test_stage_1_generated_hash_identifier_match() -> None:
    """Verify Stage 1 matches on deterministic generated hash identifiers (gen_<sha256>)."""
    job_repo = AsyncMock(spec=JobRepository)
    existing_job = _make_mock_job()
    job_repo.get_by_source_reference.return_value = existing_job

    service = JobDeduplicationService(job_repository=job_repo)
    source_id = uuid.uuid4()
    gen_hash = "gen_a1b2c3d4e5f67890123456789012345678901234567890123456789012345678"

    candidate = DeduplicationCandidate(
        source_id=source_id,
        source_job_id=gen_hash,
        source_job_id_type="GENERATED",
        source_url="https://acme.com/careers/lead-python",
        company_id=existing_job.company_id,
        company_name="Acme",
        title="Lead Python Developer",
        normalized_title="lead python engineer",
    )

    result = await service.deduplicate(candidate)
    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_1_EXACT_SOURCE_ID
    assert result.confidence == 1.0
    assert result.canonical_job_id == existing_job.id


@pytest.mark.asyncio
async def test_stage_1_different_source_job_id_type_same_identifier_matches() -> None:
    """Verify Stage 1 matches on (source_id, source_job_id) even if source_job_id_type differs."""
    job_repo = AsyncMock(spec=JobRepository)
    existing_job = _make_mock_job()
    job_repo.get_by_source_reference.return_value = existing_job

    service = JobDeduplicationService(job_repository=job_repo)
    source_id = uuid.uuid4()
    job_identifier = "job_native_123"

    # Incoming candidate specifies GENERATED while existing was NATIVE
    candidate = DeduplicationCandidate(
        source_id=source_id,
        source_job_id=job_identifier,
        source_job_id_type="GENERATED",
        source_url="https://acme.com/jobs/123",
        company_id=existing_job.company_id,
        company_name="Acme",
        title="ML Engineer",
        normalized_title="machine learning engineer",
    )

    result = await service.deduplicate(candidate)
    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_1_EXACT_SOURCE_ID
    assert result.confidence == 1.0
    assert result.canonical_job_id == existing_job.id
    job_repo.get_by_source_reference.assert_awaited_once_with(
        source_id=source_id,
        source_job_id=job_identifier,
    )


@pytest.mark.asyncio
async def test_stage_1_source_namespace_isolation() -> None:
    """Verify identical source_job_id from different sources does not match in Stage 1."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None  # Different source namespace
    job_repo.find_all_by_application_url.return_value = []
    job_repo.find_by_composite_attributes.return_value = []

    service = JobDeduplicationService(job_repository=job_repo)

    source_a = uuid.uuid4()
    candidate = DeduplicationCandidate(
        source_id=source_a,
        source_job_id="12345",  # Same string, different source namespace
        source_url="https://naukri.com/job/12345",
        company_id=uuid.uuid4(),
        company_name="Acme",
        title="ML Engineer",
        normalized_title="machine learning engineer",
    )

    result = await service.deduplicate(candidate)
    # Since get_by_source_reference returned None, Stage 1 missed
    job_repo.get_by_source_reference.assert_awaited_once_with(
        source_id=source_a,
        source_job_id="12345",
    )
    assert result.stage != DeduplicationStage.STAGE_1_EXACT_SOURCE_ID


# =============================================================================
# 4. Stage 2 Deduplication (Exact Normalized ATS URL)
# =============================================================================


@pytest.mark.asyncio
async def test_stage_2_exact_normalized_url_match() -> None:
    """Verify Stage 2 matches when application ATS URLs match after normalization."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None  # Stage 1 miss

    existing_job = _make_mock_job(
        application_url="https://jobs.lever.co/techcorp/abc-123",
    )
    job_repo.find_all_by_application_url.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    # Candidate has tracking parameters that must be stripped before lookup
    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="different_source_job_id",
        source_url="https://linkedin.com/jobs/view/999",
        application_url="https://jobs.lever.co/techcorp/abc-123?utm_source=linkedin&utm_campaign=summer26&lever-source=ad",
        company_id=existing_job.company_id,
        company_name="TechCorp",
        title="Python Engineer",
        normalized_title="python engineer",
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_2_EXACT_URL
    assert result.confidence == 0.98
    assert result.canonical_job_id == existing_job.id
    assert result.matched_job is existing_job

    # Verify repository was queried with the cleaned canonical URL
    job_repo.find_all_by_application_url.assert_awaited_once_with(
        "https://jobs.lever.co/techcorp/abc-123"
    )
    # Stage 3 must not have been evaluated
    job_repo.find_by_composite_attributes.assert_not_called()


@pytest.mark.asyncio
async def test_stage_2_preserves_meaningful_parameters() -> None:
    """Verify Stage 2 preserves essential tokens and job IDs (gh_jid, token, id)."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None

    existing_job = _make_mock_job(
        application_url="https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=445566",
    )
    job_repo.find_all_by_application_url.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="indeed_job_1",
        source_url="https://indeed.com/viewjob?jk=1",
        application_url="https://boards.greenhouse.io/embed/job_app?gh_jid=445566&for=acme&utm_source=indeed",
        company_id=existing_job.company_id,
        company_name="Acme",
        title="AI Engineer",
        normalized_title="ai engineer",
    )

    result = await service.deduplicate(candidate)
    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_2_EXACT_URL
    # Query must preserve gh_jid and for parameters (sorted alphabetically by normalize_url)
    called_url = job_repo.find_all_by_application_url.call_args[0][0]
    assert "gh_jid=445566" in called_url
    assert "for=acme" in called_url
    assert "utm_source" not in called_url


@pytest.mark.asyncio
async def test_stage_2_conflicting_multiple_matches_flagged_ambiguous() -> None:
    """Verify that multiple canonical jobs sharing the same application URL are flagged AMBIGUOUS."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None

    job_a = _make_mock_job(title="Engineer A")
    job_b = _make_mock_job(title="Engineer B")
    # Return two distinct canonical jobs for this URL
    job_repo.find_all_by_application_url.return_value = [job_a, job_b]

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="source_job_x",
        source_url="https://example.com/job/x",
        application_url="https://jobs.lever.co/company/shared-posting",
        company_id=job_a.company_id,
        company_name="Company",
        title="Software Engineer",
        normalized_title="software engineer",
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_2_EXACT_URL
    assert result.confidence == 0.98
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert set(result.candidate_job_ids) == {job_a.id, job_b.id}
    assert result.is_ambiguous is True

    # Execution must stop at Stage 2 conflict without evaluating Stage 3
    job_repo.find_by_composite_attributes.assert_not_called()


# =============================================================================
# 5. Stage 3 Deduplication (Composite Deterministic Match)
# =============================================================================


@pytest.mark.asyncio
async def test_stage_3_decisive_composite_match() -> None:
    """Verify Stage 3 matches on conjunction of Company + Title + Location + Date proximity <= 14d."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    existing_job = _make_mock_job(
        company_id=company_id,
        normalized_title="generative ai engineer",
        normalized_location="mumbai",
        posted_at=datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    # Incoming candidate posted 6 days later (within 14 days)
    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_cutshort_101",
        source_url="https://cutshort.io/job/101",
        company_id=company_id,
        company_name="Acme AI",
        title="Generative AI Engineer",
        normalized_title="generative ai engineer",
        location="Mumbai, Maharashtra",
        normalized_location="mumbai",
        posted_at=datetime(2026, 10, 11, 14, 0, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.MATCH
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.canonical_job_id == existing_job.id
    assert result.matched_job is existing_job
    assert result.evidence["stage_3"]["match"] is True


@pytest.mark.asyncio
async def test_stage_3_rejects_candidate_outside_14_day_window() -> None:
    """Verify Stage 3 rejects candidates whose posting date delta strictly exceeds 14 days."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    # Existing job posted 25 days earlier
    existing_job = _make_mock_job(
        company_id=company_id,
        normalized_title="backend developer",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_fresh_1",
        source_url="https://linkedin.com/job/fresh",
        company_id=company_id,
        company_name="Acme AI",
        title="Backend Developer",
        normalized_title="backend developer",
        location="Bengaluru",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 5, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    # Because delta > 14 days, existing job is rejected; candidate is classified as NEW
    assert result.action == DeduplicationAction.NEW
    assert result.stage == DeduplicationStage.NONE
    assert result.confidence == 0.0
    assert result.canonical_job_id is None
    assert result.is_new is True


@pytest.mark.asyncio
async def test_stage_3_flags_ambiguous_when_date_missing() -> None:
    """Verify Stage 3 flags DUPLICATE_AMBIGUOUS when publication date is missing on either record."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    existing_job = _make_mock_job(
        company_id=company_id,
        normalized_title="software engineer",
        normalized_location="pune",
        posted_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    # Incoming candidate matches company, title, location, but posted_at is NULL
    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_no_date",
        source_url="https://naukri.com/job/no_date",
        company_id=company_id,
        company_name="Acme",
        title="Software Engineer",
        normalized_title="software engineer",
        location="Pune",
        normalized_location="pune",
        posted_at=None,
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert result.candidate_job_ids == [existing_job.id]


@pytest.mark.asyncio
async def test_stage_3_flags_ambiguous_when_location_unresolvable() -> None:
    """Verify Stage 3 flags DUPLICATE_AMBIGUOUS when normalized_location is NULL."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    existing_job = _make_mock_job(
        company_id=company_id,
        normalized_title="data scientist",
        normalized_location="hyderabad",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [existing_job]

    service = JobDeduplicationService(job_repository=job_repo)

    # Candidate has company, title, within 14d, but normalized_location is None
    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_unresolvable_loc",
        source_url="https://indeed.com/job/unknown_city",
        company_id=company_id,
        company_name="Acme",
        title="Data Scientist",
        normalized_title="data scientist",
        location="Remote/India",
        normalized_location=None,  # Unresolvable location
        posted_at=datetime(2026, 10, 12, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert result.candidate_job_ids == [existing_job.id]


@pytest.mark.asyncio
async def test_stage_3_multiple_matches_flagged_ambiguous() -> None:
    """Verify Stage 3 flags DUPLICATE_AMBIGUOUS if multiple active postings match."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    job_1 = _make_mock_job(
        company_id=company_id,
        normalized_title="ai engineer",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 8, tzinfo=UTC),
    )
    job_2 = _make_mock_job(
        company_id=company_id,
        normalized_title="ai engineer",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 11, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [job_1, job_2]

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_dup",
        source_url="https://cutshort.io/job/ai",
        company_id=company_id,
        company_name="Acme",
        title="AI Engineer",
        normalized_title="ai engineer",
        location="Bengaluru",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert set(result.candidate_job_ids) == {job_1.id, job_2.id}


@pytest.mark.asyncio
async def test_stage_3_never_merges_senior_and_junior_roles() -> None:
    """Verify Stage 3 never merges distinct seniority levels (Senior vs Junior)."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    # Repository query for "junior machine learning engineer" returns empty list because
    # existing job in database is "senior machine learning engineer"
    job_repo.find_by_composite_attributes.return_value = []

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_junior_ml",
        source_url="https://company.com/job/junior",
        company_id=company_id,
        company_name="Acme AI",
        title="Junior Machine Learning Engineer",
        normalized_title="junior machine learning engineer",
        location="Bengaluru",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)
    assert result.action == DeduplicationAction.NEW
    assert result.stage == DeduplicationStage.NONE
    assert result.canonical_job_id is None
    job_repo.find_by_composite_attributes.assert_awaited_once_with(
        company_id=company_id,
        normalized_title="junior machine learning engineer",
    )


@pytest.mark.asyncio
async def test_stage_3_known_different_locations_do_not_match_when_one_unknown() -> None:
    """Verify known different locations do not become matches because incoming location is unknown."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    # Company has two existing jobs: one in Mumbai and one in Bengaluru
    job_mumbai = _make_mock_job(
        company_id=company_id,
        normalized_title="software engineer",
        normalized_location="mumbai",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    job_blr = _make_mock_job(
        company_id=company_id,
        normalized_title="software engineer",
        normalized_location="bengaluru",
        posted_at=datetime(2026, 10, 11, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [job_mumbai, job_blr]

    service = JobDeduplicationService(job_repository=job_repo)

    # Incoming candidate has unknown location
    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_unknown_loc",
        source_url="https://company.com/job/unknown",
        company_id=company_id,
        company_name="Acme",
        title="Software Engineer",
        normalized_title="software engineer",
        location=None,
        normalized_location=None,  # Location unknown
        posted_at=datetime(2026, 10, 12, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    # Neither known location may be silently merged into; must route to AMBIGUOUS
    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert set(result.candidate_job_ids) == {job_mumbai.id, job_blr.id}


@pytest.mark.asyncio
async def test_stage_3_one_exact_match_plus_one_ambiguous_candidate_flags_ambiguous() -> None:
    """Verify that an exact Stage 3 candidate combined with an ambiguous candidate flags AMBIGUOUS."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []

    company_id = uuid.uuid4()
    job_exact = _make_mock_job(
        company_id=company_id,
        normalized_title="platform engineer",
        normalized_location="pune",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    job_ambiguous = _make_mock_job(
        company_id=company_id,
        normalized_title="platform engineer",
        normalized_location=None,  # Ambiguous location
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    job_repo.find_by_composite_attributes.return_value = [job_exact, job_ambiguous]

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="job_exact_plus_amb",
        source_url="https://company.com/job/pune",
        company_id=company_id,
        company_name="Acme",
        title="Platform Engineer",
        normalized_title="platform engineer",
        location="Pune",
        normalized_location="pune",
        posted_at=datetime(2026, 10, 12, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    # Must flag AMBIGUOUS to prevent collision with ambiguous posting
    assert result.action == DeduplicationAction.AMBIGUOUS
    assert result.stage == DeduplicationStage.STAGE_3_COMPOSITE
    assert result.confidence == 0.90
    assert result.review_reason == "DUPLICATE_AMBIGUOUS"
    assert set(result.candidate_job_ids) == {job_exact.id, job_ambiguous.id}


@pytest.mark.asyncio
async def test_stage_priority_order_preservation() -> None:
    """Verify that earlier stage matches halt downstream evaluations."""
    job_repo = AsyncMock(spec=JobRepository)
    job_a = _make_mock_job()
    job_repo.get_by_source_reference.return_value = job_a

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="priority_test_job",
        source_url="https://company.com/job/1",
        application_url="https://boards.greenhouse.io/acme/1",
        company_id=job_a.company_id,
        company_name="Acme",
        title="AI Engineer",
        normalized_title="ai engineer",
    )

    res = await service.deduplicate(candidate)
    assert res.stage == DeduplicationStage.STAGE_1_EXACT_SOURCE_ID
    # Stage 2 and Stage 3 must not be queried
    job_repo.find_all_by_application_url.assert_not_called()
    job_repo.find_by_composite_attributes.assert_not_called()


# =============================================================================
# 6. Fall-Through to New Job & Provenance Recording
# =============================================================================


@pytest.mark.asyncio
async def test_no_match_returns_new_canonical_job() -> None:
    """Verify that when no stage matches, result is action=NEW with confidence 0.0."""
    job_repo = AsyncMock(spec=JobRepository)
    job_repo.get_by_source_reference.return_value = None
    job_repo.find_all_by_application_url.return_value = []
    job_repo.find_by_composite_attributes.return_value = []

    service = JobDeduplicationService(job_repository=job_repo)

    candidate = DeduplicationCandidate(
        source_id=uuid.uuid4(),
        source_job_id="brand_new_job",
        source_url="https://company.com/job/new",
        company_id=uuid.uuid4(),
        company_name="Brand New Employer",
        title="Lead Cloud Architect",
        normalized_title="lead cloud architect",
        location="Mumbai",
        normalized_location="mumbai",
        posted_at=datetime(2026, 10, 10, tzinfo=UTC),
    )

    result = await service.deduplicate(candidate)

    assert result.action == DeduplicationAction.NEW
    assert result.stage == DeduplicationStage.NONE
    assert result.confidence == 0.0
    assert result.canonical_job_id is None
    assert result.matched_job is None
    assert result.candidate_job_ids == []
    assert result.is_new is True

    # Audit trail verifies all 3 stages were evaluated
    assert result.evidence["stage_1"]["evaluated"] is True
    assert result.evidence["stage_1"]["match"] is False
    assert result.evidence["stage_2"]["evaluated"] is False  # No application_url
    assert result.evidence["stage_3"]["evaluated"] is True
    assert result.evidence["stage_3"]["match"] is False


@pytest.mark.asyncio
async def test_record_provenance_link_creates_and_flushes() -> None:
    """Verify record_provenance_link creates JobSourceReference and flushes without committing."""
    job_repo = AsyncMock(spec=JobRepository)
    mock_session = AsyncMock()
    job_repo.session = mock_session

    jsr_repo = AsyncMock(spec=JobSourceReferenceRepository)
    jsr_repo.get_by_source_external_id.return_value = None
    created_ref = MagicMock(spec=JobSourceReference)
    jsr_repo.create.return_value = created_ref

    service = JobDeduplicationService(
        job_repository=job_repo,
        job_source_reference_repository=jsr_repo,
    )

    job_id = uuid.uuid4()
    raw_job_id = uuid.uuid4()
    source_id = uuid.uuid4()

    res = await service.record_provenance_link(
        job_id=job_id,
        raw_job_id=raw_job_id,
        source_id=source_id,
        source_job_id="ext_provenance_123",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        source_job_id_type="NATIVE",
        application_url="https://boards.greenhouse.io/acme/jobs/123",
        extracted_data={"title": "Backend Engineer"},
        is_primary=False,
    )

    assert res is created_ref
    jsr_repo.create.assert_awaited_once()
    called_ref = jsr_repo.create.call_args[0][0]
    assert isinstance(called_ref, JobSourceReference)
    assert called_ref.job_id == job_id
    assert called_ref.raw_job_id == raw_job_id
    assert called_ref.source_id == source_id
    assert called_ref.source_job_id == "ext_provenance_123"
    assert called_ref.is_primary is False

    # Commit must not be called on the session
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_record_provenance_link_idempotent_on_repeated_reference() -> None:
    """Verify record_provenance_link idempotently updates timestamps on repeated references."""
    job_repo = AsyncMock(spec=JobRepository)
    mock_session = AsyncMock()
    job_repo.session = mock_session

    job_id = uuid.uuid4()
    source_id = uuid.uuid4()
    existing_ref = MagicMock(spec=JobSourceReference)
    existing_ref.job_id = job_id
    existing_ref.posted_at = None

    jsr_repo = AsyncMock(spec=JobSourceReferenceRepository)
    jsr_repo.session = mock_session
    jsr_repo.get_by_source_external_id.return_value = existing_ref

    service = JobDeduplicationService(
        job_repository=job_repo,
        job_source_reference_repository=jsr_repo,
    )

    res = await service.record_provenance_link(
        job_id=job_id,
        raw_job_id=uuid.uuid4(),
        source_id=source_id,
        source_job_id="ext_repeated_123",
        source_url="https://example.com/job/123",
        application_url="https://example.com/apply/123",
    )

    assert res is existing_ref
    # Must NOT create a second row (no INSERT)
    jsr_repo.create.assert_not_called()
    # Must flush session
    mock_session.flush.assert_awaited_once()
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_record_provenance_link_raises_value_error_on_cross_job_collision() -> None:
    """Verify record_provenance_link raises ValueError when external reference belongs to another job."""
    job_repo = AsyncMock(spec=JobRepository)
    mock_session = AsyncMock()
    job_repo.session = mock_session

    different_job_id = uuid.uuid4()
    target_job_id = uuid.uuid4()
    source_id = uuid.uuid4()

    existing_ref = MagicMock(spec=JobSourceReference)
    existing_ref.job_id = different_job_id  # Tied to different job

    jsr_repo = AsyncMock(spec=JobSourceReferenceRepository)
    jsr_repo.get_by_source_external_id.return_value = existing_ref

    service = JobDeduplicationService(
        job_repository=job_repo,
        job_source_reference_repository=jsr_repo,
    )

    with pytest.raises(ValueError, match="already linked to canonical job"):
        await service.record_provenance_link(
            job_id=target_job_id,
            raw_job_id=uuid.uuid4(),
            source_id=source_id,
            source_job_id="colliding_ext_id",
            source_url="https://example.com/job/123",
        )
