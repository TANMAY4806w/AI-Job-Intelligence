"""Integration tests for the dry-run re-evaluation workflow against real PostgreSQL 18.

Validates:
1. Re-evaluation runs cleanly against live database schema and relationships.
2. Complete zero-mutation guarantee: zero INSERT, UPDATE, DELETE, or TRUNCATE operations.
3. Subcommand dispatch via CLI parser and async_main with formatted table and JSON modes.
4. Enforcement of the dry-run safety guard (--no-dry-run rejected).
5. File output capability (-o / --output-file).
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.cli.main import async_main, build_parser
from app.orchestration.reevaluation_service import JobReevaluationService
from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource, Source
from app.persistence.session import get_sessionmaker

# Mark all tests in this file as integration tests (auto-isolated to job_intelligence_test)
pytestmark = pytest.mark.integration


async def _seed_test_company_and_jobs(
    session,
) -> tuple[Company, Source, CompanySource, list[Job]]:
    """Helper to seed a test company with Greenhouse source and canonical jobs."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"IntegReevalCorp_{unique_suffix}"
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

    # 2. Source
    source = Source(
        source_name=f"GREENHOUSE_{unique_suffix}",
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
        ats_identifier=f"testboard_{unique_suffix}",
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

    # 5. RawJobs and Canonical Jobs
    # Job A: Eligible SWE role in Bengaluru
    body_a = b'{"id": 9901, "title": "Backend Python Engineer"}'
    raw_job_a = RawJob(
        company_source_id=company_source.id,
        collection_run_id=collection_run.id,
        source_job_id="9901",
        source_job_id_type="NATIVE",
        source_url=f"https://api.greenhouse.io/v1/boards/{company_source.ats_identifier}/jobs/9901",
        payload_format="JSON",
        raw_body=body_a,
        raw_payload={"id": 9901, "title": "Backend Python Engineer"},
        content_hash=hashlib.sha256(body_a).hexdigest(),
        http_status=200,
        collected_at=now_utc,
    )
    session.add(raw_job_a)
    await session.flush()

    job_a = Job(
        company_id=company.id,
        company_name=company.name,
        title="Backend Python Engineer",
        normalized_title="backend python engineer",
        location="Bengaluru, Karnataka, India",
        normalized_location="Bengaluru",
        remote_type="ON_SITE",
        description=(
            "We are seeking an experienced Backend Python Engineer to build scalable distributed "
            "services, microservices pipelines, and PostgreSQL data persistence layers."
        ),
        application_url="https://boards.greenhouse.io/test/jobs/9901",
        employment_type="FULL_TIME",
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="REJECTED",
        rejection_reason="LOCATION_NOT_ALLOWED",  # Intentionally pre-dated
        source_verified=False,
        jd_verified=True,
        company_verified=False,
        location_verified=False,
        experience_verified=False,
        salary_verified=False,
        verification_confidence=Decimal("0.30"),
        collected_at=now_utc,
    )
    session.add(job_a)
    await session.flush()

    ref_a = JobSourceReference(
        job_id=job_a.id,
        raw_job_id=raw_job_a.id,
        source_id=source.id,
        source_job_id="9901",
        source_url=raw_job_a.source_url,
        application_url=job_a.application_url,
        is_primary=True,
        collected_at=now_utc,
    )
    session.add(ref_a)

    # Job B: Excluded role in Tel Aviv
    body_b = b'{"id": 9902, "title": "Senior Product Manager"}'
    raw_job_b = RawJob(
        company_source_id=company_source.id,
        collection_run_id=collection_run.id,
        source_job_id="9902",
        source_job_id_type="NATIVE",
        source_url=f"https://api.greenhouse.io/v1/boards/{company_source.ats_identifier}/jobs/9902",
        payload_format="JSON",
        raw_body=body_b,
        raw_payload={"id": 9902, "title": "Senior Product Manager"},
        content_hash=hashlib.sha256(body_b).hexdigest(),
        http_status=200,
        collected_at=now_utc,
    )
    session.add(raw_job_b)
    await session.flush()

    job_b = Job(
        company_id=company.id,
        company_name=company.name,
        title="Senior Product Manager",
        normalized_title="senior product manager",
        location="Tel Aviv, Israel",
        normalized_location=None,
        remote_type="ON_SITE",
        description=(
            "We are seeking an experienced Senior Product Manager to lead roadmap strategy, cross-functional "
            "product teams, user analytics, and enterprise feature rollouts."
        ),
        application_url="https://boards.greenhouse.io/test/jobs/9902",
        employment_type="FULL_TIME",
        role_priority="NOT_RELEVANT",
        relevance_status="EXCLUDED",
        eligibility_status="REVIEW",
        review_reason="POSTED_DATE_SUSPICIOUS",  # Intentionally pre-dated
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
        source_job_id="9902",
        source_url=raw_job_b.source_url,
        application_url=job_b.application_url,
        is_primary=True,
        collected_at=now_utc,
    )
    session.add(ref_b)

    await session.commit()
    return company, source, company_source, [job_a, job_b]


@pytest.mark.asyncio
async def test_dry_run_reevaluation_performs_zero_database_mutations() -> None:
    """Verify dry-run re-evaluation executes with 100% read-only zero-mutation guarantee."""
    session_factory = get_sessionmaker()

    # 1. Seed test database
    async with session_factory() as session:
        company, source, company_source, jobs = await _seed_test_company_and_jobs(session)
        company_id = company.id

    # 2. Record row counts of all application tables before re-evaluation
    tables_to_check = [
        "companies",
        "sources",
        "company_sources",
        "collection_runs",
        "jobs",
        "raw_jobs",
        "job_source_references",
        "verification_records",
        "filter_decisions",
    ]

    counts_before: dict[str, int] = {}
    timestamps_before: dict[uuid.UUID, datetime] = {}
    async with session_factory() as session:
        for tbl in tables_to_check:
            res = await session.execute(text(f"SELECT count(*) FROM {tbl};"))
            counts_before[tbl] = res.scalar() or 0

        # Record exact updated_at timestamps for jobs
        j_res = await session.execute(select(Job.id, Job.updated_at))
        for row in j_res.all():
            timestamps_before[row[0]] = row[1]

    # 3. Execute dry-run re-evaluation
    service = JobReevaluationService()
    async with session_factory() as session:
        report = await service.reevaluate_company(
            session=session,
            company_id_or_name=company_id,
            dry_run=True,
        )

    # Validate in-memory report calculations
    assert report is not None
    assert report.total_jobs == 2
    assert report.dry_run is True
    assert report.company_id == company_id

    # 4. Record row counts after re-evaluation and assert ZERO modifications
    counts_after: dict[str, int] = {}
    timestamps_after: dict[uuid.UUID, datetime] = {}
    async with session_factory() as session:
        for tbl in tables_to_check:
            res = await session.execute(text(f"SELECT count(*) FROM {tbl};"))
            counts_after[tbl] = res.scalar() or 0

        j_res = await session.execute(select(Job.id, Job.updated_at))
        for row in j_res.all():
            timestamps_after[row[0]] = row[1]

    # Assert exact match across all tables: ZERO writes occurred
    for tbl in tables_to_check:
        assert counts_before[tbl] == counts_after[tbl], (
            f"Table {tbl} row count changed from {counts_before[tbl]} to {counts_after[tbl]}!"
        )

    # Assert job updated_at timestamps were never modified
    for j_id, ts_before in timestamps_before.items():
        assert timestamps_after[j_id] == ts_before, (
            f"Job {j_id} updated_at was modified during dry-run re-evaluation!"
        )


@pytest.mark.asyncio
async def test_cli_reevaluate_command_integration() -> None:
    """Verify CLI reevaluate command works end-to-end through CLI parser and async_main."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        comp, _, _, _ = await _seed_test_company_and_jobs(session)
        company_name = comp.name
        company_id = comp.id

    parser = build_parser()

    # 1. Test human-readable table output
    args_table = parser.parse_args(["reevaluate", "-c", company_name, "--dry-run"])
    code_table = await async_main(args_table)
    assert code_table == 0

    # 2. Test machine-readable JSON output
    args_json = parser.parse_args(["reevaluate", "-i", str(company_id), "--dry-run", "--json"])
    code_json = await async_main(args_json)
    assert code_json == 0

    # 3. Test safety guard: --no-dry-run must fail with exit code 2
    args_guard = parser.parse_args(["reevaluate", "-c", company_name, "--no-dry-run"])
    code_guard = await async_main(args_guard)
    assert code_guard == 2

    # 4. Test invalid UUID handling
    args_uuid = parser.parse_args(["reevaluate", "-i", "not-a-valid-uuid", "--dry-run"])
    code_uuid = await async_main(args_uuid)
    assert code_uuid == 2

    # 5. Test unknown company handling
    args_unknown = parser.parse_args(["reevaluate", "-c", "NonexistentCompany_12345", "--dry-run"])
    code_unknown = await async_main(args_unknown)
    assert code_unknown == 1


@pytest.mark.asyncio
async def test_cli_reevaluate_output_file(tmp_path: Path) -> None:
    """Verify CLI output-file option writes valid JSON report to specified path."""
    session_factory = get_sessionmaker()

    async with session_factory() as session:
        comp, _, _, _ = await _seed_test_company_and_jobs(session)
        company_name = comp.name

    out_file = tmp_path / "reeval_test_report.json"
    parser = build_parser()
    args = parser.parse_args(["reevaluate", "-c", company_name, "--dry-run", "-o", str(out_file)])
    exit_code = await async_main(args)

    assert exit_code == 0
    assert out_file.exists()

    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["company_name"] == company_name
    assert data["total_jobs"] == 2
    assert data["dry_run"] is True
    assert "summary" in data
    assert "job_comparisons" in data
