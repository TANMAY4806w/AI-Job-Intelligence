"""Global pytest configuration and fixtures."""

import asyncio
import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.config.settings import get_settings
from app.persistence.session import dispose_engine
from tests.database_isolation import clean_test_database, validate_test_database_url


@pytest.fixture
def client() -> TestClient:
    """Fixture providing an in-process HTTP TestClient for the FastAPI application."""
    return TestClient(app)


@pytest.fixture(autouse=True)
def enforce_test_database_isolation(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Ensure integration tests execute exclusively against a dedicated test database."""
    if "integration" not in request.keywords:
        yield
        return

    settings = get_settings()
    original_dev_url = settings.DATABASE_URL
    test_db_url = os.environ.get("TEST_DATABASE_URL") or settings.TEST_DATABASE_URL

    try:
        validated_test_url = validate_test_database_url(original_dev_url, test_db_url)
    except ValueError as exc:
        pytest.fail(str(exc))

    # Pre-test cleanup on dedicated test database
    try:
        asyncio.run(clean_test_database(validated_test_url))
    except Exception as exc:
        pytest.fail(f"Failed to reset test database before test execution: {exc}")

    # Redirect application settings and persistence engine to the test database
    prev_database_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = validated_test_url
    get_settings.cache_clear()
    asyncio.run(dispose_engine())

    try:
        yield
    finally:
        # Post-test cleanup on dedicated test database
        try:
            asyncio.run(dispose_engine())
            asyncio.run(clean_test_database(validated_test_url))
        except Exception:
            pass

        # Restore original environment and dispose test connection pool
        if prev_database_url is not None:
            os.environ["DATABASE_URL"] = prev_database_url
        else:
            os.environ.pop("DATABASE_URL", None)

        get_settings.cache_clear()
        asyncio.run(dispose_engine())
