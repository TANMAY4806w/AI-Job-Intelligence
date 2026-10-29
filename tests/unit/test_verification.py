"""Unit tests for the pure deterministic verification engine.

Boundary: 100% pure evaluation tests.
Zero network calls, zero database connections.
Tests all eight verification dimensions, confidence scores, reason codes,
and publication-date verification gating per rules/VERIFICATION_V1.md.
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.verification.evaluator import JobVerificationEvaluator
from app.verification.schemas import (
    FAILURE_JD_EXPIRED,
    FAILURE_JD_NOT_FOUND,
    FAILURE_SOURCE_INVALID,
    REVIEW_APPLICATION_LINK_BROKEN,
    REVIEW_COMPANY_UNVERIFIED,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_LOCATION_AMBIGUOUS,
    REVIEW_LOCATION_REMOTE_REVIEW,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
    REVIEW_SOURCE_CONFLICT_REVIEW,
    JobVerificationInput,
)

SAMPLE_JD_TEXT = (
    "We are seeking an enthusiastic AI Software Engineer to join our growing team in Bengaluru. "
    "You will design, develop, and deploy scalable LLM applications, RAG pipelines, and agentic workflows. "
    "Key requirements include strong Python skills, familiarity with FastAPI, PostgreSQL, and modern AI frameworks. "
    "This is a full-time role offering great learning opportunities, mentorship, and impactful work."
)


@pytest.fixture
def evaluator() -> JobVerificationEvaluator:
    return JobVerificationEvaluator()


@pytest.fixture
def base_valid_input() -> JobVerificationInput:
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    return JobVerificationInput(
        job_id=uuid.uuid4(),
        # Dimension 1: Source
        source_name="greenhouse",
        source_type="ATS",
        is_source_approved=True,
        payload_format_valid=True,
        # Dimension 2: JD
        description=SAMPLE_JD_TEXT,
        http_status_code=200,
        is_page_active=True,
        is_tombstone=False,
        is_expired=False,
        # Dimension 3: Application URL
        application_url="https://boards.greenhouse.io/company/jobs/12345",
        application_url_status=200,
        application_url_reachable=True,
        source_url="https://company.com/careers/ai-engineer",
        source_url_status=200,
        source_url_reachable=True,
        # Dimension 4: Company
        company_name="Acme AI",
        is_company_registry_verified=True,
        company_domain="acme.ai",
        has_verifiable_domain=True,
        # Dimension 5: Location
        location="Bengaluru, Karnataka, India",
        normalized_location="Bengaluru",
        remote_type="HYBRID",
        company_location="Bengaluru",
        # Dimension 6: Experience
        experience_raw="0-2 years",
        experience_min_years=Decimal("0.0"),
        experience_max_years=Decimal("2.0"),
        is_senior_lead_title=False,
        # Dimension 7: Salary
        salary_min=Decimal("800000.00"),
        salary_max=Decimal("1200000.00"),
        salary_currency="INR",
        salary_period="ANNUAL",
        # Dimension 8: Posting Date
        posted_at=now - timedelta(days=2),
        collected_at=now - timedelta(hours=1),
        publication_date_corroborated=True,
    )


def test_pure_verification_imports_without_database_modules():
    """Verify in an isolated Python process that importing app.verification does not load sqlalchemy or app.persistence."""
    import subprocess

    code = (
        "import sys; "
        "import app.verification; "
        "assert 'sqlalchemy' not in sys.modules, 'sqlalchemy found in sys.modules'; "
        "assert 'app.persistence' not in sys.modules, 'app.persistence found in sys.modules'; "
        "assert hasattr(app.verification, 'JobVerificationEvaluator'); "
        "assert hasattr(app.verification, 'JobVerificationInput'); "
        "assert hasattr(app.verification, 'JobVerificationResult'); "
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, f"Process failed with stderr: {proc.stderr}"


def test_fully_verified_baseline_input(
    evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
):
    """Test that complete, corroborated input verifies all 8 dimensions with 1.00 confidence."""
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
    result = evaluator.evaluate(base_valid_input, reference_time=now)

    assert result.source_verified is True
    assert result.jd_verified is True
    assert result.application_url_verified is True
    assert result.company_verified is True
    assert result.location_verified is True
    assert result.experience_verified is True
    assert result.salary_verified is True
    assert result.posting_date_verified is True

    assert result.passed is True
    assert result.failure_reasons is None
    assert result.review_reasons is None
    assert result.verification_confidence == Decimal("1.00")
    assert result.confidence_tier == "HIGH"


# ---------------------------------------------------------------------------
# Dimension 1: Source Validity Tests
# ---------------------------------------------------------------------------
class TestSourceValidityDimension:
    def test_approved_registry_sources(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        for name in ["greenhouse", "lever", "ashby", "workable"]:
            inp = base_valid_input.model_copy(
                update={"source_name": name, "is_source_approved": None}
            )
            result = evaluator.evaluate(inp)
            assert result.source_verified is True

    def test_explicitly_disapproved_source(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"is_source_approved": False})
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert result.passed is False
        assert result.failure_reasons == [FAILURE_SOURCE_INVALID]

    def test_unknown_unregistered_source(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "source_name": "sketchy_scraper_xyz",
                "source_type": "UNKNOWN",
                "is_source_approved": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert result.passed is False
        assert FAILURE_SOURCE_INVALID in result.failure_reasons

    def test_source_id_alone_does_not_approve_unrecognized_source(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "source_id": uuid.uuid4(),
                "source_name": "sketchy_scraper_xyz",
                "source_type": "UNKNOWN",
                "is_source_approved": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert FAILURE_SOURCE_INVALID in result.failure_reasons

    def test_generic_ats_type_does_not_approve_unrecognized_platform(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        """Verify that generic source_type 'ATS' or 'ATS_API' does not approve an unrecognized platform."""
        inp = base_valid_input.model_copy(
            update={
                "source_name": "arbitrary_custom_ats",
                "source_type": "ATS_API",
                "is_source_approved": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert result.passed is False
        assert FAILURE_SOURCE_INVALID in result.failure_reasons
        assert (
            result.dimensions["source_validity"].details["failure_cause"]
            == "unrecognized_source_platform"
        )

    def test_missing_source_evidence_fails_verification(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        """Verify that completely missing source identity fails source verification."""
        inp = base_valid_input.model_copy(
            update={
                "source_id": None,
                "source_name": None,
                "source_type": None,
                "is_source_approved": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert result.passed is False
        assert FAILURE_SOURCE_INVALID in result.failure_reasons

    def test_corrupted_payload_format(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"payload_format_valid": False})
        result = evaluator.evaluate(inp)
        assert result.source_verified is False
        assert FAILURE_SOURCE_INVALID in result.failure_reasons


# ---------------------------------------------------------------------------
# Dimension 2: Actual JD Existence Tests
# ---------------------------------------------------------------------------
class TestJDExistenceDimension:
    def test_short_description_fails_minimum_length(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"description": "Hiring Python dev."}
        )  # < 100 chars
        result = evaluator.evaluate(inp)
        assert result.jd_verified is False
        assert result.passed is False
        assert result.failure_reasons == [FAILURE_JD_NOT_FOUND]

    def test_missing_description_fails(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"description": None})
        result = evaluator.evaluate(inp)
        assert result.jd_verified is False
        assert FAILURE_JD_NOT_FOUND in result.failure_reasons

    def test_http_404_fails(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"http_status_code": 404})
        result = evaluator.evaluate(inp)
        assert result.jd_verified is False
        assert result.failure_reasons == [FAILURE_JD_NOT_FOUND]

    def test_http_410_fails_with_expired(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"http_status_code": 410})
        result = evaluator.evaluate(inp)
        assert result.jd_verified is False
        assert result.failure_reasons == [FAILURE_JD_EXPIRED]

    def test_tombstone_or_expired_flag_fails(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"is_tombstone": True})
        result = evaluator.evaluate(inp)
        assert result.jd_verified is False
        assert result.failure_reasons == [FAILURE_JD_EXPIRED]

        inp_exp = base_valid_input.model_copy(update={"is_expired": True})
        result_exp = evaluator.evaluate(inp_exp)
        assert result_exp.jd_verified is False
        assert result_exp.failure_reasons == [FAILURE_JD_EXPIRED]


# ---------------------------------------------------------------------------
# Dimension 3: Application URL Validity Tests
# ---------------------------------------------------------------------------
class TestApplicationURLValidityDimension:
    def test_reachable_https_url_passes(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "https://boards.greenhouse.io/jobs/1",
                "application_url_status": 200,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is True

    def test_insecure_http_url_fails(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "http://boards.greenhouse.io/jobs/1",  # HTTP instead of HTTPS
                "source_url": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert result.review_reasons == [REVIEW_APPLICATION_LINK_BROKEN]

    def test_broken_app_url_with_accessible_fallback(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "https://broken.ats.com/job/404",
                "application_url_status": 404,
                "application_url_reachable": False,
                "source_url": "https://company.com/careers/opening",
                "source_url_status": 200,
                "source_url_reachable": True,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        # Fallback to source_url succeeds -> no APPLICATION_LINK_BROKEN review reason
        assert (
            result.review_reasons is None
            or REVIEW_APPLICATION_LINK_BROKEN not in result.review_reasons
        )

    def test_both_primary_and_fallback_broken_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "https://broken.ats.com/job/404",
                "application_url_status": 404,
                "application_url_reachable": False,
                "source_url": "https://broken.source.com/job/500",
                "source_url_status": 500,
                "source_url_reachable": False,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert REVIEW_APPLICATION_LINK_BROKEN in result.review_reasons
        # Does not fail critical passed check
        assert result.passed is True

    def test_unprobed_valid_https_url_remains_unverified_without_broken_flag(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "https://boards.greenhouse.io/jobs/1",
                "application_url_status": None,
                "application_url_reachable": None,
                "source_url": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert (
            result.review_reasons is None
            or REVIEW_APPLICATION_LINK_BROKEN not in result.review_reasons
        )
        assert (
            result.dimensions["application_url_validity"].details["primary_url_status"]
            == "unprobed_syntax_valid"
        )

    def test_api_http_status_code_200_does_not_verify_unprobed_application_url(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        """Regression test: verify that http_status_code=200 from Greenhouse job detail API
        only verifies JD existence and does NOT make an unprobed application URL appear reachable.
        """
        inp = base_valid_input.model_copy(
            update={
                # Individual Greenhouse job detail API fetch was 200 OK
                "http_status_code": 200,
                "is_page_active": True,
                # Application URL is unprobed (reachability evidence is absent)
                "application_url": "https://boards.greenhouse.io/figma/jobs/401",
                "application_url_status": None,
                "application_url_reachable": None,
                "source_url": None,
            }
        )
        result = evaluator.evaluate(inp)

        # 1. JD existence is verified by the 200 OK API response
        assert result.jd_verified is True
        assert result.dimensions["jd_existence"].score == Decimal("1.00")

        # 2. Application URL MUST remain unverified (score 0.00, verified=False)
        assert result.application_url_verified is False
        assert result.dimensions["application_url_validity"].verified is False
        assert result.dimensions["application_url_validity"].score == Decimal("0.00")
        assert (
            result.dimensions["application_url_validity"].details["primary_url_status"]
            == "unprobed_syntax_valid"
        )
        assert (
            result.review_reasons is None
            or REVIEW_APPLICATION_LINK_BROKEN not in result.review_reasons
        )

        # 3. Conversely, when probed evidence is explicitly supplied, application URL is verified
        inp_probed = inp.model_copy(
            update={
                "application_url_status": 200,
                "application_url_reachable": True,
            }
        )
        result_probed = evaluator.evaluate(inp_probed)
        assert result_probed.application_url_verified is True
        assert result_probed.dimensions["application_url_validity"].verified is True
        assert result_probed.dimensions["application_url_validity"].score == Decimal("1.00")
        assert (
            result_probed.dimensions["application_url_validity"].details["primary_url_status"]
            == "accessible_and_valid"
        )

    def test_malformed_url_routes_to_broken_when_no_fallback(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "not-a-valid-url",
                "source_url": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert REVIEW_APPLICATION_LINK_BROKEN in result.review_reasons

    def test_missing_urls_route_to_broken(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": None,
                "source_url": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert REVIEW_APPLICATION_LINK_BROKEN in result.review_reasons

    def test_malformed_primary_with_unprobed_valid_fallback(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "application_url": "not-a-valid-url",
                "source_url": "https://company.com/careers/jobs/1",
                "source_url_status": None,
                "source_url_reachable": None,
            }
        )
        result = evaluator.evaluate(inp)
        assert result.application_url_verified is False
        assert (
            result.review_reasons is None
            or REVIEW_APPLICATION_LINK_BROKEN not in result.review_reasons
        )
        assert (
            result.dimensions["application_url_validity"].details["fallback_source_url_status"]
            == "fallback_unprobed_syntax_valid"
        )


# ---------------------------------------------------------------------------
# Dimension 4: Company Legitimacy Tests
# ---------------------------------------------------------------------------
class TestCompanyLegitimacyDimension:
    def test_company_registry_verified(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"is_company_registry_verified": True})
        result = evaluator.evaluate(inp)
        assert result.company_verified is True

    def test_company_verifiable_domain(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"is_company_registry_verified": False, "has_verifiable_domain": True}
        )
        result = evaluator.evaluate(inp)
        assert result.company_verified is True

    def test_unverified_company_routes_to_review_without_rejection(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"is_company_registry_verified": False, "has_verifiable_domain": False}
        )
        result = evaluator.evaluate(inp)
        assert result.company_verified is False
        assert result.passed is True
        assert REVIEW_COMPANY_UNVERIFIED in result.review_reasons


# ---------------------------------------------------------------------------
# Dimension 5: Location Consistency Tests
# ---------------------------------------------------------------------------
class TestLocationConsistencyDimension:
    def test_p1_and_p2_approved_cities(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        expected_tiers = {
            "Mumbai": "P1",
            "Navi Mumbai": "P1",
            "Thane": "P1",
            "Pune": "P1",
            "Bengaluru": "P2",
            "Hyderabad": "P2",
        }
        for city, expected_tier in expected_tiers.items():
            inp = base_valid_input.model_copy(
                update={"normalized_location": city, "remote_type": "ON_SITE"}
            )
            result = evaluator.evaluate(inp)
            assert result.location_verified is True
            assert result.review_reasons is None
            dim = result.dimensions["location_consistency"]
            assert dim.details["matched_hub"] == city
            assert dim.details["location_tier"] == expected_tier

    def test_raw_location_normalization_to_canonical_hubs(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        raw_test_cases = [
            ("Navi Mumbai, Maharashtra", "Navi Mumbai", "P1"),
            ("New Bombay", "Navi Mumbai", "P1"),
            ("Thane West", "Thane", "P1"),
            ("Thane East, MH", "Thane", "P1"),
            ("Hinjewadi, Pune", "Pune", "P1"),
            ("Magarpatta City, Pune", "Pune", "P1"),
            ("Powai, Mumbai", "Mumbai", "P1"),
            ("Bombay Suburban", "Mumbai", "P1"),
            ("Whitefield, Bangalore", "Bengaluru", "P2"),
            ("Electronic City, Bengaluru", "Bengaluru", "P2"),
            ("Secunderabad, Telangana", "Hyderabad", "P2"),
            ("Hitec City, Hyderabad", "Hyderabad", "P2"),
            ("Gachibowli, Hyderabad", "Hyderabad", "P2"),
        ]
        for raw_loc, expected_hub, expected_tier in raw_test_cases:
            inp = base_valid_input.model_copy(
                update={"normalized_location": None, "location": raw_loc, "remote_type": "ON_SITE"}
            )
            result = evaluator.evaluate(inp)
            assert result.location_verified is True, f"Failed for {raw_loc}"
            assert result.review_reasons is None
            dim = result.dimensions["location_consistency"]
            assert dim.details["matched_hub"] == expected_hub
            assert dim.details["location_tier"] == expected_tier

    def test_remote_with_company_outside_hubs_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "remote_type": "REMOTE",
                "normalized_location": "Remote",
                "company_location": "London, UK",
            }
        )
        result = evaluator.evaluate(inp)
        # Location verified is true (or pending per Section 4.4)
        assert result.location_verified is True
        assert REVIEW_LOCATION_REMOTE_REVIEW in result.review_reasons

    def test_remote_with_company_inside_hub_passes(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "remote_type": "REMOTE",
                "company_location": "Bengaluru, India",
            }
        )
        result = evaluator.evaluate(inp)
        assert result.location_verified is True
        assert result.review_reasons is None

    def test_ambiguous_or_unapproved_location_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"normalized_location": None, "location": "India", "remote_type": "ON_SITE"}
        )
        result = evaluator.evaluate(inp)
        assert result.location_verified is False
        assert REVIEW_LOCATION_AMBIGUOUS in result.review_reasons


# ---------------------------------------------------------------------------
# Dimension 6: Experience Consistency Tests
# ---------------------------------------------------------------------------
class TestExperienceConsistencyDimension:
    def test_target_bands_verified(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        for min_yrs, max_yrs in [
            (Decimal("0.0"), Decimal("1.0")),
            (Decimal("0.0"), Decimal("2.0")),
            (Decimal("1.0"), Decimal("2.0")),
        ]:
            inp = base_valid_input.model_copy(
                update={"experience_min_years": min_yrs, "experience_max_years": max_yrs}
            )
            result = evaluator.evaluate(inp)
            assert result.experience_verified is True

    def test_one_to_three_years_protocol_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"experience_min_years": Decimal("1.0"), "experience_max_years": Decimal("3.0")}
        )
        result = evaluator.evaluate(inp)
        assert result.experience_verified is False
        assert REVIEW_EXPERIENCE_AMBIGUOUS in result.review_reasons

    def test_ambiguous_unstated_experience_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "experience_min_years": None,
                "experience_max_years": None,
                "experience_raw": "Relevant experience required",
            }
        )
        result = evaluator.evaluate(inp)
        assert result.experience_verified is False
        assert REVIEW_EXPERIENCE_AMBIGUOUS in result.review_reasons

    def test_experience_too_high_fails_verification(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"experience_min_years": Decimal("3.0"), "experience_max_years": Decimal("5.0")}
        )
        result = evaluator.evaluate(inp)
        assert result.experience_verified is False

    def test_senior_role_fails_verification(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"is_senior_lead_title": True})
        result = evaluator.evaluate(inp)
        assert result.experience_verified is False


# ---------------------------------------------------------------------------
# Dimension 7: Salary Statedness Tests
# ---------------------------------------------------------------------------
class TestSalaryStatednessDimension:
    def test_salary_present_verified(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={"salary_min": Decimal("500000.00"), "salary_max": None}
        )
        result = evaluator.evaluate(inp)
        assert result.salary_verified is True

    def test_salary_missing_routes_to_review_without_rejection(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(update={"salary_min": None, "salary_max": None})
        result = evaluator.evaluate(inp)
        assert result.salary_verified is False
        assert result.passed is True
        assert REVIEW_SALARY_UNKNOWN in result.review_reasons


# ---------------------------------------------------------------------------
# Dimension 8: Posting Date Plausibility & Gating Tests
# ---------------------------------------------------------------------------
class TestPostingDatePlausibilityDimension:
    def test_plausible_and_corroborated_is_verified(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": now - timedelta(days=2),
                "collected_at": now - timedelta(hours=2),
                "publication_date_corroborated": True,
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is True

    def test_uncorroborated_date_fails_verification_and_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": now - timedelta(days=2),
                "publication_date_corroborated": None,  # Unresolved / uncorroborated
                "posting_date_verified": None,
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons

    def test_explicitly_refuted_date_fails_verification(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": now - timedelta(days=2),
                "publication_date_corroborated": False,
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons

    def test_missing_posted_at_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(update={"posted_at": None, "collected_at": now})
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons

    def test_future_posted_at_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": now + timedelta(days=1),
                "publication_date_corroborated": True,
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons

    def test_implausible_old_date_exceeding_90_days_routes_to_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": now - timedelta(days=95),
                "collected_at": now,
                "publication_date_corroborated": True,
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons

    def test_collected_at_never_substitutes_for_missing_posted_at(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        inp = base_valid_input.model_copy(
            update={
                "posted_at": None,
                "collected_at": now - timedelta(hours=1),
            }
        )
        result = evaluator.evaluate(inp, reference_time=now)
        assert result.posting_date_verified is False
        assert REVIEW_POSTED_DATE_SUSPICIOUS in result.review_reasons


# ---------------------------------------------------------------------------
# Cross-Source Conflicts & Confidence Metric Tests
# ---------------------------------------------------------------------------
class TestConfidenceMetricAndConflicts:
    def test_cross_source_conflicts_triggers_review(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        inp = base_valid_input.model_copy(
            update={
                "has_source_conflicts": True,
                "source_conflicts": ["salary", "experience"],
            }
        )
        result = evaluator.evaluate(inp)
        assert REVIEW_SOURCE_CONFLICT_REVIEW in result.review_reasons

    def test_exact_confidence_weights_and_tiers(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)

        # 1. All 6 verified -> 1.00 (HIGH)
        res1 = evaluator.evaluate(base_valid_input, reference_time=now)
        assert res1.verification_confidence == Decimal("1.00")
        assert res1.confidence_tier == "HIGH"

        # 2. Salary missing (-0.05) -> 0.95 (HIGH)
        inp2 = base_valid_input.model_copy(update={"salary_min": None, "salary_max": None})
        res2 = evaluator.evaluate(inp2, reference_time=now)
        assert res2.verification_confidence == Decimal("0.95")
        assert res2.confidence_tier == "HIGH"

        # 3. Salary missing (-0.05) + Experience ambiguous (-0.15) -> 0.80 (MODERATE)
        inp3 = base_valid_input.model_copy(
            update={
                "salary_min": None,
                "salary_max": None,
                "experience_min_years": Decimal("1.0"),
                "experience_max_years": Decimal("3.0"),
            }
        )
        res3 = evaluator.evaluate(inp3, reference_time=now)
        assert res3.verification_confidence == Decimal("0.80")
        assert res3.confidence_tier == "MODERATE"

        # 4. Salary (-0.05) + Experience (-0.15) + Company unverified (-0.20) -> 0.60 (MODERATE)
        inp4 = base_valid_input.model_copy(
            update={
                "salary_min": None,
                "salary_max": None,
                "experience_min_years": Decimal("1.0"),
                "experience_max_years": Decimal("3.0"),
                "is_company_registry_verified": False,
                "has_verifiable_domain": False,
            }
        )
        res4 = evaluator.evaluate(inp4, reference_time=now)
        assert res4.verification_confidence == Decimal("0.60")
        assert res4.confidence_tier == "MODERATE"

        # 5. Only JD (0.30) and Source (0.20) verified -> 0.50 (LOW)
        inp5 = base_valid_input.model_copy(
            update={
                "salary_min": None,
                "salary_max": None,
                "experience_min_years": Decimal("1.0"),
                "experience_max_years": Decimal("3.0"),
                "is_company_registry_verified": False,
                "has_verifiable_domain": False,
                "normalized_location": None,
                "location": "Nowhere",
                "remote_type": "ON_SITE",
            }
        )
        res5 = evaluator.evaluate(inp5, reference_time=now)
        assert res5.verification_confidence == Decimal("0.50")
        assert res5.confidence_tier == "LOW"

    def test_deterministic_repeatability_and_immutability(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        res1 = evaluator.evaluate(base_valid_input, reference_time=now)
        res2 = evaluator.evaluate(base_valid_input, reference_time=now)

        assert res1 == res2
        assert res1.audit_trail == res2.audit_trail

    def test_to_verification_record_kwargs_and_job_updates(
        self, evaluator: JobVerificationEvaluator, base_valid_input: JobVerificationInput
    ):
        job_id = uuid.uuid4()
        now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
        result = evaluator.evaluate(base_valid_input, reference_time=now)

        kwargs = result.to_verification_record_kwargs(job_id, verification_run_at=now)
        assert kwargs["job_id"] == job_id
        assert kwargs["verification_run_at"] == now
        assert kwargs["source_verified"] is True
        assert kwargs["jd_verified"] is True
        assert kwargs["passed"] is True
        assert kwargs["verification_confidence"] == Decimal("1.00")
        assert isinstance(kwargs["audit_trail"], dict)

        job_updates = result.to_job_verification_updates()
        assert job_updates["source_verified"] is True
        assert job_updates["jd_verified"] is True
        assert job_updates["verification_confidence"] == Decimal("1.00")
