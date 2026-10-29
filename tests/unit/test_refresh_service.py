"""Unit tests for the controlled persistence refresh service."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.orchestration.reevaluation_service import JobReevaluationComparison
from app.orchestration.refresh_service import (
    JobRefreshService,
    RefreshItemSummary,
    RefreshPlan,
    RefreshResult,
)
from app.persistence.models.company import Company
from app.persistence.models.job import Job


def test_refresh_plan_and_result_to_dict_serialization() -> None:
    """Validate serialization of RefreshPlan and RefreshResult data structures."""
    company_id = uuid.uuid4()
    job_id = uuid.uuid4()

    item = RefreshItemSummary(
        job_id=job_id,
        source_job_id="101",
        title="Software Engineer",
        location="Bengaluru, India",
        action="UPDATE",
        persisted_status="REVIEW",
        proposed_status="REJECTED",
        persisted_rejection_reason=None,
        proposed_rejection_reason="LOCATION_NOT_ALLOWED",
        persisted_review_reason="POSTED_DATE_SUSPICIOUS",
        proposed_review_reason=None,
        persisted_confidence=Decimal("0.30"),
        proposed_confidence=Decimal("0.50"),
        status_changed=True,
        reason_changed=True,
        confidence_changed=True,
        verification_passed_changed=True,
        source_verified_changed=True,
    )

    plan = RefreshPlan(
        company_id=company_id,
        company_name="Test Corp",
        current_database="job_intelligence_test",
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={"REVIEW -> REJECTED": 1},
        items=[item],
    )

    plan_dict = plan.to_dict()
    assert plan_dict["company_name"] == "Test Corp"
    assert plan_dict["jobs_to_update"] == 1
    assert len(plan_dict["items"]) == 1
    assert plan_dict["items"][0]["action"] == "UPDATE"

    now_utc = datetime.now(UTC)
    result = RefreshResult(
        company_id=company_id,
        company_name="Test Corp",
        target_database="job_intelligence_test",
        status="SUCCESS",
        applied=True,
        total_jobs=1,
        jobs_updated=1,
        jobs_skipped=0,
        verification_records_inserted=1,
        filter_decisions_inserted=1,
        refreshed_at=now_utc,
        plan=plan,
    )

    result_dict = result.to_dict()
    assert result_dict["status"] == "SUCCESS"
    assert result_dict["applied"] is True
    assert result_dict["jobs_updated"] == 1


@pytest.mark.asyncio
async def test_wrong_database_identity_guard_in_preview() -> None:
    """Validate preview_refresh refuses execution when connected database does not match target-database."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    # Mock get_current_database to return 'job_intelligence_dev'
    service.get_current_database = AsyncMock(return_value="job_intelligence_dev")

    with pytest.raises(ValueError, match="Database identity mismatch"):
        await service.preview_refresh(
            session=mock_session,
            company_id_or_name="Test Corp",
            target_database="job_intelligence_test",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unauthorized_db",
    ["job_intelligence_dev", "unknown_db", "job_intelligence_prod", "staging_database"],
)
async def test_allowlist_blocks_unauthorized_target_database(unauthorized_db: str) -> None:
    """Validate apply_refresh raises PermissionError when target database is not allowlisted."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    with pytest.raises(PermissionError, match="strictly restricted to allowlisted test database"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=uuid.uuid4(),
            expected_database=unauthorized_db,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unauthorized_connected_db",
    ["job_intelligence_dev", "production_db", "test_mock_db"],
)
async def test_allowlist_blocks_unauthorized_connected_database(
    unauthorized_connected_db: str,
) -> None:
    """Validate apply_refresh raises PermissionError when connected database is not allowlisted."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    service.get_current_database = AsyncMock(return_value=unauthorized_connected_db)

    with pytest.raises(PermissionError, match="strictly restricted to allowlisted test database"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=uuid.uuid4(),
            expected_database="job_intelligence_test",
        )


@pytest.mark.asyncio
async def test_database_identity_guard_when_both_in_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate identity guard raises ValueError when connected != expected even if both in allowlist."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    service.get_current_database = AsyncMock(return_value="job_intelligence_test")
    # Temporarily monkeypatch allowlist to contain multiple entries
    monkeypatch.setattr(
        "app.orchestration.refresh_service.ALLOWED_MUTATING_DATABASES",
        frozenset({"job_intelligence_test", "job_intelligence_test_secondary"}),
    )

    with pytest.raises(ValueError, match="Database identity guard failed"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=uuid.uuid4(),
            expected_database="job_intelligence_test_secondary",
        )


@pytest.mark.asyncio
async def test_apply_refresh_rejects_dry_run_flag() -> None:
    """Validate apply_refresh raises ValueError if dry_run=True is accidentally passed."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    with pytest.raises(ValueError, match="apply_refresh called with dry_run=True"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=uuid.uuid4(),
            expected_database="job_intelligence_test",
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_idempotent_duplicate_detection_skips_when_up_to_date() -> None:
    """Validate apply_refresh returns SKIPPED_ALREADY_APPLIED when all jobs match latest rules."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    company_id = uuid.uuid4()
    mock_plan = RefreshPlan(
        company_id=company_id,
        company_name="Test Corp",
        current_database="job_intelligence_test",
        total_jobs=2,
        jobs_to_update=0,
        jobs_to_skip=2,
        verification_records_to_insert=0,
        filter_decisions_to_insert=0,
        is_already_up_to_date=True,
        status_transitions={},
        items=[],
    )
    service.preview_refresh = AsyncMock(return_value=mock_plan)

    result = await service.apply_refresh(
        session=mock_session,
        company_id_or_name=company_id,
        expected_database="job_intelligence_test",
        dry_run=False,
    )

    assert result.status == "SKIPPED_ALREADY_APPLIED"
    assert result.applied is False
    assert result.jobs_updated == 0
    assert result.jobs_skipped == 2
    assert result.verification_records_inserted == 0
    assert result.filter_decisions_inserted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diff_attribute,persisted_val,proposed_val,is_dim_flag",
    [
        ("eligibility_status", "REVIEW", "REJECTED", False),
        ("rejection_reason", "EXPERIENCE_TOO_HIGH", "LOCATION_NOT_ALLOWED", False),
        ("review_reason", "POSTED_DATE_SUSPICIOUS", None, False),
        ("role_priority", "P2", "P1", False),
        ("relevance_status", "OFF_TARGET", "TARGET", False),
        ("normalized_location", "Old City", "New City", False),
        ("verification_confidence", Decimal("0.40"), Decimal("0.80"), False),
        ("source_verified", False, True, True),
        ("jd_verified", False, True, True),
        ("company_verified", False, True, True),
        ("location_verified", False, True, True),
        ("experience_verified", False, True, True),
        ("salary_verified", False, True, True),
    ],
)
async def test_preview_refresh_detects_all_modifiable_fields(
    diff_attribute: str,
    persisted_val: Any,
    proposed_val: Any,
    is_dim_flag: bool,
) -> None:
    """Validate that preview_refresh flags UPDATE if ANY of the 13 canonical attributes differs."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    company_id = uuid.uuid4()
    company = Company(
        id=company_id,
        name="Test Corp",
        normalized_name="test corp",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    service._resolve_company = AsyncMock(return_value=company)

    # Base canonical job with default attributes
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        company_id=company_id,
        company_name="Test Corp",
        title="Senior Backend Engineer",
        normalized_title="senior backend engineer",
        location="Bengaluru, India",
        normalized_location="Bengaluru",
        remote_type="ON_SITE",
        description="A" * 150,
        application_url="https://jobs.example.com/101",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REJECTED",
        rejection_reason="EXPERIENCE_TOO_HIGH",
        review_reason=None,
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        verification_confidence=Decimal("0.80"),
    )

    # Apply the differing attribute
    dim_flags_persisted = {
        "source_verified": True,
        "jd_verified": True,
        "company_verified": True,
        "location_verified": True,
        "experience_verified": True,
        "salary_verified": True,
    }
    dim_flags_proposed = dict(dim_flags_persisted)

    if is_dim_flag:
        setattr(job, diff_attribute, persisted_val)
        dim_flags_persisted[diff_attribute] = persisted_val
        dim_flags_proposed[diff_attribute] = proposed_val
    else:
        setattr(job, diff_attribute, persisted_val)

    mock_comp = JobReevaluationComparison(
        job_id=job_id,
        source_job_id="101",
        title=job.title,
        location=job.location,
        normalized_location=(
            proposed_val if diff_attribute == "normalized_location" else "Bengaluru"
        ),
        company_name="Test Corp",
        persisted_status=(persisted_val if diff_attribute == "eligibility_status" else "REJECTED"),
        persisted_rejection_reason=(
            persisted_val if diff_attribute == "rejection_reason" else "EXPERIENCE_TOO_HIGH"
        ),
        persisted_review_reason=(persisted_val if diff_attribute == "review_reason" else None),
        persisted_role_priority=persisted_val if diff_attribute == "role_priority" else "P1",
        persisted_relevance_status=(
            persisted_val if diff_attribute == "relevance_status" else "TARGET"
        ),
        persisted_verification_confidence=(
            persisted_val if diff_attribute == "verification_confidence" else Decimal("0.80")
        ),
        persisted_verified=True,
        persisted_dimension_flags=dim_flags_persisted,
        proposed_status=proposed_val if diff_attribute == "eligibility_status" else "REJECTED",
        proposed_rejection_reason=(
            proposed_val if diff_attribute == "rejection_reason" else "EXPERIENCE_TOO_HIGH"
        ),
        proposed_review_reason=proposed_val if diff_attribute == "review_reason" else None,
        proposed_role_priority=proposed_val if diff_attribute == "role_priority" else "P1",
        proposed_relevance_status=(
            proposed_val if diff_attribute == "relevance_status" else "TARGET"
        ),
        proposed_verification_confidence=(
            proposed_val if diff_attribute == "verification_confidence" else Decimal("0.80")
        ),
        proposed_verified=True,
        proposed_dimension_flags=dim_flags_proposed,
        proposed_dimensions={},
        status_changed=(diff_attribute == "eligibility_status"),
        reason_changed=(diff_attribute in ("rejection_reason", "review_reason")),
        confidence_changed=(diff_attribute == "verification_confidence"),
        verification_passed_changed=False,
        missing_evidence=[],
        confirmed_failures=[],
    )

    # Mock DB query to return our job
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [job]
    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_exec_res)

    # Mock reevaluate_job to return our comparison
    service.reevaluation_service.reevaluate_job = MagicMock(return_value=mock_comp)

    plan = await service.preview_refresh(
        session=mock_session,
        company_id_or_name=company_id,
    )

    # Must detect modification required: NOT already up to date!
    assert plan.jobs_to_update == 1
    assert plan.jobs_to_skip == 0
    assert plan.is_already_up_to_date is False
    assert plan.items[0].action == "UPDATE"


@pytest.mark.asyncio
async def test_preview_refresh_skips_when_all_13_fields_match() -> None:
    """Validate that preview_refresh returns SKIP and is_already_up_to_date=True when all 13 fields match."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    company_id = uuid.uuid4()
    company = Company(
        id=company_id,
        name="Test Corp",
        normalized_name="test corp",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    service._resolve_company = AsyncMock(return_value=company)

    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        company_id=company_id,
        company_name="Test Corp",
        title="Senior Backend Engineer",
        normalized_title="senior backend engineer",
        location="Bengaluru, India",
        normalized_location="Bengaluru",
        remote_type="ON_SITE",
        description="A" * 150,
        application_url="https://jobs.example.com/101",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REJECTED",
        rejection_reason="EXPERIENCE_TOO_HIGH",
        review_reason=None,
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        verification_confidence=Decimal("0.80"),
    )

    dim_flags = {
        "source_verified": True,
        "jd_verified": True,
        "company_verified": True,
        "location_verified": True,
        "experience_verified": True,
        "salary_verified": True,
    }

    mock_comp = JobReevaluationComparison(
        job_id=job_id,
        source_job_id="101",
        title=job.title,
        location=job.location,
        normalized_location="Bengaluru",
        company_name="Test Corp",
        persisted_status="REJECTED",
        persisted_rejection_reason="EXPERIENCE_TOO_HIGH",
        persisted_review_reason=None,
        persisted_role_priority="P1",
        persisted_relevance_status="TARGET",
        persisted_verification_confidence=Decimal("0.80"),
        persisted_verified=True,
        persisted_dimension_flags=dim_flags,
        proposed_status="REJECTED",
        proposed_rejection_reason="EXPERIENCE_TOO_HIGH",
        proposed_review_reason=None,
        proposed_role_priority="P1",
        proposed_relevance_status="TARGET",
        proposed_verification_confidence=Decimal("0.80"),
        proposed_verified=True,
        proposed_dimension_flags=dim_flags,
        proposed_dimensions={},
        status_changed=False,
        reason_changed=False,
        confidence_changed=False,
        verification_passed_changed=False,
        missing_evidence=[],
        confirmed_failures=[],
    )

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [job]
    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_exec_res)
    service.reevaluation_service.reevaluate_job = MagicMock(return_value=mock_comp)

    plan = await service.preview_refresh(
        session=mock_session,
        company_id_or_name=company_id,
    )

    assert plan.jobs_to_update == 0
    assert plan.jobs_to_skip == 1
    assert plan.is_already_up_to_date is True
    assert plan.items[0].action == "SKIP"


@pytest.mark.asyncio
async def test_preview_refresh_ignores_updated_at_timestamp() -> None:
    """Validate updated_at alone never triggers an UPDATE when all 13 canonical fields match."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    company_id = uuid.uuid4()
    company = Company(
        id=company_id,
        name="Test Corp",
        normalized_name="test corp",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    service._resolve_company = AsyncMock(return_value=company)

    # Job has a distinct updated_at timestamp set
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        company_id=company_id,
        company_name="Test Corp",
        title="Senior Backend Engineer",
        normalized_title="senior backend engineer",
        location="Bengaluru, India",
        normalized_location="Bengaluru",
        remote_type="ON_SITE",
        description="A" * 150,
        application_url="https://jobs.example.com/101",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REJECTED",
        rejection_reason="EXPERIENCE_TOO_HIGH",
        review_reason=None,
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        verification_confidence=Decimal("0.80"),
        updated_at=datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC),
    )

    dim_flags = {
        "source_verified": True,
        "jd_verified": True,
        "company_verified": True,
        "location_verified": True,
        "experience_verified": True,
        "salary_verified": True,
    }

    mock_comp = JobReevaluationComparison(
        job_id=job_id,
        source_job_id="101",
        title=job.title,
        location=job.location,
        normalized_location="Bengaluru",
        company_name="Test Corp",
        persisted_status="REJECTED",
        persisted_rejection_reason="EXPERIENCE_TOO_HIGH",
        persisted_review_reason=None,
        persisted_role_priority="P1",
        persisted_relevance_status="TARGET",
        persisted_verification_confidence=Decimal("0.80"),
        persisted_verified=True,
        persisted_dimension_flags=dim_flags,
        proposed_status="REJECTED",
        proposed_rejection_reason="EXPERIENCE_TOO_HIGH",
        proposed_review_reason=None,
        proposed_role_priority="P1",
        proposed_relevance_status="TARGET",
        proposed_verification_confidence=Decimal("0.80"),
        proposed_verified=True,
        proposed_dimension_flags=dim_flags,
        proposed_dimensions={},
        status_changed=False,
        reason_changed=False,
        confidence_changed=False,
        verification_passed_changed=False,
        missing_evidence=[],
        confirmed_failures=[],
    )

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [job]
    mock_exec_res = MagicMock()
    mock_exec_res.scalars.return_value = mock_scalars
    mock_session.execute = AsyncMock(return_value=mock_exec_res)
    service.reevaluation_service.reevaluate_job = MagicMock(return_value=mock_comp)

    plan = await service.preview_refresh(
        session=mock_session,
        company_id_or_name=company_id,
    )

    # Must NOT trigger update solely based on updated_at
    assert plan.jobs_to_update == 0
    assert plan.jobs_to_skip == 1
    assert plan.is_already_up_to_date is True
    assert plan.items[0].action == "SKIP"
