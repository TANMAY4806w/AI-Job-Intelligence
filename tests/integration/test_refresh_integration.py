"""Integration tests for the controlled persistence refresh workflow against real PostgreSQL 18.

Validates:
1. Successful transactional refresh against live PostgreSQL schema.
2. Complete rollback behavior when an operation fails mid-transaction.
3. Idempotent execution (duplicate application detection without redundant audit rows).
4. Preservation of pre-existing historical VerificationRecord and FilterDecision rows.
5. Enforcement of database identity guards (rejection of wrong database name).
6. Enforcement of development database safety block ('job_intelligence_dev').
7. CLI subcommand dispatch for preview and apply modes.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.cli.main import async_main, build_parser
from app.orchestration.refresh_service import (
    FIGMA_COMPANY_ID,
    JobRefreshService,
)
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource, Source
from app.persistence.session import get_sessionmaker

# Mark all tests in this file as integration tests (auto-isolated to job_intelligence_test)
pytestmark = pytest.mark.integration


async def _seed_refresh_test_data(
    session,
) -> tuple[Company, Source, CompanySource, list[Job], list[uuid.UUID], list[uuid.UUID]]:
    """Helper to seed a test company with 2 canonical jobs and initial historical audit records."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"RefreshTestCorp_{unique_suffix}"
    now_utc = datetime.now(UTC)

    # 1. Company
    company = Company(
        name=company_name,
        normalized_name=company_name.lower(),
        sector="Technology",
        business_model="PRODUCT",
        is_verified=True,
    )
    session.add(company)
    await session.flush()

    # 2. Source (Get or create canonical Greenhouse source)
    res = await session.execute(select(Source).where(Source.source_name == "Greenhouse"))
    source = res.scalar_one_or_none()
    if source is None:
        source = Source(
            source_name="Greenhouse",
            source_type="ATS",
            base_url="https://api.greenhouse.io/v1/boards",
            is_active=True,
        )
        session.add(source)
        await session.flush()

    # 3. CompanySource
    company_source = CompanySource(
        company_id=company.id,
        source_id=source.id,
        ats_identifier=f"refreshboard_{unique_suffix}",
        discovery_method="MANUAL_ONBOARDING",
        is_active=True,
    )
    session.add(company_source)
    await session.flush()

    # 4. CollectionRun
    collection_run = CollectionRun(
        source_id=source.id,
        company_source_id=company_source.id,
        run_type="HARVEST",
        status="SUCCESS",
        records_discovered=2,
        records_ingested=2,
        records_failed=0,
        started_at=now_utc,
        finished_at=now_utc,
    )
    session.add(collection_run)
    await session.flush()

    # 5. RawJobs, Canonical Jobs, and Initial Audit Records
    # Job A: Backend Engineer in Bengaluru (initial bug state: source_verified=False, confidence=0.30)
    body_a = b'{"id": 8101, "title": "Senior Backend Python Engineer"}'
    raw_job_a = RawJob(
        company_source_id=company_source.id,
        collection_run_id=collection_run.id,
        source_job_id="8101",
        source_job_id_type="NATIVE",
        source_url=f"https://api.greenhouse.io/v1/boards/{company_source.ats_identifier}/jobs/8101",
        payload_format="JSON",
        raw_body=body_a,
        raw_payload={"id": 8101, "title": "Senior Backend Python Engineer"},
        content_hash=hashlib.sha256(body_a).hexdigest(),
        http_status=200,
        collected_at=now_utc,
    )
    session.add(raw_job_a)
    await session.flush()

    job_a = Job(
        company_id=company.id,
        company_name=company.name,
        title="Senior Backend Python Engineer",
        normalized_title="senior backend python engineer",
        location="Bengaluru, Karnataka, India",
        normalized_location="Bengaluru",
        remote_type="ON_SITE",
        description=(
            "We are seeking an experienced Backend Python Engineer to build scalable distributed "
            "microservices pipelines, PostgreSQL persistence models, and cloud infrastructure."
        ),
        application_url="https://boards.greenhouse.io/test/jobs/8101",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REJECTED",
        rejection_reason="EXPERIENCE_TOO_HIGH",
        source_verified=False,  # Outdated
        jd_verified=True,
        company_verified=False,
        location_verified=False,  # Outdated
        experience_verified=False,
        salary_verified=False,
        verification_confidence=Decimal("0.30"),  # Outdated
        collected_at=now_utc,
    )
    session.add(job_a)
    await session.flush()

    ref_a = JobSourceReference(
        job_id=job_a.id,
        raw_job_id=raw_job_a.id,
        source_id=source.id,
        source_job_id="8101",
        source_url=raw_job_a.source_url,
        application_url=job_a.application_url,
        is_primary=True,
        collected_at=now_utc,
    )
    session.add(ref_a)

    # Initial historical audit row for Job A
    initial_v_a = VerificationRecord(
        job_id=job_a.id,
        verification_run_at=now_utc,
        source_verified=False,
        jd_verified=True,
        application_url_verified=False,
        company_verified=False,
        location_verified=False,
        experience_verified=False,
        salary_verified=False,
        posting_date_verified=False,
        verification_confidence=Decimal("0.30"),
        passed=False,
        failure_reasons=["SOURCE_INVALID"],
        review_reasons=[],
        audit_trail={"initial": True},
    )
    session.add(initial_v_a)

    initial_f_a = FilterDecision(
        job_id=job_a.id,
        rule_version="V1.0",
        final_status="REJECTED",
        role_priority="P1",
        reason_codes=["EXPERIENCE_TOO_HIGH"],
        evaluation_context={"initial": True},
        evaluated_at=now_utc,
    )
    session.add(initial_f_a)

    # Job B: SWE in Tel Aviv (initial state: REVIEW/POSTED_DATE_SUSPICIOUS)
    body_b = b'{"id": 8102, "title": "Software Engineer (Tel Aviv)"}'
    raw_job_b = RawJob(
        company_source_id=company_source.id,
        collection_run_id=collection_run.id,
        source_job_id="8102",
        source_job_id_type="NATIVE",
        source_url=f"https://api.greenhouse.io/v1/boards/{company_source.ats_identifier}/jobs/8102",
        payload_format="JSON",
        raw_body=body_b,
        raw_payload={"id": 8102, "title": "Software Engineer (Tel Aviv)"},
        content_hash=hashlib.sha256(body_b).hexdigest(),
        http_status=200,
        collected_at=now_utc,
    )
    session.add(raw_job_b)
    await session.flush()

    job_b = Job(
        company_id=company.id,
        company_name=company.name,
        title="Software Engineer (Tel Aviv)",
        normalized_title="software engineer (tel aviv)",
        location="Tel Aviv, Israel",
        normalized_location=None,
        remote_type="ON_SITE",
        description=(
            "We are seeking an ambitious Software Engineer to join our Tel Aviv engineering office "
            "building core product infrastructure, high-throughput backend APIs, and distributed systems."
        ),
        application_url="https://boards.greenhouse.io/test/jobs/8102",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REVIEW",
        review_reason="POSTED_DATE_SUSPICIOUS",
        source_verified=False,
        jd_verified=True,
        company_verified=False,
        location_verified=False,
        experience_verified=False,
        salary_verified=False,
        verification_confidence=Decimal("0.30"),
        collected_at=now_utc,
    )
    session.add(job_b)
    await session.flush()

    ref_b = JobSourceReference(
        job_id=job_b.id,
        raw_job_id=raw_job_b.id,
        source_id=source.id,
        source_job_id="8102",
        source_url=raw_job_b.source_url,
        application_url=job_b.application_url,
        is_primary=True,
        collected_at=now_utc,
    )
    session.add(ref_b)

    initial_v_b = VerificationRecord(
        job_id=job_b.id,
        verification_run_at=now_utc,
        source_verified=False,
        jd_verified=True,
        application_url_verified=False,
        company_verified=False,
        location_verified=False,
        experience_verified=False,
        salary_verified=False,
        posting_date_verified=False,
        verification_confidence=Decimal("0.30"),
        passed=False,
        failure_reasons=["SOURCE_INVALID"],
        review_reasons=["POSTED_DATE_SUSPICIOUS"],
        audit_trail={"initial": True},
    )
    session.add(initial_v_b)

    initial_f_b = FilterDecision(
        job_id=job_b.id,
        rule_version="V1.0",
        final_status="REVIEW",
        role_priority="P1",
        reason_codes=["POSTED_DATE_SUSPICIOUS"],
        evaluation_context={"initial": True},
        evaluated_at=now_utc,
    )
    session.add(initial_f_b)

    await session.commit()
    return (
        company,
        source,
        company_source,
        [job_a, job_b],
        [initial_v_a.id, initial_v_b.id],
        [initial_f_a.id, initial_f_b.id],
    )


@pytest.mark.asyncio
async def test_successful_transactional_refresh_and_audit_preservation() -> None:
    """Verify refresh updates canonical jobs, appends audit rows, and preserves existing audit records."""
    session_factory = get_sessionmaker()

    # 1. Seed database
    async with session_factory() as session:
        company, _, _, jobs, init_v_ids, init_f_ids = await _seed_refresh_test_data(session)
        company_id = company.id
        job_a_id = jobs[0].id
        job_b_id = jobs[1].id

    # 2. Execute apply_refresh
    service = JobRefreshService()
    async with session_factory() as session:
        result = await service.apply_refresh(
            session=session,
            company_id_or_name=company_id,
            expected_database="job_intelligence_test",
            dry_run=False,
        )
        await session.commit()

    assert result.status == "SUCCESS"
    assert result.applied is True
    assert result.jobs_updated == 2
    assert result.verification_records_inserted == 2
    assert result.filter_decisions_inserted == 2

    # 3. Verify PostgreSQL database state
    async with session_factory() as session:
        # Check canonical jobs updated
        refreshed_job_a = await session.get(Job, job_a_id)
        assert refreshed_job_a.source_verified is True
        assert refreshed_job_a.location_verified is True
        assert refreshed_job_a.verification_confidence == Decimal("0.60")

        refreshed_job_b = await session.get(Job, job_b_id)
        assert refreshed_job_b.eligibility_status == "REJECTED"
        assert refreshed_job_b.rejection_reason == "LOCATION_NOT_ALLOWED"
        assert refreshed_job_b.review_reason is None
        assert refreshed_job_b.source_verified is True
        assert refreshed_job_b.normalized_location == "Tel Aviv"
        assert refreshed_job_b.verification_confidence == Decimal("0.50")

        # Check total audit rows: 2 initial + 2 new = 4 total
        v_total = (
            await session.execute(
                select(func.count(VerificationRecord.id)).where(
                    VerificationRecord.job_id.in_([job_a_id, job_b_id])
                )
            )
        ).scalar()
        assert v_total == 4

        f_total = (
            await session.execute(
                select(func.count(FilterDecision.id)).where(
                    FilterDecision.job_id.in_([job_a_id, job_b_id])
                )
            )
        ).scalar()
        assert f_total == 4

        # Verify initial audit records are perfectly preserved
        for v_id in init_v_ids:
            old_v = await session.get(VerificationRecord, v_id)
            assert old_v is not None
            assert old_v.source_verified is False  # Original untouched
            assert old_v.audit_trail == {"initial": True}

        for f_id in init_f_ids:
            old_f = await session.get(FilterDecision, f_id)
            assert old_f is not None
            assert old_f.evaluation_context == {"initial": True}


@pytest.mark.asyncio
async def test_rollback_after_simulated_failure() -> None:
    """Verify failure mid-transaction completely reverts all writes (atomicity)."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company, _, _, jobs, _, _ = await _seed_refresh_test_data(session)
        company_id = company.id
        job_b_id = jobs[1].id

    service = JobRefreshService()

    # Attempt apply_refresh within an aborted transaction
    with pytest.raises(RuntimeError, match="Simulated mid-transaction failure"):
        async with session_factory() as session:
            await service.apply_refresh(
                session=session,
                company_id_or_name=company_id,
                expected_database="job_intelligence_test",
                dry_run=False,
            )
            # Simulate unexpected crash before commit
            raise RuntimeError("Simulated mid-transaction failure")

    # Assert database rolled back completely
    async with session_factory() as session:
        job_b = await session.get(Job, job_b_id)
        assert job_b.eligibility_status == "REVIEW"  # NOT updated
        assert job_b.review_reason == "POSTED_DATE_SUSPICIOUS"

        v_cnt = (
            await session.execute(
                select(func.count(VerificationRecord.id)).where(
                    VerificationRecord.job_id == job_b_id
                )
            )
        ).scalar()
        assert v_cnt == 1  # Only the original initial record


@pytest.mark.asyncio
async def test_duplicate_execution_is_idempotent() -> None:
    """Verify repeated execution skips cleanly when all jobs are already up to date."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company, _, _, _, _, _ = await _seed_refresh_test_data(session)
        company_id = company.id

    service = JobRefreshService()

    # Run 1: Applies updates
    async with session_factory() as session:
        res1 = await service.apply_refresh(
            session=session,
            company_id_or_name=company_id,
            expected_database="job_intelligence_test",
            dry_run=False,
        )
        await session.commit()
    assert res1.status == "SUCCESS"
    assert res1.jobs_updated == 2

    # Run 2: Immediate duplicate run
    async with session_factory() as session:
        res2 = await service.apply_refresh(
            session=session,
            company_id_or_name=company_id,
            expected_database="job_intelligence_test",
            dry_run=False,
        )
        await session.commit()

    # Assert idempotency: skipped without writes
    assert res2.status == "SKIPPED_ALREADY_APPLIED"
    assert res2.applied is False
    assert res2.jobs_updated == 0
    assert res2.verification_records_inserted == 0
    assert res2.filter_decisions_inserted == 0

    # Assert database audit rows: Run 1 inserted 2, Run 2 inserted 0 (total = 2 initial + 2 Run 1 = 4)
    async with session_factory() as session:
        v_total = (await session.execute(select(func.count(VerificationRecord.id)))).scalar()
        f_total = (await session.execute(select(func.count(FilterDecision.id)))).scalar()
        assert v_total == 4
        assert f_total == 4


@pytest.mark.asyncio
async def test_wrong_database_identity_rejection() -> None:
    """Verify apply_refresh refuses execution when expected_database does not match."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company, _, _, _, _, _ = await _seed_test_data_company(session)
        company_id = company.id

    service = JobRefreshService()
    async with session_factory() as session:
        with pytest.raises(
            PermissionError, match="strictly restricted to allowlisted test database"
        ):
            await service.apply_refresh(
                session=session,
                company_id_or_name=company_id,
                expected_database="wrong_database_name",
                dry_run=False,
            )


@pytest.mark.asyncio
async def test_development_database_safety_guard() -> None:
    """Verify apply_refresh strictly forbids targeting 'job_intelligence_dev'."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        company, _, _, _, _, _ = await _seed_test_data_company(session)
        company_id = company.id

    service = JobRefreshService()
    async with session_factory() as session:
        with pytest.raises(
            PermissionError, match="strictly restricted to allowlisted test database"
        ):
            await service.apply_refresh(
                session=session,
                company_id_or_name=company_id,
                expected_database="job_intelligence_dev",
                dry_run=False,
            )


async def _seed_test_data_company(session) -> tuple[Company, Any, Any, Any, Any, Any]:
    return await _seed_refresh_test_data(session)


@pytest.mark.asyncio
async def test_cli_refresh_preview_and_apply_integration(tmp_path: Path) -> None:
    """Verify CLI refresh subcommand executes preview, apply, and idempotency checks."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        comp, _, _, _, _, _ = await _seed_refresh_test_data(session)
        company_name = comp.name

    parser = build_parser()

    # 1. Preview mode (read-only)
    args_prev = parser.parse_args(["refresh", "-c", company_name, "--preview"])
    code_prev = await async_main(args_prev)
    assert code_prev == 0

    # 2. Preview JSON mode with output file
    prev_file = tmp_path / "preview_plan.json"
    args_json_prev = parser.parse_args(
        ["refresh", "-c", company_name, "--preview", "--json", "-o", str(prev_file)]
    )
    code_json_prev = await async_main(args_json_prev)
    assert code_json_prev == 0
    assert prev_file.exists()

    with open(prev_file, encoding="utf-8") as f:
        plan_data = json.load(f)
    assert plan_data["company_name"] == company_name
    assert plan_data["jobs_to_update"] == 2

    # 3. Apply mode without --target-database -> fails (exit code 2)
    args_no_target = parser.parse_args(["refresh", "-c", company_name, "--apply"])
    code_no_target = await async_main(args_no_target)
    assert code_no_target == 2

    # 4. Apply mode targeting dev DB -> blocked by safety guard (exit code 2)
    args_dev_target = parser.parse_args(
        ["refresh", "-c", company_name, "--apply", "--target-database", "job_intelligence_dev"]
    )
    code_dev_target = await async_main(args_dev_target)
    assert code_dev_target == 2

    # 4b. Apply mode targeting unknown/unauthorized DB -> blocked by safety guard (exit code 2)
    args_unknown_target = parser.parse_args(
        ["refresh", "-c", company_name, "--apply", "--target-database", "production_db"]
    )
    code_unknown_target = await async_main(args_unknown_target)
    assert code_unknown_target == 2

    # 5. Apply mode targeting test database -> succeeds (exit code 0)
    apply_file = tmp_path / "apply_result.json"
    args_apply = parser.parse_args(
        [
            "refresh",
            "-c",
            company_name,
            "--apply",
            "--target-database",
            "job_intelligence_test",
            "-o",
            str(apply_file),
        ]
    )
    code_apply = await async_main(args_apply)
    assert code_apply == 0
    assert apply_file.exists()

    with open(apply_file, encoding="utf-8") as f:
        res_data = json.load(f)
    assert res_data["status"] == "SUCCESS"
    assert res_data["jobs_updated"] == 2

    # 6. Duplicate apply mode -> idempotent skip (exit code 0)
    args_dup = parser.parse_args(
        ["refresh", "-c", company_name, "--apply", "--target-database", "job_intelligence_test"]
    )
    code_dup = await async_main(args_dup)
    assert code_dup == 0


@pytest.mark.asyncio
async def test_cli_refresh_dev_override_without_token_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify CLI --allow-database-override is blocked when authorization token is missing or invalid."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp, _, _, _, _, _ = await _seed_refresh_test_data(session)
        company_name = comp.name

    parser = build_parser()

    # 1. Flag provided, token missing
    monkeypatch.delenv("FIGMA_REFRESH_AUTHORIZATION_TOKEN", raising=False)
    args_no_token = parser.parse_args(
        [
            "refresh",
            "-c",
            company_name,
            "--apply",
            "--target-database",
            "job_intelligence_dev",
            "--allow-database-override",
            "job_intelligence_dev",
        ]
    )
    code_no_token = await async_main(args_no_token)
    assert code_no_token == 2

    # 2. Flag provided, token invalid
    monkeypatch.setenv("FIGMA_REFRESH_AUTHORIZATION_TOKEN", "BAD_TOKEN_VALUE")
    code_bad_token = await async_main(args_no_token)
    assert code_bad_token == 2


@pytest.mark.asyncio
async def test_cli_refresh_dev_override_rejected_for_arbitrary_database() -> None:
    """Verify CLI --allow-database-override cannot bypass protection for non-dev databases."""
    parser = build_parser()
    args_arbitrary = parser.parse_args(
        [
            "refresh",
            "-c",
            "Figma",
            "--apply",
            "--target-database",
            "production_db",
            "--allow-database-override",
            "production_db",
        ]
    )
    code = await async_main(args_arbitrary)
    assert code == 2


@pytest.mark.asyncio
async def test_decoupled_output_file_failure_logs_warning_without_rollback(tmp_path: Path) -> None:
    """Verify that if output file writing fails after commit, the transaction remains committed."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp, _, _, jobs, _, _ = await _seed_refresh_test_data(session)
        company_name = comp.name
        job_b_id = jobs[1].id

    parser = build_parser()

    # Pass an impossible file path (a directory path where a file cannot be written)
    bad_file_path = str(
        tmp_path
    )  # tmp_path is a directory! Opening directory as file fails with IsADirectoryError / PermissionError / OSError

    args = parser.parse_args(
        [
            "refresh",
            "-c",
            company_name,
            "--apply",
            "--target-database",
            "job_intelligence_test",
            "-o",
            bad_file_path,
        ]
    )
    code = await async_main(args)
    # The commit succeeded, warning was emitted, exit code is 0
    assert code == 0

    # Verify that the database was committed (Job B updated from REVIEW to REJECTED)
    async with session_factory() as session:
        job_b = await session.get(Job, job_b_id)
        assert job_b.eligibility_status == "REJECTED"
        assert job_b.rejection_reason == "LOCATION_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_refresh_with_figma_company_id_in_test_database_does_not_trigger_dev_invariants() -> (
    None
):
    """Verify that a test database refresh using FIGMA_COMPANY_ID does not trigger 138-job dev invariants."""
    session_factory = get_sessionmaker()

    # Seed 1 test job associated with FIGMA_COMPANY_ID in test database
    async with session_factory() as session:
        comp = await session.get(Company, FIGMA_COMPANY_ID)
        if comp is None:
            comp = Company(
                id=FIGMA_COMPANY_ID,
                name="Figma Test Isolated",
                normalized_name="figma-test-isolated",
                sector="Technology",
                business_model="PRODUCT",
                is_verified=True,
            )
            session.add(comp)
            await session.flush()

        res = await session.execute(select(Source).where(Source.source_name == "Greenhouse"))
        source = res.scalar_one_or_none()
        if source is None:
            source = Source(
                source_name="Greenhouse",
                source_type="ATS",
                base_url="https://api.greenhouse.io/v1/boards",
                is_active=True,
            )
            session.add(source)
            await session.flush()

        unique_suffix = uuid.uuid4().hex[:6]
        cs = CompanySource(
            company_id=comp.id,
            source_id=source.id,
            ats_identifier=f"testboard_{unique_suffix}",
            discovery_method="MANUAL_ONBOARDING",
            is_active=True,
        )
        session.add(cs)
        await session.flush()

        now_utc = datetime.now(UTC)
        c_run = CollectionRun(
            source_id=source.id,
            company_source_id=cs.id,
            run_type="HARVEST",
            status="SUCCESS",
            records_discovered=1,
            records_ingested=1,
            records_failed=0,
            started_at=now_utc,
            finished_at=now_utc,
        )
        session.add(c_run)
        await session.flush()

        raw_j = RawJob(
            company_source_id=cs.id,
            collection_run_id=c_run.id,
            source_job_id=f"9901_{unique_suffix}",
            source_job_id_type="NATIVE",
            source_url="https://api.greenhouse.io/v1/boards/test/jobs/9901",
            payload_format="JSON",
            raw_body=b'{"id": 9901, "title": "Staff Engineer"}',
            raw_payload={"id": 9901, "title": "Staff Engineer"},
            content_hash=hashlib.sha256(b'{"id": 9901, "title": "Staff Engineer"}').hexdigest(),
            http_status=200,
            collected_at=now_utc,
        )
        session.add(raw_j)
        await session.flush()

        job = Job(
            company_id=comp.id,
            company_name=comp.name,
            title="Staff Platform Engineer",
            normalized_title="staff platform engineer",
            location="San Francisco, CA",
            normalized_location="San Francisco",
            remote_type="ON_SITE",
            description="Staff platform engineering role in distributed systems.",
            application_url="https://boards.greenhouse.io/test/jobs/9901",
            employment_type="FULL_TIME",
            role_priority="P1",
            relevance_status="TARGET",
            eligibility_status="REVIEW",
            source_verified=False,
            jd_verified=True,
            company_verified=False,
            location_verified=False,
            experience_verified=False,
            salary_verified=False,
            verification_confidence=Decimal("0.30"),
            collected_at=now_utc,
        )
        session.add(job)
        await session.flush()

        ref = JobSourceReference(
            job_id=job.id,
            raw_job_id=raw_j.id,
            source_id=source.id,
            source_job_id=f"9901_{unique_suffix}",
            source_url=raw_j.source_url,
            application_url=job.application_url,
            is_primary=True,
            collected_at=now_utc,
        )
        session.add(ref)
        await session.commit()

    service = JobRefreshService()
    async with session_factory() as session:
        # Applying refresh against test DB MUST succeed and NOT raise 138-job error
        res = await service.apply_refresh(
            session=session,
            company_id_or_name=FIGMA_COMPANY_ID,
            expected_database="job_intelligence_test",
            dry_run=False,
        )
        await session.commit()

    assert res.status == "SUCCESS"
    assert res.target_database == "job_intelligence_test"
    assert res.jobs_updated == 1


@pytest.mark.asyncio
async def test_pre_commit_invariant_failure_triggers_atomic_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that if an in-transaction pre-commit invariant fails, the entire transaction rolls back."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp, _, _, jobs, _, _ = await _seed_refresh_test_data(session)
        job_b_id = jobs[1].id

    service = JobRefreshService()

    # Monkeypatch _validate_pre_commit_invariants to simulate an invariant failure
    async def mock_fail_invariants(*args, **kwargs):
        raise RuntimeError("Simulated pre-commit invariant failure: data mismatch")

    monkeypatch.setattr(service, "_validate_pre_commit_invariants", mock_fail_invariants)

    # Attempt execute_refresh_workflow using the mocked service
    with pytest.raises(RuntimeError, match="Simulated pre-commit invariant failure"):
        async with session_factory() as session:
            try:
                await service.apply_refresh(
                    session=session,
                    company_id_or_name=comp.id,
                    expected_database="job_intelligence_test",
                    dry_run=False,
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Verify that the database was rolled back (Job B remains in REVIEW status)
    async with session_factory() as session:
        job_b = await session.get(Job, job_b_id)
        assert job_b.eligibility_status == "REVIEW"
        assert job_b.review_reason == "POSTED_DATE_SUSPICIOUS"
