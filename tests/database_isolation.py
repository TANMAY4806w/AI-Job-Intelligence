"""Test database isolation, safety guards, and cleanup utilities."""

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

PROTECTED_DATABASE_NAMES: Final[frozenset[str]] = frozenset(
    {"job_intelligence_dev", "job_intelligence_prod", "job_intelligence"}
)

APPLICATION_TABLES_ORDERED_FOR_TRUNCATE: Final[tuple[str, ...]] = (
    "job_source_references",
    "verification_records",
    "filter_decisions",
    "ai_analysis_results",
    "raw_jobs",
    "jobs",
    "collection_runs",
    "discovery_runs",
    "company_sources",
    "companies",
    "sources",
)


def validate_test_database_url(dev_db_url: str, test_db_url: str | None) -> str:
    """Validate that TEST_DATABASE_URL is explicitly set and distinct from development.

    Raises ValueError with actionable instructions if missing or pointing to
    a protected/development database.
    """
    if not test_db_url or not test_db_url.strip():
        raise ValueError(
            "Missing test-database configuration: TEST_DATABASE_URL is not configured. "
            "Integration tests require a dedicated test database distinct from the "
            "development database to prevent data pollution. Please set TEST_DATABASE_URL "
            "in your .env file or environment (e.g. "
            "TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_test)."
        )

    try:
        parsed_test = make_url(test_db_url.strip())
        parsed_dev = make_url(dev_db_url.strip()) if dev_db_url else None
    except Exception as exc:
        raise ValueError(f"Invalid database URL provided in TEST_DATABASE_URL: {exc}") from exc

    test_db_name = (parsed_test.database or "").lower()
    dev_db_name = (parsed_dev.database or "").lower() if parsed_dev else ""

    if not test_db_name:
        raise ValueError("TEST_DATABASE_URL does not specify a valid database name.")

    # Guard 1: Direct identity match with dev database
    if dev_db_name and test_db_name == dev_db_name:
        raise ValueError(
            f"Dangerous database target: TEST_DATABASE_URL points to the development database "
            f"('{test_db_name}'). Integration tests are strictly prohibited from running against "
            f"the development database to protect real data."
        )

    # Guard 2: Protected database names
    if test_db_name in PROTECTED_DATABASE_NAMES:
        raise ValueError(
            f"Dangerous database target: TEST_DATABASE_URL points to protected database "
            f"'{test_db_name}'. Integration tests must run against a dedicated test database "
            f"(such as 'job_intelligence_test')."
        )

    return test_db_url.strip()


async def clean_test_database(test_db_url: str) -> None:
    """Safely truncate all application tables in the dedicated test database.

    Enforces runtime fail-fast guards verifying the live connection target
    before issuing any destructive DDL/DML.
    """
    parsed = make_url(test_db_url)
    target_name = (parsed.database or "").lower()

    if target_name in PROTECTED_DATABASE_NAMES:
        raise RuntimeError(
            f"Refusing to execute destructive cleanup on protected database: {target_name}"
        )

    temp_engine = create_async_engine(test_db_url, echo=False)
    try:
        async with temp_engine.connect() as conn:
            # Runtime check of the active database name from PostgreSQL
            live_db = (await conn.execute(text("SELECT current_database();"))).scalar()
            live_db_name = (live_db or "").lower()

            if live_db_name in PROTECTED_DATABASE_NAMES or live_db_name != target_name:
                raise RuntimeError(
                    f"Safety violation: Live database connection reported '{live_db_name}', "
                    f"which does not match test target '{target_name}' or is protected!"
                )

            truncate_stmt = (
                f"TRUNCATE TABLE {', '.join(APPLICATION_TABLES_ORDERED_FOR_TRUNCATE)} "
                "RESTART IDENTITY CASCADE;"
            )
            await conn.execute(text(truncate_stmt))
            await conn.commit()
    finally:
        await temp_engine.dispose()
