"""Unit tests for Job Feed, Review Queue, and Web Dashboard API endpoints."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference
from app.persistence.models.source import Source


def _create_mock_job(
    *,
    job_id: uuid.UUID | None = None,
    title: str = "Staff Backend Engineer",
    company_name: str = "Acme Corp",
    role_priority: str = "P1",
    eligibility_status: str = "ELIGIBLE",
    application_status: str = "NOT_APPLIED",
    review_reason: str | None = None,
    rejection_reason: str | None = None,
) -> Job:
    """Helper creating a detached Job model instance for unit testing."""
    jid = job_id or uuid.uuid4()
    cid = uuid.uuid4()

    job = Job(
        id=jid,
        company_id=cid,
        company_name=company_name,
        title=title,
        normalized_title=title.lower(),
        location="Remote, US",
        normalized_location="remote",
        employment_type="FULL_TIME",
        remote_type="REMOTE",
        description="We are looking for a Staff Engineer to build high-scale cloud systems.",
        responsibilities=["Architect services", "Lead engineers"],
        requirements=["10+ years experience", "Python expertise"],
        skills=["Python", "PostgreSQL", "FastAPI"],
        experience_raw="8-12 years",
        experience_min_years=Decimal("8.0"),
        experience_max_years=Decimal("12.0"),
        salary_min=Decimal("180000.00"),
        salary_max=Decimal("220000.00"),
        salary_currency="USD",
        salary_period="YEAR",
        posted_at=datetime.now(UTC),
        collected_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        application_url="https://jobs.example.com/acme/123",
        application_status=application_status,
        role_priority=role_priority,
        relevance_status="TARGET",
        eligibility_status=eligibility_status,
        rejection_reason=rejection_reason,
        review_reason=review_reason,
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        verification_confidence=Decimal("0.95"),
    )

    src = Source(
        id=uuid.uuid4(),
        source_name="greenhouse",
        source_type="ATS",
    )
    ref = JobSourceReference(
        id=uuid.uuid4(),
        job_id=jid,
        raw_job_id=uuid.uuid4(),
        source_id=src.id,
        source_job_id="gh-1234",
        source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/1234",
        application_url="https://jobs.example.com/acme/123",
        is_primary=True,
    )
    ref.source = src
    job.source_references = [ref]
    job.filter_decisions = []
    job.verification_records = []

    return job


@pytest.fixture
def test_client() -> TestClient:
    """Fixture providing TestClient with dummy database session dependency override."""
    mock_session = AsyncMock()

    async def override_get_db_session():
        yield mock_session

    from app.persistence.session import get_db_session

    app.dependency_overrides[get_db_session] = override_get_db_session
    client = TestClient(app)
    yield client
    app.dependency_overrides.pop(get_db_session, None)


def test_list_jobs_endpoint_success(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs returns paginated job listing with expected envelope."""
    sample_job = _create_mock_job()

    with patch(
        "app.api.v1.jobs.JobRepository.search_and_filter_jobs",
        new=AsyncMock(return_value=([sample_job], 1)),
    ):
        response = test_client.get("/api/v1/jobs?page=1&page_size=20&sort_by=freshness")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["total_pages"] == 1
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["title"] == "Staff Backend Engineer"
    assert item["company_name"] == "Acme Corp"
    assert item["role_priority"] == "P1"
    assert item["eligibility_status"] == "ELIGIBLE"
    assert item["primary_source"] == "greenhouse"
    assert item["salary_min"] == 180000.0


def test_list_jobs_invalid_sort_parameter(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs rejects unsupported sort_by values with HTTP 422."""
    response = test_client.get("/api/v1/jobs?sort_by=unsupported_order")
    assert response.status_code == 422


def test_get_job_filters_endpoint(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs/filters returns facet distributions and counts."""
    mock_facets = {
        "total_jobs": 138,
        "total_eligible": 45,
        "total_review": 12,
        "total_rejected": 81,
        "total_dismissed": 3,
        "eligibility_counts": {"ELIGIBLE": 45, "REVIEW": 12, "REJECTED": 81},
        "priority_counts": {"P1": 20, "P2": 35, "P3": 83},
        "remote_type_counts": {"REMOTE": 50, "HYBRID": 30, "ONSITE": 58},
        "provider_counts": {"greenhouse": 138},
        "top_locations": [{"name": "San Francisco, CA", "count": 25}],
    }

    with patch(
        "app.api.v1.jobs.JobRepository.get_filter_facets",
        new=AsyncMock(return_value=mock_facets),
    ):
        response = test_client.get("/api/v1/jobs/filters")

    assert response.status_code == 200
    data = response.json()
    assert data["total_jobs"] == 138
    assert data["total_eligible"] == 45
    assert data["provider_counts"]["greenhouse"] == 138


def test_get_job_detail_found(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs/{job_id} returns comprehensive job details when found."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=target_id)

    with patch(
        "app.api.v1.jobs.JobRepository.get_by_id_with_details",
        new=AsyncMock(return_value=mock_job),
    ):
        response = test_client.get(f"/api/v1/jobs/{target_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(target_id)
    assert data["title"] == "Staff Backend Engineer"
    assert data["source_verified"] is True
    assert data["jd_verified"] is True
    assert len(data["sources"]) == 1
    assert data["sources"][0]["source_name"] == "greenhouse"


def test_get_job_detail_not_found(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs/{job_id} returns HTTP 404 for unknown IDs."""
    unknown_id = uuid.uuid4()

    with patch(
        "app.api.v1.jobs.JobRepository.get_by_id_with_details",
        new=AsyncMock(return_value=None),
    ):
        response = test_client.get(f"/api/v1/jobs/{unknown_id}")

    assert response.status_code == 404
    assert f"Job with ID '{unknown_id}' not found." in response.json()["detail"]


def test_get_review_queue_endpoint(test_client: TestClient) -> None:
    """Verify GET /api/v1/review-queue retrieves pending review vacancies."""
    review_job = _create_mock_job(
        eligibility_status="REVIEW",
        review_reason="EXPERIENCE_AMBIGUOUS",
    )

    with patch(
        "app.api.v1.jobs.JobRepository.get_review_queue",
        new=AsyncMock(return_value=([review_job], 1)),
    ):
        response = test_client.get("/api/v1/review-queue")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["eligibility_status"] == "REVIEW"
    assert data["items"][0]["review_reason"] == "EXPERIENCE_AMBIGUOUS"


def test_review_job_endpoint_approve(test_client: TestClient) -> None:
    """Verify POST /api/v1/jobs/{job_id}/review APPROVE preserves deterministic eligibility and candidate application tracking."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(
        job_id=target_id, eligibility_status="REVIEW", application_status="NOT_APPLIED"
    )

    decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=target_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="APPROVED",
        role_priority="P1",
        reason_codes=["HUMAN_REVIEW_APPROVE"],
        evaluation_context={"action": "APPROVE", "notes": "Approved by recruiter"},
        evaluated_at=datetime.now(UTC),
    )

    async def fake_record_human_review(job, action, notes=None, reviewer="human_reviewer"):
        return decision

    with (
        patch(
            "app.api.v1.jobs.JobRepository.get_by_id",
            new=AsyncMock(return_value=mock_job),
        ),
        patch(
            "app.api.v1.jobs.JobRepository.record_human_review",
            side_effect=fake_record_human_review,
        ),
    ):
        response = test_client.post(
            f"/api/v1/jobs/{target_id}/review",
            json={"action": "APPROVE", "notes": "Approved by recruiter", "reviewer": "recruiter_1"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(target_id)
    assert data["human_review_status"] == "APPROVED"
    assert data["application_status"] == "NOT_APPLIED"  # Independent candidate tracking!
    assert data["eligibility_status"] == "REVIEW"  # Deterministic rule preserved!


def test_dismiss_job_endpoint(test_client: TestClient) -> None:
    """Verify POST /api/v1/jobs/{job_id}/dismiss records dismissal audit record without mutating candidate tracking."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=target_id, application_status="NOT_APPLIED")

    decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=target_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="DISMISSED",
        role_priority="P1",
        reason_codes=["HUMAN_DISMISS"],
        evaluation_context={"reason": "Not interested"},
        evaluated_at=datetime.now(UTC),
    )

    async def fake_record_dismissal(job, reason=None, reviewer="human_reviewer"):
        return decision

    with (
        patch(
            "app.api.v1.jobs.JobRepository.get_by_id",
            new=AsyncMock(return_value=mock_job),
        ),
        patch(
            "app.api.v1.jobs.JobRepository.record_dismissal",
            side_effect=fake_record_dismissal,
        ),
    ):
        response = test_client.post(
            f"/api/v1/jobs/{target_id}/dismiss",
            json={"reason": "Not interested", "reviewer": "user"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(target_id)
    assert data["human_review_status"] == "DISMISSED"
    assert data["application_status"] == "NOT_APPLIED"


def test_update_application_status_endpoint(test_client: TestClient) -> None:
    """Verify PATCH /api/v1/jobs/{job_id}/application-status updates candidate application status."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=target_id, application_status="NOT_APPLIED")

    async def fake_update_application_status(job, new_status):
        job.application_status = new_status
        return job

    with (
        patch(
            "app.api.v1.jobs.JobRepository.get_by_id",
            new=AsyncMock(return_value=mock_job),
        ),
        patch(
            "app.api.v1.jobs.JobRepository.update_application_status",
            side_effect=fake_update_application_status,
        ),
    ):
        response = test_client.patch(
            f"/api/v1/jobs/{target_id}/application-status",
            json={"application_status": "APPLIED"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["job_id"] == str(target_id)
    assert data["application_status"] == "APPLIED"


def test_url_sanitization_rejects_malicious_schemes() -> None:
    """Verify URL sanitizer strictly permits http/https and rejects javascript:, data:, and relative exploits."""
    from app.api.v1.jobs import sanitize_url

    assert (
        sanitize_url("https://boards.greenhouse.io/job/123")
        == "https://boards.greenhouse.io/job/123"
    )
    assert sanitize_url("http://example.com") == "http://example.com"
    assert sanitize_url("javascript:alert(1)") is None
    assert sanitize_url("data:text/html,<script>alert(1)</script>") is None
    assert sanitize_url("vbscript:msgbox(1)") is None
    assert sanitize_url("/relative/path") is None
    assert sanitize_url(None) is None


def test_job_detail_all_eight_verification_dimensions(test_client: TestClient) -> None:
    """Verify GET /api/v1/jobs/{job_id} exposes all 8 verification dimensions in detail schema."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=target_id)

    vr = VerificationRecord(
        job_id=target_id,
        source_verified=True,
        jd_verified=True,
        application_url_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        posting_date_verified=True,
        verification_confidence=Decimal("0.95"),
        passed=True,
        audit_trail={},
    )
    mock_job.verification_records = [vr]

    with patch(
        "app.api.v1.jobs.JobRepository.get_by_id_with_details",
        new=AsyncMock(return_value=mock_job),
    ):
        response = test_client.get(f"/api/v1/jobs/{target_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["source_verified"] is True
    assert data["jd_verified"] is True
    assert data["application_url_verified"] is True
    assert data["company_verified"] is True
    assert data["location_verified"] is True
    assert data["experience_verified"] is True
    assert data["salary_verified"] is True
    assert data["posting_date_verified"] is True
    assert data["verification_confidence"] == 0.95


def test_list_companies_endpoint(test_client: TestClient) -> None:
    """Verify GET /api/v1/companies returns companies with ATS sources and job counts."""
    mock_company = Company(
        id=uuid.uuid4(),
        name="Figma",
        normalized_name="figma",
        website="https://figma.com",
        industry="Design Software",
        sector="Technology",
        is_verified=True,
        confidence=Decimal("1.00"),
    )
    mock_company.company_sources = []

    with patch(
        "app.api.v1.companies.CompanyRepository.list_all_with_stats",
        new=AsyncMock(return_value=[(mock_company, 138)]),
    ):
        response = test_client.get("/api/v1/companies")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["name"] == "Figma"
    assert data[0]["active_jobs_count"] == 138


def test_web_dashboard_html_view(test_client: TestClient) -> None:
    """Verify GET /dashboard returns HTML 200 containing dashboard elements."""
    mock_facets = {
        "total_jobs": 138,
        "total_eligible": 45,
        "total_review": 12,
        "total_rejected": 81,
        "total_dismissed": 0,
        "eligibility_counts": {},
        "priority_counts": {},
        "remote_type_counts": {},
        "provider_counts": {"greenhouse": 138},
        "top_locations": [],
    }

    with (
        patch(
            "app.api.web.JobRepository.get_filter_facets",
            new=AsyncMock(return_value=mock_facets),
        ),
        patch(
            "app.api.web.JobRepository.search_and_filter_jobs",
            new=AsyncMock(return_value=([], 0)),
        ),
    ):
        response = test_client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Job Intelligence Dashboard" in response.text
    assert "Active Vacancies" in response.text


def test_web_jobs_feed_html_view(test_client: TestClient) -> None:
    """Verify GET /jobs returns HTML 200 containing feed interface."""
    mock_job = _create_mock_job()
    mock_facets = {
        "total_jobs": 1,
        "total_eligible": 1,
        "total_review": 0,
        "total_rejected": 0,
        "total_dismissed": 0,
        "eligibility_counts": {"ELIGIBLE": 1},
        "priority_counts": {"P1": 1},
        "remote_type_counts": {"REMOTE": 1},
        "provider_counts": {"greenhouse": 1},
        "top_locations": [],
    }

    with (
        patch(
            "app.api.web.JobRepository.search_and_filter_jobs",
            new=AsyncMock(return_value=([mock_job], 1)),
        ),
        patch(
            "app.api.web.JobRepository.get_filter_facets",
            new=AsyncMock(return_value=mock_facets),
        ),
    ):
        response = test_client.get("/jobs")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Job Feed" in response.text
    assert "Staff Backend Engineer" in response.text


def test_web_job_detail_html_view(test_client: TestClient) -> None:
    """Verify GET /jobs/{job_id} returns HTML 200 with 8-dimension verification grid."""
    target_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=target_id)
    mock_facets = {"total_review": 0}

    with (
        patch(
            "app.api.web.JobRepository.get_by_id_with_details",
            new=AsyncMock(return_value=mock_job),
        ),
        patch(
            "app.api.web.JobRepository.get_filter_facets",
            new=AsyncMock(return_value=mock_facets),
        ),
    ):
        response = test_client.get(f"/jobs/{target_id}")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "8-Dimension Deterministic Verification" in response.text
    assert "Staff Backend Engineer" in response.text


def test_web_review_queue_html_view(test_client: TestClient) -> None:
    """Verify GET /review-queue returns HTML 200."""
    review_job = _create_mock_job(
        eligibility_status="REVIEW",
        review_reason="EXPERIENCE_AMBIGUOUS",
    )

    with patch(
        "app.api.web.JobRepository.get_review_queue",
        new=AsyncMock(return_value=([review_job], 1)),
    ):
        response = test_client.get("/review-queue")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Human Review Queue" in response.text
    assert "EXPERIENCE_AMBIGUOUS" in response.text


def test_csrf_origin_validation_rejects_untrusted_origins(test_client: TestClient) -> None:
    """Verify that state-changing requests from untrusted origins are rejected with 403 Forbidden."""
    job_id = uuid.uuid4()
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"action": "APPROVE", "notes": "LGTM"},
        headers={"Origin": "https://evil-attacker.com"},
    )
    assert response.status_code == 403
    assert "CSRF origin validation failed" in response.json()["detail"]
    assert "evil-attacker.com" in response.json()["detail"]


def test_csrf_origin_validation_rejects_null_origin(test_client: TestClient) -> None:
    """Verify that sandboxed iframe 'Origin: null' requests are rejected with 403 Forbidden."""
    job_id = uuid.uuid4()
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/dismiss",
        json={"reason": "Dismiss"},
        headers={"Origin": "null"},
    )
    assert response.status_code == 403
    assert "CSRF origin validation failed" in response.json()["detail"]


def test_csrf_referer_validation_rejects_untrusted_referer(test_client: TestClient) -> None:
    """Verify that state-changing requests with untrusted Referer header are rejected with 403 Forbidden."""
    job_id = uuid.uuid4()
    response = test_client.patch(
        f"/api/v1/jobs/{job_id}/application-status",
        json={"application_status": "APPLIED"},
        headers={"Referer": "https://malicious-site.org/exploit.html"},
    )
    assert response.status_code == 403
    assert "CSRF referer validation failed" in response.json()["detail"]


def test_csrf_origin_validation_permits_trusted_dashboard_origin(test_client: TestClient) -> None:
    """Verify that state-changing requests from the configured dashboard origin succeed."""
    job_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=job_id)
    mock_decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=job_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="APPROVED",
        role_priority="P1",
        reason_codes=["HUMAN_REVIEW_APPROVE"],
        evaluation_context={"reviewer": "test"},
        evaluated_at=datetime.now(UTC),
    )

    with (
        patch("app.api.v1.jobs.JobRepository.get_by_id", new=AsyncMock(return_value=mock_job)),
        patch(
            "app.api.v1.jobs.JobRepository.record_human_review",
            new=AsyncMock(return_value=mock_decision),
        ),
    ):
        response = test_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
    assert response.status_code == 200
    assert response.json()["human_review_status"] == "APPROVED"


def test_csrf_origin_validation_permits_direct_api_clients_without_browser_headers(
    test_client: TestClient,
) -> None:
    """Verify programmatic API clients (curl, CLI) without Origin/Referer headers can execute actions."""
    job_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=job_id)
    mock_decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=job_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="DISMISSED",
        role_priority="P1",
        reason_codes=["HUMAN_DISMISS"],
        evaluation_context={"reason": "Test dismiss"},
        evaluated_at=datetime.now(UTC),
    )

    with (
        patch("app.api.v1.jobs.JobRepository.get_by_id", new=AsyncMock(return_value=mock_job)),
        patch(
            "app.api.v1.jobs.JobRepository.record_dismissal",
            new=AsyncMock(return_value=mock_decision),
        ),
    ):
        response = test_client.post(
            f"/api/v1/jobs/{job_id}/dismiss",
            json={"reason": "Dismissing via CLI"},
        )
    assert response.status_code == 200
    assert response.json()["human_review_status"] == "DISMISSED"


def test_csrf_rejects_cookie_authenticated_requests_without_origin_or_referer(
    test_client: TestClient,
) -> None:
    """Verify that requests carrying ambient session cookies without Origin/Referer are rejected."""
    job_id = uuid.uuid4()
    # Simulates an attacker initiating a form submission with Referrer-Policy: no-referrer
    test_client.cookies.set("session_token", "fake_session_12345")
    try:
        response = test_client.post(
            f"/api/v1/jobs/{job_id}/dismiss",
            json={"reason": "Exploit attempt without referer"},
        )
        assert response.status_code == 403
        assert "CSRF validation failed" in response.json()["detail"]
        assert "must include a valid Origin or Referer header" in response.json()["detail"]
    finally:
        test_client.cookies.clear()


def test_csrf_permits_cookie_authenticated_requests_with_valid_origin(
    test_client: TestClient,
) -> None:
    """Verify that cookie-authenticated requests with a valid trusted Origin header succeed."""
    job_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=job_id)
    mock_decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=job_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="DISMISSED",
        role_priority="P1",
        reason_codes=["HUMAN_DISMISS"],
        evaluation_context={"reason": "Valid dismissal with cookie"},
        evaluated_at=datetime.now(UTC),
    )

    test_client.cookies.set("session_token", "valid_session_12345")
    try:
        with (
            patch("app.api.v1.jobs.JobRepository.get_by_id", new=AsyncMock(return_value=mock_job)),
            patch(
                "app.api.v1.jobs.JobRepository.record_dismissal",
                new=AsyncMock(return_value=mock_decision),
            ),
        ):
            response = test_client.post(
                f"/api/v1/jobs/{job_id}/dismiss",
                json={"reason": "Valid dismissal"},
                headers={"Origin": "http://127.0.0.1:8000"},
            )
        assert response.status_code == 200
        assert response.json()["human_review_status"] == "DISMISSED"
    finally:
        test_client.cookies.clear()


def test_csrf_rejects_host_header_manipulation_attack(test_client: TestClient) -> None:
    """Verify that an attacker cannot bypass CSRF validation by manipulating the Host header."""
    job_id = uuid.uuid4()
    # Attacker attempts to spoof Host to match their untrusted Origin
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/review",
        json={"action": "APPROVE"},
        headers={
            "Host": "evil-attacker.com",
            "Origin": "https://evil-attacker.com",
        },
    )
    # Rejection by TrustedHostMiddleware (400) or CSRFOriginValidationMiddleware (403)
    assert response.status_code in (400, 403)


def test_csrf_rejects_dns_rebinding_attack(test_client: TestClient) -> None:
    """Verify that a DNS-rebinding attack (rebind.evil.com resolving to localhost) is rejected."""
    job_id = uuid.uuid4()
    response = test_client.post(
        f"/api/v1/jobs/{job_id}/dismiss",
        json={"reason": "DNS rebinding payload"},
        headers={
            "Host": "rebind.evil.com:8000",
            "Origin": "http://rebind.evil.com:8000",
        },
    )
    assert response.status_code in (400, 403)


def test_production_configuration_rejects_localhost_and_test_origins() -> None:
    """Verify production configuration restricts trusted origins strictly to DASHBOARD_BASE_URL.

    In production/staging:
    - localhost, 127.0.0.1, test, and testserver origins must be rejected.
    - API_HOST is never automatically trusted as a browser origin.
    - Only the configured DASHBOARD_BASE_URL and explicit additional origins succeed.
    """
    from app.api.main import create_app
    from app.api.middleware.csrf import CSRFOriginValidationMiddleware
    from app.config.settings import Settings

    prod_settings = Settings(
        APP_ENV="production",
        DASHBOARD_BASE_URL="https://jobs.prod-enterprise.com",
        ADDITIONAL_TRUSTED_ORIGINS=["https://admin.prod-enterprise.com"],
        API_HOST="0.0.0.0",
        API_PORT=8000,
    )

    # 1. Static origin resolution check
    trusted = CSRFOriginValidationMiddleware.get_trusted_origins(prod_settings)
    assert trusted == {
        "https://jobs.prod-enterprise.com",
        "https://admin.prod-enterprise.com",
    }
    assert "http://localhost:8000" not in trusted
    assert "http://127.0.0.1:8000" not in trusted
    assert "http://test" not in trusted
    assert "http://testserver" not in trusted
    assert "http://0.0.0.0:8000" not in trusted

    # 2. End-to-end middleware execution in production app
    with patch("app.api.main.get_settings", return_value=prod_settings):
        prod_app = create_app()

    prod_client = TestClient(prod_app, base_url="https://jobs.prod-enterprise.com")
    job_id = uuid.uuid4()
    mock_job = _create_mock_job(job_id=job_id)
    mock_decision = FilterDecision(
        id=uuid.uuid4(),
        job_id=job_id,
        rule_version="HUMAN_REVIEW_V1",
        final_status="APPROVED",
        role_priority="P1",
        reason_codes=["HUMAN_REVIEW_APPROVE"],
        evaluation_context={"reviewer": "prod_admin"},
        evaluated_at=datetime.now(UTC),
    )

    with (
        patch("app.api.v1.jobs.JobRepository.get_by_id", new=AsyncMock(return_value=mock_job)),
        patch(
            "app.api.v1.jobs.JobRepository.record_human_review",
            new=AsyncMock(return_value=mock_decision),
        ),
    ):
        # A. Localhost origin in production -> 403 Forbidden
        res_local = prod_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "http://localhost:8000"},
        )
        assert res_local.status_code == 403
        assert "CSRF origin validation failed" in res_local.json()["detail"]

        # B. 127.0.0.1 origin in production -> 403 Forbidden
        res_ip = prod_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert res_ip.status_code == 403

        # C. Test runner origins in production -> 403 Forbidden
        res_test = prod_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "http://test"},
        )
        assert res_test.status_code == 403

        # D. Configured production DASHBOARD_BASE_URL -> 200 OK
        res_prod = prod_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "https://jobs.prod-enterprise.com"},
        )
        assert res_prod.status_code == 200
        assert res_prod.json()["human_review_status"] == "APPROVED"

        # E. Explicit additional trusted origin -> 200 OK
        res_admin = prod_client.post(
            f"/api/v1/jobs/{job_id}/review",
            json={"action": "APPROVE"},
            headers={"Origin": "https://admin.prod-enterprise.com"},
        )
        assert res_admin.status_code == 200


def test_production_trusted_host_middleware_allows_only_explicit_hosts() -> None:
    """Verify TrustedHostMiddleware in production contains strictly explicit hosts without wildcards."""
    from app.api.main import create_app
    from app.config.settings import Settings

    prod_settings = Settings(
        APP_ENV="production",
        DASHBOARD_BASE_URL="https://jobs.prod-enterprise.com",
        ADDITIONAL_ALLOWED_HOSTS=["admin.prod-enterprise.com"],
    )

    with patch("app.api.main.get_settings", return_value=prod_settings):
        prod_app = create_app()

    prod_client = TestClient(prod_app, base_url="https://jobs.prod-enterprise.com")

    # 1. Untrusted host in production -> 400 Bad Request
    res_evil = prod_client.get(
        "/health",
        headers={"Host": "evil.com"},
    )
    assert res_evil.status_code == 400

    # 2. Localhost host header in production -> 400 Bad Request
    res_local = prod_client.get(
        "/health",
        headers={"Host": "localhost"},
    )
    assert res_local.status_code == 400

    # 3. Explicit production host -> 200 OK
    res_prod = prod_client.get(
        "/health",
        headers={"Host": "jobs.prod-enterprise.com"},
    )
    assert res_prod.status_code == 200


def test_debug_false_suppresses_dev_origins_even_if_app_env_is_development() -> None:
    """Verify that setting DEBUG=False prevents dev/test origins from being trusted."""
    from app.api.middleware.csrf import CSRFOriginValidationMiddleware
    from app.config.settings import Settings

    settings = Settings(
        APP_ENV="development",
        DEBUG=False,
        DASHBOARD_BASE_URL="https://prod-domain.com",
    )
    trusted = CSRFOriginValidationMiddleware.get_trusted_origins(settings)
    assert "https://prod-domain.com" in trusted
    assert "http://localhost:8000" not in trusted
    assert "http://127.0.0.1:8000" not in trusted
    assert "http://testserver" not in trusted
