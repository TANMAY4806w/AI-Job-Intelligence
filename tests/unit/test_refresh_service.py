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
    DEV_AUTHORIZATION_TOKEN_ENV_VAR,
    DEV_DATABASE_NAME,
    EXPECTED_DEV_AUTHORIZATION_TOKEN,
    FIGMA_COMPANY_ID,
    FIGMA_TEL_AVIV_JOB_ID,
    JobRefreshService,
    RefreshItemSummary,
    RefreshPlan,
    RefreshResult,
    ScopedDevAuthorization,
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


# =========================================================================
# Scoped Dev Authorization Tests (6-Point Gate)
# =========================================================================


@pytest.mark.asyncio
async def test_scoped_dev_authorization_requires_override_flag() -> None:
    """Validate execution against dev fails when allow_database_override is missing."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value=DEV_DATABASE_NAME)

    with pytest.raises(PermissionError, match="blocked without explicit override"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database=DEV_DATABASE_NAME,
            allow_database_override=None,
            authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_requires_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validate execution against dev fails when authorization token is missing or wrong."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value=DEV_DATABASE_NAME)

    # Missing token
    monkeypatch.delenv(DEV_AUTHORIZATION_TOKEN_ENV_VAR, raising=False)
    with pytest.raises(PermissionError, match="requires valid authorization token"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database=DEV_DATABASE_NAME,
            allow_database_override=DEV_DATABASE_NAME,
            authorization_token=None,
        )

    # Invalid token
    with pytest.raises(PermissionError, match="requires valid authorization token"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database=DEV_DATABASE_NAME,
            allow_database_override=DEV_DATABASE_NAME,
            authorization_token="INVALID_TOKEN_XYZ",
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_requires_dev_expected_database() -> None:
    """Validate execution fails if current DB is dev but expected DB is not dev."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value=DEV_DATABASE_NAME)

    with pytest.raises(ValueError, match="Database identity guard failed"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database="job_intelligence_test",
            allow_database_override=DEV_DATABASE_NAME,
            authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_requires_dev_connected_database() -> None:
    """Validate execution fails if expected DB is dev but connected DB is test."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value="job_intelligence_test")

    with pytest.raises(ValueError, match="Database identity guard failed"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database=DEV_DATABASE_NAME,
            allow_database_override=DEV_DATABASE_NAME,
            authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_rejects_non_figma_company() -> None:
    """Validate execution against dev fails if company is not Figma."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value=DEV_DATABASE_NAME)

    non_figma = Company(
        id=uuid.uuid4(),
        name="Other Corp",
        normalized_name="other corp",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    service._resolve_company = AsyncMock(return_value=non_figma)

    with pytest.raises(PermissionError, match="strictly restricted to Figma"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=non_figma.id,
            expected_database=DEV_DATABASE_NAME,
            allow_database_override=DEV_DATABASE_NAME,
            authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_rejects_arbitrary_database_override() -> None:
    """Validate override flag cannot be used on arbitrary non-dev databases."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value="staging_db")

    with pytest.raises(PermissionError, match="strictly restricted to allowlisted test database"):
        await service.apply_refresh(
            session=mock_session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database="staging_db",
            allow_database_override="staging_db",
            authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
        )


@pytest.mark.asyncio
async def test_scoped_dev_authorization_succeeds_when_all_conditions_met() -> None:
    """Validate all 6 conditions pass authorization and execute successfully (simulated)."""
    service = JobRefreshService()
    mock_session = AsyncMock()
    service.get_current_database = AsyncMock(return_value=DEV_DATABASE_NAME)

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    service._resolve_company = AsyncMock(return_value=figma_company)

    # Empty plan (already up to date) for idempotency path
    mock_plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=138,
        jobs_to_update=0,
        jobs_to_skip=138,
        verification_records_to_insert=0,
        filter_decisions_to_insert=0,
        is_already_up_to_date=True,
        status_transitions={},
        items=[],
    )
    service.preview_refresh = AsyncMock(return_value=mock_plan)

    res = await service.apply_refresh(
        session=mock_session,
        company_id_or_name=FIGMA_COMPANY_ID,
        expected_database=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )
    assert res.status == "SKIPPED_ALREADY_APPLIED"
    assert res.company_id == FIGMA_COMPANY_ID
    assert res.target_database == DEV_DATABASE_NAME


# =========================================================================
# Pre-Commit In-Transaction Invariant Validation Tests
# =========================================================================


@pytest.mark.asyncio
async def test_pre_commit_invariants_pass_when_figma_criteria_met() -> None:
    """Validate _validate_pre_commit_invariants succeeds when all 138 Figma jobs match rules."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )

    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
        for _ in range(138)
    ]
    # Set 138th job as the Tel Aviv job
    tel_aviv_job = Job(
        id=FIGMA_TEL_AVIV_JOB_ID,
        company_id=FIGMA_COMPANY_ID,
        eligibility_status="REJECTED",
        rejection_reason="LOCATION_NOT_ALLOWED",
        normalized_location="Tel Aviv",
        source_verified=True,
        verification_confidence=Decimal("0.50"),
        updated_at=now_utc,
    )
    mock_jobs[137] = tel_aviv_job

    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database="job_intelligence_test",
        total_jobs=138,
        jobs_to_update=138,
        jobs_to_skip=0,
        verification_records_to_insert=138,
        filter_decisions_to_insert=138,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    # Mock DB queries:
    # 1. Total jobs count query -> 138
    # 2. Distribution query -> (("REJECTED", "EXCLUDED_ROLE", 123), ("REJECTED", "LOCATION_NOT_ALLOWED", 15))
    # 3. Non-rejected count query -> 0
    # 4. Tel Aviv get -> tel_aviv_job
    # 5. VerificationRecord per-job counts -> 138 jobs with count 2
    # 6. FilterDecision per-job counts -> 138 jobs with count 2

    job_ids = [j.id for j in mock_jobs]

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "group by" in stmt_str and "rejection_reason" in stmt_str:
            mock_res.all.return_value = [
                ("REJECTED", "EXCLUDED_ROLE", 123),
                ("REJECTED", "LOCATION_NOT_ALLOWED", 15),
            ]
        elif "group by" in stmt_str and (
            "verification_records" in stmt_str or "filter_decisions" in stmt_str
        ):
            mock_res.all.return_value = [(jid, 2) for jid in job_ids]
        elif "eligibility_status !=" in stmt_str or "eligibility_status <>" in stmt_str:
            mock_res.scalar.return_value = 0
        else:
            mock_res.scalar.return_value = 138
        return mock_res

    mock_session.execute = AsyncMock(side_effect=mock_execute)
    mock_session.get = AsyncMock(return_value=tel_aviv_job)

    dev_auth = JobRefreshService._authorize_development_refresh(
        company=figma_company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )

    # Should execute without raising
    await service._validate_pre_commit_invariants(
        session=mock_session,
        company=figma_company,
        plan=plan,
        jobs_to_process=mock_jobs,
        now_utc=now_utc,
        current_database=DEV_DATABASE_NAME,
        dev_authorization=dev_auth,
    )


def test_scoped_dev_authorization_direct_construction_fails() -> None:
    """Validate ScopedDevAuthorization cannot be constructed directly without private sentinel."""
    with pytest.raises(
        PermissionError, match="ScopedDevAuthorization cannot be constructed directly"
    ):
        ScopedDevAuthorization(
            company_id=FIGMA_COMPANY_ID,
            company_name="Figma",
            database_name=DEV_DATABASE_NAME,
        )


def test_scoped_dev_authorization_verify_authority_mismatches() -> None:
    """Validate verify_authority fails when company or database context mismatches."""
    company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    auth = JobRefreshService._authorize_development_refresh(
        company=company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )
    # Valid authority passes
    auth.verify_authority(target_company_id=FIGMA_COMPANY_ID, target_database=DEV_DATABASE_NAME)

    # Wrong company
    other_comp_id = uuid.uuid4()
    with pytest.raises(PermissionError, match="Scoped authorization context mismatch"):
        auth.verify_authority(target_company_id=other_comp_id, target_database=DEV_DATABASE_NAME)

    # Wrong database
    with pytest.raises(PermissionError, match="Scoped authorization context mismatch"):
        auth.verify_authority(
            target_company_id=FIGMA_COMPANY_ID, target_database="job_intelligence_test"
        )


@pytest.mark.asyncio
async def test_unauthorized_call_cannot_reach_figma_dev_invariants() -> None:
    """Validate that calling dev invariants without ScopedDevAuthorization raises PermissionError."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    with pytest.raises(
        PermissionError,
        match="forbidden without explicit ScopedDevAuthorization proof",
    ):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=figma_company,
            plan=plan,
            jobs_to_process=mock_jobs,
            now_utc=now_utc,
            current_database=DEV_DATABASE_NAME,
            dev_authorization=None,
        )


@pytest.mark.asyncio
async def test_pre_commit_invariants_fails_on_job_count_mismatch() -> None:
    """Validate _validate_pre_commit_invariants raises when total Figma jobs != 138."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    # Return count = 137
    mock_res = MagicMock()
    mock_res.scalar.return_value = 137
    mock_session.execute = AsyncMock(return_value=mock_res)

    dev_auth = JobRefreshService._authorize_development_refresh(
        company=figma_company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )

    with pytest.raises(RuntimeError, match="expected 138 Figma jobs, found 137"):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=figma_company,
            plan=plan,
            jobs_to_process=mock_jobs,
            now_utc=now_utc,
            current_database=DEV_DATABASE_NAME,
            dev_authorization=dev_auth,
        )


@pytest.mark.asyncio
async def test_pre_commit_invariants_fails_on_distribution_mismatch() -> None:
    """Validate _validate_pre_commit_invariants raises when status distribution != 123/15."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "group by" in stmt_str and "rejection_reason" in stmt_str:
            # Distribution mismatch: 120 EXCLUDED_ROLE and 18 LOCATION_NOT_ALLOWED
            mock_res.all.return_value = [
                ("REJECTED", "EXCLUDED_ROLE", 120),
                ("REJECTED", "LOCATION_NOT_ALLOWED", 18),
            ]
        else:
            mock_res.scalar.return_value = 138
        return mock_res

    mock_session.execute = AsyncMock(side_effect=mock_execute)

    dev_auth = JobRefreshService._authorize_development_refresh(
        company=figma_company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )

    with pytest.raises(RuntimeError, match="expected 123 REJECTED/EXCLUDED_ROLE jobs, found 120"):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=figma_company,
            plan=plan,
            jobs_to_process=mock_jobs,
            now_utc=now_utc,
            current_database=DEV_DATABASE_NAME,
            dev_authorization=dev_auth,
        )


@pytest.mark.asyncio
async def test_pre_commit_invariants_fails_on_tel_aviv_job_mismatch() -> None:
    """Validate _validate_pre_commit_invariants raises when Tel Aviv job attributes mismatch."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "group by" in stmt_str and "rejection_reason" in stmt_str:
            mock_res.all.return_value = [
                ("REJECTED", "EXCLUDED_ROLE", 123),
                ("REJECTED", "LOCATION_NOT_ALLOWED", 15),
            ]
        elif "eligibility_status !=" in stmt_str or "eligibility_status <>" in stmt_str:
            mock_res.scalar.return_value = 0
        else:
            mock_res.scalar.return_value = 138
        return mock_res

    mock_session.execute = AsyncMock(side_effect=mock_execute)

    # Tel Aviv job with confidence 0.30 instead of 0.50
    bad_tel_aviv = Job(
        id=FIGMA_TEL_AVIV_JOB_ID,
        company_id=FIGMA_COMPANY_ID,
        eligibility_status="REJECTED",
        rejection_reason="LOCATION_NOT_ALLOWED",
        normalized_location="Tel Aviv",
        source_verified=True,
        verification_confidence=Decimal("0.30"),  # Mismatch!
        updated_at=now_utc,
    )
    mock_session.get = AsyncMock(return_value=bad_tel_aviv)

    dev_auth = JobRefreshService._authorize_development_refresh(
        company=figma_company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )

    with pytest.raises(
        RuntimeError, match="Tel Aviv job verification_confidence is 0.30, expected 0.50"
    ):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=figma_company,
            plan=plan,
            jobs_to_process=mock_jobs,
            now_utc=now_utc,
            current_database=DEV_DATABASE_NAME,
            dev_authorization=dev_auth,
        )


@pytest.mark.asyncio
async def test_pre_commit_invariants_fails_on_audit_count_mismatch() -> None:
    """Validate _validate_pre_commit_invariants raises when a Figma job lacks 2 audit rows."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        )
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database=DEV_DATABASE_NAME,
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    valid_tel_aviv = Job(
        id=FIGMA_TEL_AVIV_JOB_ID,
        company_id=FIGMA_COMPANY_ID,
        eligibility_status="REJECTED",
        rejection_reason="LOCATION_NOT_ALLOWED",
        normalized_location="Tel Aviv",
        source_verified=True,
        verification_confidence=Decimal("0.50"),
        updated_at=now_utc,
    )
    mock_session.get = AsyncMock(return_value=valid_tel_aviv)

    async def mock_execute(stmt):
        mock_res = MagicMock()
        stmt_str = str(stmt).lower()
        if "group by" in stmt_str and "rejection_reason" in stmt_str:
            mock_res.all.return_value = [
                ("REJECTED", "EXCLUDED_ROLE", 123),
                ("REJECTED", "LOCATION_NOT_ALLOWED", 15),
            ]
        elif "eligibility_status !=" in stmt_str or "eligibility_status <>" in stmt_str:
            mock_res.scalar.return_value = 0
        elif "verification_records" in stmt_str and "group by" in stmt_str:
            # Audit row count mismatch: only 137 records returned
            mock_res.all.return_value = [(uuid.uuid4(), 2) for _ in range(137)]
        else:
            mock_res.scalar.return_value = 138
        return mock_res

    mock_session.execute = AsyncMock(side_effect=mock_execute)

    dev_auth = JobRefreshService._authorize_development_refresh(
        company=figma_company,
        current_db=DEV_DATABASE_NAME,
        expected_db=DEV_DATABASE_NAME,
        allow_database_override=DEV_DATABASE_NAME,
        authorization_token=EXPECTED_DEV_AUTHORIZATION_TOKEN,
    )

    with pytest.raises(
        RuntimeError, match="each of the 138 Figma jobs must have exactly 2 VerificationRecords"
    ):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=figma_company,
            plan=plan,
            jobs_to_process=mock_jobs,
            now_utc=now_utc,
            current_database=DEV_DATABASE_NAME,
            dev_authorization=dev_auth,
        )


@pytest.mark.asyncio
async def test_test_database_refresh_cannot_trigger_figma_dev_invariants() -> None:
    """Validate that a refresh against test database cannot accidentally trigger Figma dev invariants."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    figma_company = Company(
        id=FIGMA_COMPANY_ID,
        name="Figma",
        normalized_name="figma",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    # Only 2 jobs (NOT 138)
    mock_jobs = [
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        ),
        Job(
            id=uuid.uuid4(),
            company_id=FIGMA_COMPANY_ID,
            eligibility_status="REJECTED",
            rejection_reason="EXCLUDED_ROLE",
            verification_confidence=Decimal("0.50"),
            updated_at=now_utc,
        ),
    ]
    plan = RefreshPlan(
        company_id=FIGMA_COMPANY_ID,
        company_name="Figma",
        current_database="job_intelligence_test",
        total_jobs=2,
        jobs_to_update=2,
        jobs_to_skip=0,
        verification_records_to_insert=2,
        filter_decisions_to_insert=2,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    # Calling with current_database='job_intelligence_test' MUST NOT trigger the 138-job check
    await service._validate_pre_commit_invariants(
        session=mock_session,
        company=figma_company,
        plan=plan,
        jobs_to_process=mock_jobs,
        now_utc=now_utc,
        current_database="job_intelligence_test",
    )


@pytest.mark.asyncio
async def test_generic_company_pre_commit_invariants() -> None:
    """Validate generic companies pass pre-commit invariants without Figma-specific checks."""
    service = JobRefreshService()
    mock_session = AsyncMock()

    generic_company = Company(
        id=uuid.uuid4(),
        name="Acme Corp",
        normalized_name="acme corp",
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    now_utc = datetime.now(UTC)
    job = Job(
        id=uuid.uuid4(),
        company_id=generic_company.id,
        eligibility_status="ELIGIBLE",
        verification_confidence=Decimal("0.90"),
        updated_at=now_utc,
    )
    plan = RefreshPlan(
        company_id=generic_company.id,
        company_name=generic_company.name,
        current_database="job_intelligence_test",
        total_jobs=1,
        jobs_to_update=1,
        jobs_to_skip=0,
        verification_records_to_insert=1,
        filter_decisions_to_insert=1,
        is_already_up_to_date=False,
        status_transitions={},
        items=[],
    )

    # Should succeed without executing Figma queries
    await service._validate_pre_commit_invariants(
        session=mock_session,
        company=generic_company,
        plan=plan,
        jobs_to_process=[job],
        now_utc=now_utc,
        current_database="job_intelligence_test",
    )

    # Fails if processed jobs count mismatches plan
    with pytest.raises(
        RuntimeError, match="Pre-commit invariant violation: processed 0 jobs, expected 1"
    ):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=generic_company,
            plan=plan,
            jobs_to_process=[],
            now_utc=now_utc,
            current_database="job_intelligence_test",
        )

    # Fails if job has null eligibility_status
    job.eligibility_status = ""
    with pytest.raises(RuntimeError, match="has null eligibility_status"):
        await service._validate_pre_commit_invariants(
            session=mock_session,
            company=generic_company,
            plan=plan,
            jobs_to_process=[job],
            now_utc=now_utc,
            current_database="job_intelligence_test",
        )
