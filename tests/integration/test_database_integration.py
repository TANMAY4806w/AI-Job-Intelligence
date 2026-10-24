"""Integration tests for live PostgreSQL database connectivity and ORM operations.

Requires an accessible PostgreSQL instance configured via application settings (.env).
Validates:
1. Low-level PostgreSQL connectivity via SELECT 1.
2. ORM record insertion, flushing, and querying using AsyncSession.
3. Transactional rollback guarantees ensuring zero test records remain in the database.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.persistence.models import Company, Source
from app.persistence.session import dispose_engine, get_engine, get_sessionmaker

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_postgresql_connectivity_select_one() -> None:
    """Verify live PostgreSQL server connectivity by executing SELECT 1."""
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1;"))
            scalar_value = result.scalar()
            assert scalar_value == 1
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: unable to connect to database. "
            f"Ensure PostgreSQL is running on port 5432 and .env is configured. Details: {exc}"
        )


@pytest.mark.asyncio
async def test_source_orm_insert_flush_read_and_rollback() -> None:
    """Verify ORM insert, flush, query, and rollback behavior on the Source model."""
    unique_suffix = uuid.uuid4().hex[:10].upper()
    test_source_name = f"INTEGRATION_TEST_SRC_{unique_suffix}"

    test_source = Source(
        id=uuid.uuid4(),
        source_name=test_source_name,
        source_type="ATS_API",
        base_url="https://integration-test.example.com/api",
        is_active=True,
        rate_limit_rpm=120,
        requires_browser=False,
    )

    session_factory = get_sessionmaker()

    try:
        # Step 1: Open an uncommitted transaction, insert, flush, and read back
        async with session_factory() as session:
            session.add(test_source)
            await session.flush()

            # Evict local objects from memory cache to force a real PostgreSQL query
            session.expunge_all()

            query = select(Source).where(Source.source_name == test_source_name)
            reloaded = (await session.execute(query)).scalar_one_or_none()

            assert reloaded is not None
            assert reloaded.id == test_source.id
            assert reloaded.source_name == test_source_name
            assert reloaded.source_type == "ATS_API"
            assert reloaded.rate_limit_rpm == 120

            # Step 2: Explicitly roll back the transaction
            await session.rollback()

        # Step 3: Verify the record was purged by the rollback and does not persist in the database
        async with session_factory() as verify_session:
            verify_query = select(Source).where(Source.source_name == test_source_name)
            persisted = (await verify_session.execute(verify_query)).scalar_one_or_none()
            assert persisted is None, "Test record was not rolled back; leaked into database!"
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and schema migration 091bce25752c is applied. Details: {exc}"
        )


@pytest.mark.asyncio
async def test_company_orm_insert_flush_read_and_rollback() -> None:
    """Verify ORM insert, flush, and rollback behavior on the canonical Company model."""
    unique_slug = f"integration-company-{uuid.uuid4().hex[:10]}"

    test_company = Company(
        id=uuid.uuid4(),
        name="Integration Test Employer Private Limited",
        normalized_name=unique_slug,
        website="https://integration-employer.example.com",
        industry="Enterprise Technology",
        sector="IT",
        business_model="PRODUCT",
        is_verified=True,
        confidence=Decimal("1.00"),
    )

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            session.add(test_company)
            await session.flush()

            session.expunge_all()

            query = select(Company).where(Company.normalized_name == unique_slug)
            fetched = (await session.execute(query)).scalar_one_or_none()

            assert fetched is not None
            assert fetched.id == test_company.id
            assert fetched.name == "Integration Test Employer Private Limited"
            assert fetched.normalized_name == unique_slug
            assert fetched.sector == "IT"
            assert fetched.confidence == Decimal("1.00")

            # Roll back to leave database clean
            await session.rollback()

        # Verify absence after rollback
        async with session_factory() as verify_session:
            verify_query = select(Company).where(Company.normalized_name == unique_slug)
            persisted = (await verify_session.execute(verify_query)).scalar_one_or_none()
            assert persisted is None, "Test company was not rolled back; leaked into database!"
    except (OSError, SQLAlchemyError) as exc:
        pytest.fail(
            f"PostgreSQL integration prerequisite unavailable: database operation failed. "
            f"Ensure PostgreSQL is running and schema migration 091bce25752c is applied. Details: {exc}"
        )
