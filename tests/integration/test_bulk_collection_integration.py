"""Live PostgreSQL integration tests for Multi-ATS Bulk Collection and Discovery.

Boundary: End-to-end integration tests using PostgreSQL 18 with mocked HTTP responses.
Validates:
1. Multi-ATS bulk collection across Greenhouse, Lever, and Ashby in a single execution.
2. Idempotent onboarding of Source, Company, and CompanySource records from registry.
3. Persistence of RawJob, Job, JobSourceReference, and CollectionRun records in PostgreSQL.
4. Error isolation: Failure on one ATS platform (e.g., HTTP 500) does not abort collection
   for remaining ATS platforms.
5. Idempotent rerun: Subsequent bulk collection runs recognize duplicates without creating
   duplicate canonical jobs.
6. Dry-run mode produces previews with zero mutations to PostgreSQL.
"""

import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.cli.collect import execute_bulk_collection
from app.collectors.ashby.collector import ASHBY_API_BASE
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import GREENHOUSE_API_BASE
from app.collectors.lever.collector import LEVER_API_BASE
from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource
from app.persistence.session import get_sessionmaker

pytestmark = pytest.mark.integration

SAMPLE_GH_LISTING = {
    "jobs": [
        {
            "id": 1001,
            "title": "Senior Backend Engineer - Python",
            "updated_at": "2026-03-01T10:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/testgh/jobs/1001",
            "location": {"name": "San Francisco, CA"},
        }
    ],
    "meta": {"total": 1},
}

SAMPLE_GH_DETAIL = {
    "id": 1001,
    "title": "Senior Backend Engineer - Python",
    "updated_at": "2026-03-01T10:00:00Z",
    "first_published": "2026-02-28T09:00:00Z",
    "absolute_url": "https://boards.greenhouse.io/testgh/jobs/1001",
    "content": "<p>Build high-scale distributed systems using Python and PostgreSQL.</p>",
    "location": {"name": "San Francisco, CA"},
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 2, "name": "San Francisco"}],
}

SAMPLE_LEVER_LISTING = [
    {
        "id": "lever-job-2001",
        "text": "Lead Machine Learning Engineer",
        "createdAt": 1772323200000,
        "hostedUrl": "https://jobs.lever.co/testlever/lever-job-2001",
        "applyUrl": "https://jobs.lever.co/testlever/lever-job-2001/apply",
        "categories": {
            "department": "AI Research",
            "location": "New York, NY",
            "team": "Machine Learning",
        },
        "descriptionPlain": "Develop LLM inference optimization engines.",
    }
]

SAMPLE_LEVER_DETAIL = {
    "id": "lever-job-2001",
    "text": "Lead Machine Learning Engineer",
    "createdAt": 1772323200000,
    "hostedUrl": "https://jobs.lever.co/testlever/lever-job-2001",
    "applyUrl": "https://jobs.lever.co/testlever/lever-job-2001/apply",
    "categories": {
        "department": "AI Research",
        "location": "New York, NY",
        "team": "Machine Learning",
    },
    "description": "<p>Develop LLM inference optimization engines.</p>",
}

SAMPLE_ASHBY_BOARD = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "ashby-job-3001",
            "title": "Platform Infrastructure Engineer",
            "department": "Infrastructure",
            "team": "Core Platform",
            "isRemote": True,
            "location": "Remote - US",
            "publishedAt": "2026-03-01T12:00:00.000Z",
            "jobUrl": "https://jobs.ashbyhq.com/testashby/ashby-job-3001",
            "applyUrl": "https://jobs.ashbyhq.com/testashby/ashby-job-3001/application",
            "descriptionHtml": "<p>Manage Kubernetes clusters and cloud deployments.</p>",
            "address": {
                "postalAddress": {"addressLocality": "San Francisco", "addressRegion": "CA"}
            },
        }
    ],
}


def _create_test_registry_file(tmp_path: Path, companies: list[dict]) -> Path:
    reg_file = tmp_path / "test_companies.json"
    reg_file.write_text(json.dumps(companies, indent=2), encoding="utf-8")
    return reg_file


@pytest.mark.asyncio
async def test_bulk_collection_end_to_end_multi_provider(tmp_path: Path) -> None:
    """Verify bulk collection across Greenhouse, Lever, and Ashby in a single run."""
    companies_config = [
        {
            "name": "Acme Greenhouse",
            "slug": "acme-gh",
            "ats_provider": "GREENHOUSE",
            "board_identifier": "testgh",
            "career_url": "https://acmegh.com/careers",
            "is_active": True,
        },
        {
            "name": "Acme Lever",
            "slug": "acme-lever",
            "ats_provider": "LEVER",
            "board_identifier": "testlever",
            "career_url": "https://acmelever.com/careers",
            "is_active": True,
        },
        {
            "name": "Acme Ashby",
            "slug": "acme-ashby",
            "ats_provider": "ASHBY",
            "board_identifier": "testashby",
            "career_url": "https://acmeashby.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Greenhouse mocks
        if url == f"{GREENHOUSE_API_BASE}/testgh/jobs?content=true":
            return httpx.Response(200, json=SAMPLE_GH_LISTING)
        elif url == f"{GREENHOUSE_API_BASE}/testgh/jobs/1001":
            return httpx.Response(200, json=SAMPLE_GH_DETAIL)
        # Lever mocks
        elif url.startswith(f"{LEVER_API_BASE}/testlever") and "lever-job-2001" not in url:
            return httpx.Response(200, json=SAMPLE_LEVER_LISTING)
        elif url == f"{LEVER_API_BASE}/testlever/lever-job-2001?mode=json":
            return httpx.Response(200, json=SAMPLE_LEVER_DETAIL)
        # Ashby mocks
        elif url == f"{ASHBY_API_BASE}/testashby?includeCompensation=true":
            return httpx.Response(200, json=SAMPLE_ASHBY_BOARD)

        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        http_client = PoliteHttpClient(client=client)

        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database="job_intelligence_test",
            http_client=http_client,
        )
        assert exit_code == 0

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Check that 3 CollectionRun records were created with status COMPLETED
        runs = (await session.execute(select(CollectionRun))).scalars().all()
        assert len(runs) == 3
        assert all(r.status == "COMPLETED" for r in runs)
        assert sum(r.records_ingested for r in runs) == 3

        # Check that 3 Company records exist
        comps = (await session.execute(select(Company))).scalars().all()
        assert len(comps) == 3
        comp_slugs = {c.normalized_name for c in comps}
        assert comp_slugs == {"acme-gh", "acme-lever", "acme-ashby"}

        # Check that 3 RawJob records exist (one per platform)
        raw_jobs = (await session.execute(select(RawJob))).scalars().all()
        assert len(raw_jobs) == 3

        # Check that 3 canonical Job records exist
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 3
        job_titles = {j.title for j in jobs}
        assert "Senior Backend Engineer - Python" in job_titles
        assert "Lead Machine Learning Engineer" in job_titles
        assert "Platform Infrastructure Engineer" in job_titles

        # Check that 3 JobSourceReference records exist
        refs = (await session.execute(select(JobSourceReference))).scalars().all()
        assert len(refs) == 3


@pytest.mark.asyncio
async def test_bulk_collection_partial_failure_isolation(tmp_path: Path) -> None:
    """Verify that failure on one ATS platform does not stop other platforms from succeeding."""
    companies_config = [
        {
            "name": "Failing Lever",
            "slug": "fail-lever",
            "ats_provider": "LEVER",
            "board_identifier": "faillever",
            "career_url": "https://faillever.com/careers",
            "is_active": True,
        },
        {
            "name": "Successful Ashby",
            "slug": "success-ashby",
            "ats_provider": "ASHBY",
            "board_identifier": "successashby",
            "career_url": "https://successashby.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Lever fails with HTTP 500
        if "faillever" in url:
            return httpx.Response(500, content=b"Internal Server Error")
        # Ashby succeeds
        elif url == f"{ASHBY_API_BASE}/successashby?includeCompensation=true":
            return httpx.Response(200, json=SAMPLE_ASHBY_BOARD)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        http_client = PoliteHttpClient(client=client)

        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database="job_intelligence_test",
            http_client=http_client,
        )
        # Exit code should be 1 indicating batch had failure(s)
        assert exit_code == 1

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Verify collection runs: 1 failed, 1 completed
        runs = (await session.execute(select(CollectionRun))).scalars().all()
        assert len(runs) == 2
        statuses = {r.status for r in runs}
        assert statuses == {"COMPLETED", "FAILED"}

        # Verify Ashby canonical job was successfully persisted despite Lever failure
        jobs = (await session.execute(select(Job))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].title == "Platform Infrastructure Engineer"


@pytest.mark.asyncio
async def test_bulk_collection_idempotent_rerun(tmp_path: Path) -> None:
    """Verify that rerunning collection is idempotent and detects duplicates."""
    companies_config = [
        {
            "name": "Acme Ashby",
            "slug": "acme-ashby",
            "ats_provider": "ASHBY",
            "board_identifier": "testashby",
            "career_url": "https://acmeashby.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ASHBY_API_BASE}/testashby?includeCompensation=true":
            return httpx.Response(200, json=SAMPLE_ASHBY_BOARD)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        http_client = PoliteHttpClient(client=client)

        # Run 1: initial ingestion
        exit_1 = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database="job_intelligence_test",
            http_client=http_client,
        )
        assert exit_1 == 0

        # Run 2: idempotent rerun with identical data
        exit_2 = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database="job_intelligence_test",
            http_client=http_client,
        )
        assert exit_2 == 0

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Two collection runs executed
        runs = (await session.execute(select(CollectionRun))).scalars().all()
        assert len(runs) == 2
        # Second run should have recorded duplicate
        assert runs[1].duplicates_detected == 1

        # Exactly 1 canonical job must exist in database
        job_count = (await session.execute(select(func.count(Job.id)))).scalar_one()
        assert job_count == 1


@pytest.mark.asyncio
async def test_bulk_collection_dry_run_zero_db_mutations(tmp_path: Path) -> None:
    """Verify that --dry-run produces no changes in PostgreSQL."""
    companies_config = [
        {
            "name": "Dry Run Corp",
            "slug": "dry-run-corp",
            "ats_provider": "LEVER",
            "board_identifier": "dryrun",
            "career_url": "https://dryrun.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        dry_run=True,
    )
    assert exit_code == 0

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # Assert database is completely empty of any records
        comp_count = (await session.execute(select(func.count(Company.id)))).scalar_one()
        job_count = (await session.execute(select(func.count(Job.id)))).scalar_one()
        run_count = (await session.execute(select(func.count(CollectionRun.id)))).scalar_one()

        assert comp_count == 0
        assert job_count == 0
        assert run_count == 0


@pytest.mark.asyncio
async def test_bulk_collection_default_preview_zero_db_mutations(tmp_path: Path) -> None:
    """Verify that omitting --apply defaults to read-only preview with zero mutations."""
    companies_config = [
        {
            "name": "Preview Corp",
            "slug": "preview-corp",
            "ats_provider": "GREENHOUSE",
            "board_identifier": "preview-corp",
            "career_url": "https://preview.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    # Calling without apply=True must default to preview
    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=False,
    )
    assert exit_code == 0

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp_count = (await session.execute(select(func.count(Company.id)))).scalar_one()
        job_count = (await session.execute(select(func.count(Job.id)))).scalar_one()
        run_count = (await session.execute(select(func.count(CollectionRun.id)))).scalar_one()

        assert comp_count == 0
        assert job_count == 0
        assert run_count == 0


@pytest.mark.asyncio
async def test_bulk_collection_target_database_mismatch_rejected(tmp_path: Path) -> None:
    """Verify that target database mismatch aborts execution with exit code 2."""
    companies_config = [
        {
            "name": "Acme Corp",
            "slug": "acme-corp",
            "ats_provider": "LEVER",
            "board_identifier": "acme",
            "career_url": "https://acme.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    # Specifying wrong database name must return 2
    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=True,
        target_database="non_existent_database_name",
    )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_missing_target_database_rejected(tmp_path: Path) -> None:
    """Verify that apply=True without target_database aborts execution with exit code 2."""
    companies_config = [
        {
            "name": "Acme Corp",
            "slug": "acme-corp",
            "ats_provider": "LEVER",
            "board_identifier": "acme",
            "career_url": "https://acme.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=True,
        target_database=None,
    )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_live_preview_zero_db_mutations(tmp_path: Path) -> None:
    """Verify that --preview-live makes read-only HTTP calls and performs zero DB mutations."""
    companies_config = [
        {
            "name": "Live Preview Corp",
            "slug": "live-preview-corp",
            "ats_provider": "GREENHOUSE",
            "board_identifier": "testgh",
            "career_url": "https://preview.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "boards-api.greenhouse.io" in url_str:
            return httpx.Response(200, json=SAMPLE_GH_LISTING, request=request)
        return httpx.Response(404, text="Not Found", request=request)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mock_client = PoliteHttpClient(client=client)

        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            preview_live=True,
            apply=False,
            http_client=mock_client,
        )
        assert exit_code == 0

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp_count = (await session.execute(select(func.count(Company.id)))).scalar_one()
        job_count = (await session.execute(select(func.count(Job.id)))).scalar_one()
        run_count = (await session.execute(select(func.count(CollectionRun.id)))).scalar_one()

        assert comp_count == 0
        assert job_count == 0
        assert run_count == 0


@pytest.mark.asyncio
async def test_bulk_collection_company_level_atomicity(tmp_path: Path) -> None:
    """Verify company-level atomicity: if Company A succeeds and Company B fails,

    Company A's data remains committed to PostgreSQL while Company B is isolated.
    """
    companies_config = [
        {
            "name": "Company Succeeded A",
            "slug": "company-succeeded-a",
            "ats_provider": "GREENHOUSE",
            "board_identifier": "testgh",
            "career_url": "https://company-a.com/careers",
            "is_active": True,
        },
        {
            "name": "Company Failed B",
            "slug": "company-failed-b",
            "ats_provider": "LEVER",
            "board_identifier": "failboard",
            "career_url": "https://company-b.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "boards-api.greenhouse.io" in url_str:
            if "jobs?content=true" in url_str:
                return httpx.Response(200, json=SAMPLE_GH_LISTING, request=request)
            return httpx.Response(200, json=SAMPLE_GH_DETAIL, request=request)
        elif "failboard" in url_str:
            return httpx.Response(500, text="Internal Server Error", request=request)
        return httpx.Response(404, text="Not Found", request=request)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mock_client = PoliteHttpClient(client=client)

        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database="job_intelligence_test",
            http_client=mock_client,
        )
        # Batch returns 1 because Company B failed
        assert exit_code == 1

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        # 1. Verify Company Succeeded A is committed in PostgreSQL
        comp_a = (
            await session.execute(select(Company).where(Company.name == "Company Succeeded A"))
        ).scalar_one_or_none()
        assert comp_a is not None
        assert comp_a.name == "Company Succeeded A"

        # Check that jobs for Company A are committed in PostgreSQL
        jobs_a = (
            (await session.execute(select(Job).where(Job.company_id == comp_a.id))).scalars().all()
        )
        assert len(jobs_a) == 1
        assert jobs_a[0].title == "Senior Backend Engineer - Python"

        # Check that CollectionRun for Company A is committed with COMPLETED status
        runs_a = (
            (
                await session.execute(
                    select(CollectionRun)
                    .join(CompanySource, CollectionRun.company_source_id == CompanySource.id)
                    .where(CompanySource.company_id == comp_a.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(runs_a) >= 1
        assert runs_a[0].status == "COMPLETED"

        # 2. Verify Company Failed B
        comp_b = (
            await session.execute(select(Company).where(Company.name == "Company Failed B"))
        ).scalar_one_or_none()
        if comp_b is not None:
            # If synced prior to collection failure, check that no canonical jobs were committed
            jobs_b = (
                (await session.execute(select(Job).where(Job.company_id == comp_b.id)))
                .scalars()
                .all()
            )
            assert len(jobs_b) == 0


@pytest.mark.asyncio
async def test_bulk_collection_dev_database_blocked_by_default(tmp_path: Path) -> None:
    """Verify that targeting job_intelligence_dev is blocked without explicit authorization."""
    companies_config = [
        {
            "name": "Dev Block Test",
            "slug": "dev-block-test",
            "ats_provider": "LEVER",
            "board_identifier": "testlever",
            "career_url": "https://devblock.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=True,
        target_database="job_intelligence_dev",
    )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_unknown_prod_database_blocked(tmp_path: Path) -> None:
    """Verify that unknown/prod databases are blocked by allowlist."""
    companies_config = [
        {
            "name": "Prod Block Test",
            "slug": "prod-block-test",
            "ats_provider": "LEVER",
            "board_identifier": "testlever",
            "career_url": "https://prodblock.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    for forbidden_db in ("job_intelligence_prod", "production", "postgres", "random_db"):
        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            apply=True,
            target_database=forbidden_db,
        )
        assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_dev_database_unconditionally_rejected_even_with_override_and_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that targeting job_intelligence_dev is unconditionally rejected even

    if --allow-database-override and FIGMA_REFRESH_AUTHORIZATION_TOKEN are provided.
    Bulk collection must remain strictly separated from Figma refresh authorization.
    """
    monkeypatch.setenv(
        "FIGMA_REFRESH_AUTHORIZATION_TOKEN", "AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14"
    )

    companies_config = [
        {
            "name": "Dev Rejection Test",
            "slug": "dev-rejection-test",
            "ats_provider": "LEVER",
            "board_identifier": "testlever",
            "career_url": "https://devreject.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    # Attempt execution with both override and token
    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=True,
        target_database="job_intelligence_dev",
        allow_database_override="job_intelligence_dev",
    )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_figma_cannot_bypass_dev_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that explicitly selecting Figma in bulk collection cannot bypass

    the strict prohibition on modifying job_intelligence_dev.
    """
    monkeypatch.setenv(
        "FIGMA_REFRESH_AUTHORIZATION_TOKEN", "AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14"
    )

    companies_config = [
        {
            "name": "Figma",
            "slug": "figma",
            "ats_provider": "GREENHOUSE",
            "board_identifier": "figma",
            "career_url": "https://figma.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        companies=["Figma"],
        apply=True,
        target_database="job_intelligence_dev",
        allow_database_override="job_intelligence_dev",
    )
    assert exit_code == 2


@pytest.mark.asyncio
async def test_bulk_collection_preview_live_empty_board_status_is_empty(
    tmp_path: Path,
) -> None:
    """Verify that an empty board in live preview is reported as STATUS_EMPTY ('EMPTY'), not 'SUCCESS'."""
    companies_config = [
        {
            "name": "Empty Vercel Ashby",
            "slug": "empty-vercel",
            "ats_provider": "ASHBY",
            "board_identifier": "emptyvercel",
            "career_url": "https://vercel.com/careers",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    # Mock Ashby endpoint returning 200 with 0 jobs
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiVersion": "1", "jobs": []}, request=request)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mock_client = PoliteHttpClient(client=client)

        out_file = str(tmp_path / "preview_out.json")
        exit_code = await execute_bulk_collection(
            registry_file=reg_file,
            preview_live=True,
            apply=False,
            output_file=out_file,
            http_client=mock_client,
        )
        assert exit_code == 0

        with open(out_file, encoding="utf-8") as f:
            data = json.load(f)

        assert data["companies_planned"] == 1
        assert data["companies_empty"] == 1
        assert data["companies_succeeded"] == 0
        assert data["total_discovered"] == 0
        assert data["companies"][0]["status"] == "EMPTY"
        assert "0 vacancies (empty board)" in data["companies"][0]["details"]


@pytest.mark.asyncio
async def test_bulk_collection_override_flag_strictly_rejected(tmp_path: Path) -> None:
    """Verify that passing --allow-database-override is rejected with exit code 2.

    Overrides are exclusive to single-company Figma refresh and prohibited in bulk collection.
    """
    companies_config = [
        {
            "name": "Override Rejection Test",
            "slug": "override-reject",
            "ats_provider": "LEVER",
            "board_identifier": "testlever",
            "career_url": "https://overridereject.com",
            "is_active": True,
        },
    ]
    reg_file = _create_test_registry_file(tmp_path, companies_config)

    # Even with valid test target database, providing an override flag is strictly rejected
    exit_code = await execute_bulk_collection(
        registry_file=reg_file,
        apply=True,
        target_database="job_intelligence_test",
        allow_database_override="job_intelligence_test",
    )
    assert exit_code == 2
