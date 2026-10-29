"""Unit tests for the dry-run re-evaluation orchestration service.

Validates:
1. Re-evaluation under latest verification and eligibility rules.
2. Distinction between proposed and persisted outcomes.
3. Proper separation of missing evidence from confirmed failures.
4. Correct location handling and foreign city normalization.
5. Handling of missing or suspicious publication dates.
6. Deterministic repeatability of dry-run evaluation.
7. Strict rejection of mutating execution requests (dry_run=False).
8. Report serialization into machine-readable dictionary format.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.orchestration.reevaluation_service import (
    JobReevaluationComparison,
    JobReevaluationService,
    ReevaluationReport,
)
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.rules.schemas import (
    REJECTION_EXCLUDED_ROLE,
    REJECTION_LOCATION_NOT_ALLOWED,
    REVIEW_COMPANY_UNVERIFIED,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
)


def _make_mock_job(
    *,
    job_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    company_name: str = "Test Company",
    title: str = "Software Engineer",
    location: str | None = "Bengaluru, India",
    remote_type: str = "ON_SITE",
    description: str = (
        "We are seeking an experienced Software Engineer to develop scalable backend "
        "systems in Python with PostgreSQL, asynchronous microservices, and modern cloud deployment pipelines."
    ),
    application_url: str = "https://boards.greenhouse.io/test/jobs/101",
    source_name: str = "Greenhouse",
    source_job_id: str = "101",
    eligibility_status: str = "REJECTED",
    rejection_reason: str | None = "LOCATION_NOT_ALLOWED",
    review_reason: str | None = None,
    source_verified: bool = False,
    jd_verified: bool = True,
    company_verified: bool = False,
    location_verified: bool = False,
    experience_verified: bool = False,
    salary_verified: bool = False,
    verification_confidence: Decimal = Decimal("0.30"),
    posted_at: datetime | None = None,
    collected_at: datetime | None = None,
) -> Job:
    """Helper to construct a mock Job entity with attached provenance tree."""
    c_id = company_id or uuid.uuid4()
    s_id = uuid.uuid4()
    cs_id = uuid.uuid4()
    r_id = uuid.uuid4()
    j_id = job_id or uuid.uuid4()
    c_now = collected_at or datetime.now(UTC)

    mock_source = MagicMock(spec=Source)
    mock_source.id = s_id
    mock_source.source_name = source_name
    mock_source.source_type = "ATS"
    mock_source.is_active = True

    mock_cs = MagicMock(spec=CompanySource)
    mock_cs.id = cs_id
    mock_cs.company_id = c_id
    mock_cs.source_id = s_id
    mock_cs.is_active = True
    mock_cs.source = mock_source

    mock_raw = MagicMock(spec=RawJob)
    mock_raw.id = r_id
    mock_raw.company_source_id = cs_id
    mock_raw.source_job_id = source_job_id
    mock_raw.source_url = f"https://api.greenhouse.io/v1/boards/test/jobs/{source_job_id}"
    mock_raw.payload_format = "JSON"
    mock_raw.http_status = 200
    mock_raw.collected_at = c_now
    mock_raw.company_source = mock_cs

    mock_ref = MagicMock(spec=JobSourceReference)
    mock_ref.id = uuid.uuid4()
    mock_ref.job_id = j_id
    mock_ref.raw_job_id = r_id
    mock_ref.source_id = s_id
    mock_ref.source_job_id = source_job_id
    mock_ref.source_url = mock_raw.source_url
    mock_ref.application_url = application_url
    mock_ref.is_primary = True
    mock_ref.raw_job = mock_raw
    mock_ref.source = mock_source

    mock_job = MagicMock(spec=Job)
    mock_job.id = j_id
    mock_job.company_id = c_id
    mock_job.company_name = company_name
    mock_job.title = title
    mock_job.normalized_title = title.lower()
    mock_job.location = location
    mock_job.normalized_location = None
    mock_job.remote_type = remote_type
    mock_job.description = description
    mock_job.application_url = application_url
    mock_job.employment_type = "FULL_TIME"
    mock_job.experience_raw = None
    mock_job.experience_min_years = None
    mock_job.experience_max_years = None
    mock_job.salary_min = None
    mock_job.salary_max = None
    mock_job.salary_currency = "INR"
    mock_job.salary_period = None
    mock_job.posted_at = posted_at
    mock_job.collected_at = c_now
    mock_job.eligibility_status = eligibility_status
    mock_job.rejection_reason = rejection_reason
    mock_job.review_reason = review_reason
    mock_job.source_verified = source_verified
    mock_job.jd_verified = jd_verified
    mock_job.company_verified = company_verified
    mock_job.location_verified = location_verified
    mock_job.experience_verified = experience_verified
    mock_job.salary_verified = salary_verified
    mock_job.verification_confidence = verification_confidence
    mock_job.source_references = [mock_ref]

    return mock_job


def test_reevaluate_job_with_latest_rules_and_approved_source() -> None:
    """Validate re-evaluation correctly approves Greenhouse source and computes updated confidence."""
    service = JobReevaluationService()
    job = _make_mock_job(
        title="Software Engineer",
        location="Bengaluru, India",
        source_name="Greenhouse",
        source_verified=False,  # Persisted had bug where source_verified was False
        verification_confidence=Decimal("0.40"),
    )

    comparison = service.reevaluate_job(job)

    # Proposed outcomes
    assert comparison.proposed_dimension_flags["source_verified"] is True
    assert comparison.proposed_dimension_flags["jd_verified"] is True
    assert comparison.proposed_dimension_flags["location_verified"] is True
    assert comparison.proposed_verified is True
    # Confidence: JD 0.30 + Source 0.20 + Location 0.10 = 0.60
    assert comparison.proposed_verification_confidence == Decimal("0.60")
    assert comparison.confidence_changed is True
    assert comparison.verification_passed_changed is True


def test_proposed_vs_persisted_outcome_detection_for_tel_aviv() -> None:
    """Validate transition from persisted REVIEW/POSTED_DATE_SUSPICIOUS to proposed REJECTED/LOCATION_NOT_ALLOWED."""
    service = JobReevaluationService()
    job = _make_mock_job(
        title="Software Engineer - Figma Weave (Tel Aviv, Israel)",
        location="Tel Aviv, Israel",
        remote_type="ON_SITE",
        posted_at=None,
        eligibility_status="REVIEW",
        rejection_reason=None,
        review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
        verification_confidence=Decimal("0.30"),
    )

    comparison = service.reevaluate_job(job)

    assert comparison.persisted_status == "REVIEW"
    assert comparison.persisted_review_reason == REVIEW_POSTED_DATE_SUSPICIOUS

    assert comparison.proposed_status == "REJECTED"
    assert comparison.proposed_rejection_reason == REJECTION_LOCATION_NOT_ALLOWED
    assert comparison.proposed_review_reason is None

    assert comparison.status_changed is True
    assert comparison.reason_changed is True
    assert comparison.proposed_verification_confidence == Decimal("0.50")


def test_location_normalization_handling_across_tiers() -> None:
    """Validate location handling for target hubs, foreign unlisted cities, and remote positions."""
    service = JobReevaluationService()

    # 1. Target Tech Hub (P2 Bengaluru)
    job_blr = _make_mock_job(
        title="Software Engineer",
        location="Bengaluru, Karnataka, India",
        remote_type="ON_SITE",
    )
    comp_blr = service.reevaluate_job(job_blr)
    assert comp_blr.normalized_location == "Bengaluru"
    assert comp_blr.proposed_dimension_flags["location_verified"] is True

    # 2. Confirmed Foreign Unlisted City (Tel Aviv)
    job_tlv = _make_mock_job(
        title="Software Engineer",
        location="Tel Aviv-Yafo, Israel",
        remote_type="ON_SITE",
    )
    comp_tlv = service.reevaluate_job(job_tlv)
    assert comp_tlv.normalized_location == "Tel Aviv"
    assert comp_tlv.proposed_status == "REJECTED"
    assert comp_tlv.proposed_rejection_reason == REJECTION_LOCATION_NOT_ALLOWED

    # 3. Excluded Role Trumps Location
    job_pm = _make_mock_job(
        title="Senior Product Manager",
        location="Bengaluru, India",
    )
    comp_pm = service.reevaluate_job(job_pm)
    assert comp_pm.proposed_status == "REJECTED"
    assert comp_pm.proposed_rejection_reason == REJECTION_EXCLUDED_ROLE


def test_missing_evidence_vs_confirmed_failures_classification() -> None:
    """Validate clear distinction between missing evidence attributes and confirmed hard failures."""
    service = JobReevaluationService()
    job = _make_mock_job(
        title="Account Executive",
        location="New York, NY",
        posted_at=None,
    )

    comparison = service.reevaluate_job(job)

    # Missing evidence attributes
    assert REVIEW_SALARY_UNKNOWN in comparison.missing_evidence
    assert REVIEW_EXPERIENCE_AMBIGUOUS in comparison.missing_evidence
    assert REVIEW_COMPANY_UNVERIFIED in comparison.missing_evidence
    assert REVIEW_POSTED_DATE_SUSPICIOUS in comparison.missing_evidence
    assert "unprobed_syntax_valid" in comparison.missing_evidence

    # Confirmed hard failures
    assert REJECTION_EXCLUDED_ROLE in comparison.confirmed_failures


def test_unknown_or_suspicious_publication_dates() -> None:
    """Validate handling of jobs with absent publication timestamps."""
    service = JobReevaluationService()
    job = _make_mock_job(
        title="Software Engineer",
        location="Bengaluru, India",
        posted_at=None,
    )

    comparison = service.reevaluate_job(job)

    assert comparison.proposed_dimension_flags["posting_date_verified"] is False
    assert REVIEW_POSTED_DATE_SUSPICIOUS in comparison.missing_evidence


def test_repeatability_of_dry_run_evaluation() -> None:
    """Validate evaluation yields deterministic and repeatable results across multiple invocations."""
    service = JobReevaluationService()
    job = _make_mock_job(
        title="Software Engineer",
        location="Bengaluru, India",
        posted_at=None,
    )

    comp1 = service.reevaluate_job(job)
    comp2 = service.reevaluate_job(job)

    assert comp1.proposed_status == comp2.proposed_status
    assert comp1.proposed_rejection_reason == comp2.proposed_rejection_reason
    assert comp1.proposed_verification_confidence == comp2.proposed_verification_confidence
    assert comp1.proposed_dimension_flags == comp2.proposed_dimension_flags
    assert comp1.missing_evidence == comp2.missing_evidence
    assert comp1.confirmed_failures == comp2.confirmed_failures


@pytest.mark.asyncio
async def test_dry_run_safety_guard_rejects_mutating_mode() -> None:
    """Validate that attempting mutating execution (dry_run=False) raises NotImplementedError."""
    service = JobReevaluationService()
    mock_session = AsyncMock()

    with pytest.raises(NotImplementedError, match="Mutating re-evaluation is disabled"):
        await service.reevaluate_company(
            session=mock_session,
            company_id_or_name=uuid.uuid4(),
            dry_run=False,
        )


def test_report_to_dict_serialization() -> None:
    """Validate ReevaluationReport serializes to a clean JSON-serializable dictionary."""
    comp = JobReevaluationComparison(
        job_id=uuid.uuid4(),
        source_job_id="101",
        title="Software Engineer",
        location="Bengaluru, India",
        normalized_location="Bengaluru",
        company_name="Test Company",
        persisted_status="REJECTED",
        persisted_rejection_reason="LOCATION_NOT_ALLOWED",
        persisted_review_reason=None,
        persisted_verification_confidence=Decimal("0.40"),
        persisted_verified=False,
        persisted_dimension_flags={"source_verified": False, "jd_verified": True},
        proposed_status="REJECTED",
        proposed_rejection_reason="LOCATION_NOT_ALLOWED",
        proposed_review_reason=None,
        proposed_verification_confidence=Decimal("0.60"),
        proposed_verified=True,
        proposed_dimension_flags={"source_verified": True, "jd_verified": True},
        proposed_dimensions={},
        status_changed=False,
        reason_changed=False,
        confidence_changed=True,
        verification_passed_changed=True,
        missing_evidence=[REVIEW_SALARY_UNKNOWN],
        confirmed_failures=[REJECTION_LOCATION_NOT_ALLOWED],
    )

    report = ReevaluationReport(
        company_id=uuid.uuid4(),
        company_name="Test Company",
        total_jobs=1,
        reference_time=datetime.now(UTC),
        dry_run=True,
        persisted_status_counts={"REJECTED": 1},
        proposed_status_counts={"REJECTED": 1},
        persisted_reason_counts={"LOCATION_NOT_ALLOWED": 1},
        proposed_reason_counts={"LOCATION_NOT_ALLOWED": 1},
        status_transitions={"REJECTED -> REJECTED": 1},
        persisted_dimension_counts={"jd_existence": 1},
        proposed_dimension_counts={"source_validity": 1, "jd_existence": 1},
        persisted_confidence_counts={"0.40": 1},
        proposed_confidence_counts={"0.60": 1},
        average_persisted_confidence=Decimal("0.400"),
        average_proposed_confidence=Decimal("0.600"),
        missing_evidence_counts={REVIEW_SALARY_UNKNOWN: 1},
        confirmed_failure_counts={REJECTION_LOCATION_NOT_ALLOWED: 1},
        total_status_changes=0,
        job_comparisons=[comp],
    )

    data = report.to_dict()
    assert data["company_name"] == "Test Company"
    assert data["total_jobs"] == 1
    assert data["dry_run"] is True
    assert data["summary"]["total_status_changes"] == 0
    assert len(data["job_comparisons"]) == 1
    assert data["job_comparisons"][0]["title"] == "Software Engineer"
