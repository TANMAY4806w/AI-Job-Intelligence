"""Integration regression tests verifying test database isolation against PostgreSQL 18.

Validates:
1. Integration test sessions connect exclusively to the dedicated test database (job_intelligence_test).
2. Committed records persist into job_intelligence_test and NEVER leak into job_intelligence_dev.
3. Development database data (including Figma's records) remains completely isolated and untouched.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.persistence.models.company import Company
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.session import get_sessionmaker, session_scope

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_integration_session_connects_to_test_database() -> None:
    """Verify that application sessions during integration tests connect to job_intelligence_test."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        result = await session.execute(text("SELECT current_database();"))
        active_db = result.scalar()
        assert active_db == "job_intelligence_test", (
            f"Expected connection to 'job_intelligence_test', but was connected to '{active_db}'!"
        )


@pytest.mark.asyncio
async def test_committed_integration_records_do_not_leak_to_development_db() -> None:
    """Verify committed records write to job_intelligence_test and NEVER appear in development db."""
    unique_suffix = uuid.uuid4().hex[:8]
    test_comp_name = f"Isolation Probe Corp {unique_suffix}"
    test_comp_slug = f"isolation-probe-{unique_suffix}"

    # 1. Commit record via standard application session scope
    async with session_scope() as session:
        comp_repo = CompanyRepository(session)
        created_comp = await comp_repo.create(
            Company(
                id=uuid.uuid4(),
                name=test_comp_name,
                normalized_name=test_comp_slug,
                sector="TECH",
                business_model="SAAS",
                is_verified=True,
                confidence=Decimal("1.00"),
            )
        )
        await session.commit()
        comp_id = created_comp.id

    # 2. Verify record exists in the active test database
    session_factory = get_sessionmaker()
    async with session_factory() as verify_test_session:
        test_repo = CompanyRepository(verify_test_session)
        assert await test_repo.get_by_id(comp_id) is not None

    # 3. Connect directly to the development database and verify zero leakage
    test_engine_url = session_factory().bind.url
    dev_url = test_engine_url.set(database="job_intelligence_dev")
    dev_engine = create_async_engine(dev_url, echo=False)
    try:
        async with dev_engine.connect() as dev_conn:
            # Confirm we are querying job_intelligence_dev
            dev_db = (await dev_conn.execute(text("SELECT current_database();"))).scalar()
            assert dev_db == "job_intelligence_dev"

            # Verify the probe company does NOT exist in development db
            check = await dev_conn.execute(
                text(
                    f"SELECT COUNT(*) FROM companies WHERE id = '{comp_id}' OR normalized_name = '{test_comp_slug}'"
                )
            )
            count = check.scalar()
            assert count == 0, (
                f"LEAK DETECTED: {test_comp_name} was committed into development database!"
            )

            # Verify Figma data is intact in development database
            figma_check = await dev_conn.execute(
                text("SELECT COUNT(*) FROM companies WHERE normalized_name = 'figma'")
            )
            assert figma_check.scalar() == 1, (
                "CRITICAL: Figma record in development database was altered!"
            )
    finally:
        await dev_engine.dispose()


@pytest.mark.asyncio
async def test_clean_test_database_clears_all_eleven_application_tables() -> None:
    """Verify that clean_test_database wipes all 11 application tables and preserves alembic_version."""
    from tests.database_isolation import (
        APPLICATION_TABLES_ORDERED_FOR_TRUNCATE,
        clean_test_database,
    )

    session_factory = get_sessionmaker()
    test_db_url = str(session_factory().bind.url.render_as_string(hide_password=False))

    # 1. Execute safe cleanup
    await clean_test_database(test_db_url)

    # 2. Check each of the 11 application tables has 0 rows
    async with session_factory() as session:
        for table_name in APPLICATION_TABLES_ORDERED_FOR_TRUNCATE:
            res = await session.execute(text(f"SELECT COUNT(*) FROM {table_name};"))
            row_count = res.scalar()
            assert row_count == 0, (
                f"Table '{table_name}' was not cleaned up! Row count: {row_count}"
            )

        # 3. Check alembic_version has not been wiped
        v_res = await session.execute(text("SELECT version_num FROM alembic_version;"))
        version = v_res.scalar()
        assert version == "05377f2ddb14", f"Alembic version corrupted or missing: {version}"
