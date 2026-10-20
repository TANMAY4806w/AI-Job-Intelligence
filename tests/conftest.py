"""Global pytest configuration and fixtures."""

import pytest
from fastapi.testclient import TestClient

from app.api.main import app


@pytest.fixture
def client() -> TestClient:
    """Fixture providing an in-process HTTP TestClient for the FastAPI application."""
    return TestClient(app)
