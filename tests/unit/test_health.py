"""Unit tests for the health check endpoint."""

from fastapi.testclient import TestClient


def test_health_check_returns_200_and_ok_status(client: TestClient) -> None:
    """Verify that GET /health returns HTTP 200 with expected operational metadata."""
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["app"] == "AI Job Intelligence"
    assert "version" in data
    assert "environment" in data
