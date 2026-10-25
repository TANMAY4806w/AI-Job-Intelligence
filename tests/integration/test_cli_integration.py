"""Integration tests for the operator CLI using real PostgreSQL 18.

Validates:
1. End-to-end Greenhouse onboarding and idempotency in PostgreSQL.
2. Anti-overwrite and token conflict enforcement against database partial unique indexes.
3. Full collection pipeline invocation via CLI functions and persistence of RawJobs/Jobs.
4. Failed-run diagnostic persistence (verifying CollectionRun with status="FAILED" is committed).
5. Collection execution by mapping ID.
6. Subcommand dispatch (onboard, list, collect) through main() entrypoint.
"""

import asyncio
import json
import uuid

import httpx
import pytest

from app.cli.greenhouse import (
    execute_greenhouse_collection,
    onboard_greenhouse_mapping,
)
from app.cli.main import main
from app.collectors.base.collector import (
    STATUS_FAILED,
    STATUS_SUCCESS,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import GREENHOUSE_API_BASE
from app.persistence.repositories.collection_run_repository import CollectionRunRepository
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.session import dispose_engine, get_sessionmaker, session_scope

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration


SAMPLE_LISTING = {
    "jobs": [
        {
            "id": 8801,
            "title": "Backend Python Engineer",
            "updated_at": "2026-03-01T12:00:00Z",
            "absolute_url": "https://boards.greenhouse.io/cli-integ/jobs/8801",
            "location": {"name": "Bengaluru, India"},
        }
    ],
    "meta": {"total": 1},
}

SAMPLE_DETAIL = {
    "id": 8801,
    "title": "Backend Python Engineer",
    "first_published": "2026-02-28T10:00:00Z",
    "updated_at": "2026-03-01T12:00:00Z",
    "content": "<p>Design and deploy distributed job collection services with Python and PostgreSQL.</p>",
    "absolute_url": "https://boards.greenhouse.io/cli-integ/jobs/8801",
    "location": {"name": "Bengaluru, India"},
    "departments": [{"id": 1, "name": "Engineering"}],
    "offices": [{"id": 1, "name": "Bengaluru"}],
}


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_onboard_greenhouse_mapping_persists_and_is_idempotent_integration() -> None:
    """Verify onboarding creates entities, flushes to DB, and reuses records without duplicates."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Integ Employer {unique_suffix}"
    board_token = f"integ-{unique_suffix}"

    # 1. First onboarding: creates Company, Source, and CompanySource
    async with session_scope() as session:
        comp, src, cs, created = await onboard_greenhouse_mapping(
            session, company_name, board_token
        )
        assert created is True
        assert comp.name == company_name
        assert src.source_name == "GREENHOUSE"
        assert cs.ats_identifier == board_token
        company_id = comp.id
        source_id = src.id
        cs_id = cs.id
        await session.commit()

    # 2. Verify in fresh session that rows exist in DB
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp_repo = CompanyRepository(session)
        src_repo = SourceRepository(session)
        cs_repo = CompanySourceRepository(session)

        db_comp = await comp_repo.get_by_id(company_id)
        db_src = await src_repo.get_by_id(source_id)
        db_cs = await cs_repo.get_by_id(cs_id)

        assert db_comp is not None
        assert db_src is not None
        assert db_cs is not None
        assert db_cs.ats_identifier == board_token

    # 3. Repeated onboarding with identical parameters: reuses records without duplicates
    async with session_scope() as session:
        comp2, src2, cs2, created2 = await onboard_greenhouse_mapping(
            session, company_name, board_token
        )
        assert created2 is False
        assert comp2.id == company_id
        assert src2.id == source_id
        assert cs2.id == cs_id
        await session.commit()

    # 4. Anti-overwrite test: attempting to onboard same company with different token raises
    async with session_scope() as session:
        with pytest.raises(
            ValueError, match="Silently overwriting with 'different-token' is prohibited"
        ):
            await onboard_greenhouse_mapping(session, company_name, "different-token")

    # 5. Token conflict test: attempting to onboard different company with same token raises
    async with session_scope() as session:
        with pytest.raises(ValueError, match="already mapped to a different company"):
            await onboard_greenhouse_mapping(session, f"Other Corp {unique_suffix}", board_token)


@pytest.mark.asyncio
async def test_company_identity_collision_and_resolution_integration() -> None:
    """Verify names differing only by legal suffix collide on slug and require explicit resolution."""
    unique_suffix = uuid.uuid4().hex[:8]
    corp_name = f"{unique_suffix} Summit Corp"
    llc_name = f"{unique_suffix} Summit LLC"
    corp_token = f"summit-corp-{unique_suffix}"
    llc_token = f"summit-llc-{unique_suffix}"

    # 1. Onboard Summit Corp
    async with session_scope() as session:
        comp_corp, _, cs_corp, created = await onboard_greenhouse_mapping(
            session, corp_name, corp_token
        )
        assert created is True
        corp_id = comp_corp.id
        await session.commit()

    # 2. Onboard Summit LLC without explicit slug: must raise ambiguous identity error
    async with session_scope() as session:
        with pytest.raises(ValueError, match="Ambiguous company identity"):
            await onboard_greenhouse_mapping(session, llc_name, llc_token)

    # 3. Resolve collision by supplying explicit distinct slug
    llc_slug = f"summit-llc-{unique_suffix}"
    async with session_scope() as session:
        comp_llc, _, cs_llc, created_llc = await onboard_greenhouse_mapping(
            session, llc_name, llc_token, company_slug=llc_slug
        )
        assert created_llc is True
        assert comp_llc.normalized_name == llc_slug
        llc_id = comp_llc.id
        await session.commit()

    # 4. Verify in a fresh session that both distinct legal entities and their mappings coexist
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comp_repo = CompanyRepository(session)
        cs_repo = CompanySourceRepository(session)

        db_corp = await comp_repo.get_by_id(corp_id)
        db_llc = await comp_repo.get_by_id(llc_id)

        assert db_corp is not None
        assert db_llc is not None
        assert db_corp.id != db_llc.id
        assert db_corp.name == corp_name
        assert db_llc.name == llc_name

        corp_mappings = await cs_repo.list_by_company_id(corp_id)
        llc_mappings = await cs_repo.list_by_company_id(llc_id)

        assert len(corp_mappings) == 1
        assert corp_mappings[0].ats_identifier == corp_token

        assert len(llc_mappings) == 1
        assert llc_mappings[0].ats_identifier == llc_token


@pytest.mark.asyncio
async def test_collection_runner_expired_orm_instance_integration() -> None:
    """Verify CollectionRunner safely refreshes and executes on an expired CompanySource ORM instance."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Expired Corp {unique_suffix}"
    board_token = f"expired-{unique_suffix}"

    raw_listing_bytes = json.dumps(SAMPLE_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/8801":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    # 1. Onboard mapping
    async with session_scope() as session:
        _, _, company_source, _ = await onboard_greenhouse_mapping(
            session, company_name, board_token
        )
        cs_id = company_source.id
        await session.commit()

    # 2. In a new session, retrieve company_source and explicitly expire it
    async with httpx.AsyncClient(transport=transport) as raw_client:
        http_client = PoliteHttpClient(client=raw_client)
        async with session_scope() as session:
            cs_repo = CompanySourceRepository(session)
            cs = await cs_repo.get_by_id(cs_id)
            assert cs is not None

            # Expire the instance to test expired attribute handling
            session.expire(cs)

            outcome, exit_code = await execute_greenhouse_collection(
                session,
                cs,
                company_name=company_name,
                http_client=http_client,
            )

            assert exit_code == 0
            assert outcome.status == STATUS_SUCCESS
            assert outcome.records_discovered == 1
            assert outcome.records_ingested == 1


@pytest.mark.asyncio
async def test_cli_collect_end_to_end_integration() -> None:
    """Verify execute_greenhouse_collection harvests listings and commits records to PostgreSQL 18."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Collect Corp {unique_suffix}"
    board_token = f"collect-{unique_suffix}"

    raw_listing_bytes = json.dumps(SAMPLE_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/8801":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)

    # 1. Onboard mapping
    async with session_scope() as session:
        _, _, company_source, _ = await onboard_greenhouse_mapping(
            session, company_name, board_token
        )
        cs_id = company_source.id
        await session.commit()

    # 2. Run collection with mocked HTTP client
    async with httpx.AsyncClient(transport=transport) as raw_client:
        http_client = PoliteHttpClient(client=raw_client)
        async with session_scope() as session:
            cs_repo = CompanySourceRepository(session)
            cs = await cs_repo.get_by_id(cs_id)
            assert cs is not None

            outcome, exit_code = await execute_greenhouse_collection(
                session,
                cs,
                company_name=company_name,
                http_client=http_client,
            )

            assert exit_code == 0
            assert outcome.status == STATUS_SUCCESS
            assert outcome.records_discovered == 1
            assert outcome.records_ingested == 1
            assert outcome.collection_run_id is not None
            run_id = outcome.collection_run_id

    # 3. Verify PostgreSQL persistence in a fresh session
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        run_repo = CollectionRunRepository(session)
        cs_repo = CompanySourceRepository(session)

        db_run = await run_repo.get_by_id(run_id)
        assert db_run is not None
        assert db_run.status == "COMPLETED"
        assert db_run.records_discovered == 1
        assert db_run.records_ingested == 1
        assert db_run.finished_at is not None

        updated_cs = await cs_repo.get_by_id(cs_id)
        assert updated_cs is not None
        assert updated_cs.last_collected_at is not None


@pytest.mark.asyncio
async def test_cli_collect_failed_board_persists_diagnostic_run_integration() -> None:
    """Verify board failure returns exit code 1 while committing diagnostic CollectionRun."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"Failed Corp {unique_suffix}"
    board_token = f"failed-{unique_suffix}"

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Greenhouse board not found")

    transport = httpx.MockTransport(mock_handler)

    # 1. Onboard mapping
    async with session_scope() as session:
        _, _, company_source, _ = await onboard_greenhouse_mapping(
            session, company_name, board_token
        )
        cs_id = company_source.id
        await session.commit()

    # 2. Run collection that fails with HTTP 404
    async with httpx.AsyncClient(transport=transport) as raw_client:
        http_client = PoliteHttpClient(client=raw_client)
        async with session_scope() as session:
            cs_repo = CompanySourceRepository(session)
            cs = await cs_repo.get_by_id(cs_id)
            assert cs is not None

            outcome, exit_code = await execute_greenhouse_collection(
                session,
                cs,
                company_name=company_name,
                http_client=http_client,
            )

            assert exit_code == 1
            assert outcome.status == STATUS_FAILED
            assert outcome.collection_run_id is not None
            run_id = outcome.collection_run_id

    # 3. Verify diagnostic CollectionRun was persisted in PostgreSQL with status FAILED
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        run_repo = CollectionRunRepository(session)
        cs_repo = CompanySourceRepository(session)

        db_run = await run_repo.get_by_id(run_id)
        assert db_run is not None
        assert db_run.status == "FAILED"
        assert db_run.finished_at is not None
        assert "collector_status" in db_run.telemetry
        assert db_run.telemetry["collector_status"] == STATUS_FAILED

        updated_cs = await cs_repo.get_by_id(cs_id)
        assert updated_cs is not None
        assert updated_cs.last_collected_at is not None


def test_cli_main_entrypoint_onboard_and_list_integration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify main() entrypoint handles 'onboard' and 'list' commands against PostgreSQL 18."""
    unique_suffix = uuid.uuid4().hex[:8]
    company_name = f"CLI Entry Employer {unique_suffix}"
    board_token = f"entry-{unique_suffix}"

    # 1. Run onboard via CLI
    code_onboard = main(["greenhouse", "onboard", "-c", company_name, "-t", board_token])
    assert code_onboard == 0
    captured_onboard = capsys.readouterr()
    assert "GREENHOUSE ONBOARDING SUCCESSFUL" in captured_onboard.out
    assert company_name in captured_onboard.out
    assert board_token in captured_onboard.out

    # 2. Run list via CLI (text mode)
    code_list = main(["greenhouse", "list"])
    assert code_list == 0
    captured_list = capsys.readouterr()
    assert "CONFIGURED GREENHOUSE COMPANY MAPPINGS" in captured_list.out
    assert board_token in captured_list.out

    # 3. Run list via CLI (json mode)
    code_list_json = main(["greenhouse", "list", "--json"])
    assert code_list_json == 0
    captured_json = capsys.readouterr()
    data = json.loads(captured_json.out)
    assert isinstance(data, list)
    matching = [item for item in data if item["board_token"] == board_token]
    assert len(matching) == 1
    assert matching[0]["company_name"] == company_name
    mapping_id = matching[0]["mapping_id"]

    # 4. Collect via CLI using --mapping-id with mocked transport
    raw_listing_bytes = json.dumps(SAMPLE_LISTING).encode("utf-8")
    raw_detail_bytes = json.dumps(SAMPLE_DETAIL).encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs?content=true":
            return httpx.Response(200, content=raw_listing_bytes)
        elif url_str == f"{GREENHOUSE_API_BASE}/{board_token}/jobs/8801":
            return httpx.Response(200, content=raw_detail_bytes)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    from unittest.mock import patch

    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        code_collect = main(["greenhouse", "collect", "-m", mapping_id])
        assert code_collect == 0
        captured_collect = capsys.readouterr()
        assert "GREENHOUSE COLLECTION SUMMARY" in captured_collect.out
        assert "Final Status:        SUCCESS" in captured_collect.out
