"""Unit tests for test database isolation rules, guards, and safety invariants."""

import pytest

from tests.database_isolation import (
    PROTECTED_DATABASE_NAMES,
    clean_test_database,
    validate_test_database_url,
)

DEV_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_dev"
TEST_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/job_intelligence_test"


def test_missing_test_database_url_raises_actionable_error() -> None:
    """Verify missing or empty TEST_DATABASE_URL fails fast with actionable instructions."""
    with pytest.raises(ValueError, match="Missing test-database configuration"):
        validate_test_database_url(DEV_URL, None)

    with pytest.raises(ValueError, match="Missing test-database configuration"):
        validate_test_database_url(DEV_URL, "")

    with pytest.raises(ValueError, match="Missing test-database configuration"):
        validate_test_database_url(DEV_URL, "   ")


def test_matching_dev_database_url_refuses_to_run() -> None:
    """Verify TEST_DATABASE_URL pointing to the development database raises a safety violation."""
    with pytest.raises(ValueError, match="Dangerous database target.*development database"):
        validate_test_database_url(DEV_URL, DEV_URL)

    # Different user/pass or port but same dev database name
    variant_dev_url = "postgresql+asyncpg://other_user:secret@127.0.0.1:5432/job_intelligence_dev"
    with pytest.raises(ValueError, match="Dangerous database target.*development database"):
        validate_test_database_url(DEV_URL, variant_dev_url)


@pytest.mark.parametrize("protected_name", list(PROTECTED_DATABASE_NAMES))
def test_protected_database_names_refuse_to_run(protected_name: str) -> None:
    """Verify known protected database names are rejected by fail-fast guards."""
    protected_url = f"postgresql+asyncpg://user:pass@localhost:5432/{protected_name}"
    with pytest.raises(
        ValueError, match="Dangerous database target.*(development|protected) database"
    ):
        validate_test_database_url(DEV_URL, protected_url)


def test_valid_dedicated_test_database_url_passes() -> None:
    """Verify valid dedicated test database URL passes validation cleanly."""
    result = validate_test_database_url(DEV_URL, TEST_URL)
    assert result == TEST_URL


def test_invalid_url_string_raises_error() -> None:
    """Verify malformed connection strings raise descriptive errors."""
    with pytest.raises(ValueError, match="Invalid database URL"):
        validate_test_database_url(DEV_URL, "not-a-valid-url:::")


@pytest.mark.asyncio
async def test_clean_test_database_refuses_development_database() -> None:
    """Verify clean_test_database immediately refuses to touch protected databases."""
    with pytest.raises(
        RuntimeError, match="Refusing to execute destructive cleanup on protected database"
    ):
        await clean_test_database(DEV_URL)


def test_all_eleven_application_models_included_in_truncate_list() -> None:
    """Verify that every application model table in Base.metadata is in the truncate tuple."""
    from app.persistence.models import Base
    from tests.database_isolation import APPLICATION_TABLES_ORDERED_FOR_TRUNCATE

    model_table_names = set(Base.metadata.tables.keys())
    truncate_table_names = set(APPLICATION_TABLES_ORDERED_FOR_TRUNCATE)

    assert len(model_table_names) == 11, (
        f"Expected 11 application tables, found: {len(model_table_names)}"
    )
    assert truncate_table_names == model_table_names, (
        f"Mismatch between application models and truncate list: "
        f"Missing from truncate: {model_table_names - truncate_table_names}, "
        f"Extra in truncate: {truncate_table_names - model_table_names}"
    )
