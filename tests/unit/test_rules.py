"""Unit tests for the deterministic job filtering and eligibility engine.

Boundary: 100% offline in-memory execution; zero PostgreSQL, database, or network dependency.
Validates all requirements from JOB_FILTERS_V1.md, VERIFICATION_V1.md, and V1_SPECIFICATION.md.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.normalization.schemas import NormalizedJobData
from app.rules import (
    DEFAULT_RULE_VERSION,
    JobFilterInput,
    evaluate_job_filter,
)
from app.rules.schemas import (
    REJECTION_EXCLUDED_ROLE,
    REJECTION_EXPERIENCE_TOO_HIGH,
    REJECTION_JOB_TOO_OLD,
    REJECTION_LOCATION_NOT_ALLOWED,
    REJECTION_SALARY_BELOW_MINIMUM,
    REVIEW_EXCLUSION_AMBIGUOUS,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_LOCATION_AMBIGUOUS,
    REVIEW_LOCATION_REMOTE_REVIEW,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
)

# Fixed evaluation anchor timestamp for deterministic freshness tests
T_REF = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)


# Helper to build valid default eligible job input
def _make_input(
    title: str = "AI Engineer",
    normalized_title: str | None = None,
    description: str = "Build scalable LLM and agentic workflows in Python.",
    location: str | None = "Bengaluru, Karnataka, India",
    normalized_location: str | None = "Bengaluru",
    remote_type: str = "ON_SITE",
    experience_raw: str | None = "0-2 years",
    experience_min_years: Decimal | None = Decimal("0.0"),
    experience_max_years: Decimal | None = Decimal("2.0"),
    salary_min: Decimal | None = Decimal("600000.00"),
    salary_max: Decimal | None = Decimal("800000.00"),
    salary_currency: str | None = "INR",
    salary_period: str | None = "ANNUAL",
    posted_at: datetime | None = T_REF - timedelta(days=1),
    collected_at: datetime | None = T_REF,
    posting_date_verified: bool | None = True,
) -> JobFilterInput:
    return JobFilterInput(
        title=title,
        normalized_title=normalized_title,
        description=description,
        location=location,
        normalized_location=normalized_location,
        remote_type=remote_type,
        experience_raw=experience_raw,
        experience_min_years=experience_min_years,
        experience_max_years=experience_max_years,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_period=salary_period,
        posted_at=posted_at,
        collected_at=collected_at,
        posting_date_verified=posting_date_verified,
    )


# ==============================================================================
# 1. ROLE TAXONOMY & PRIORITY TESTS
# ==============================================================================


class TestRoleTaxonomy:
    """Validate 14 P1 roles, 7 P2 roles, 9 P3 roles, and exclusions."""

    @pytest.mark.parametrize(
        "role_title",
        [
            "AI Engineer",
            "GenAI Engineer",
            "Applied AI Engineer",
            "AI Software Engineer",
            "AI Developer",
            "GenAI Developer",
            "Agentic AI Engineer",
            "Agentic AI Developer",
            "LLM Engineer",
            "LLM Application Developer",
            "AI Application Engineer",
            "AI Application Developer",
            "RAG Engineer",
            "AI Agent Engineer",
        ],
    )
    def test_all_p1_roles(self, role_title: str):
        job = _make_input(title=role_title)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "P1"
        assert res.relevance_status == "TARGET"
        assert res.eligibility_status == "ELIGIBLE"

    @pytest.mark.parametrize(
        "role_title",
        [
            "Forward Deployed Engineer (AI/FDE)",
            "AI Backend Engineer",
            "AI/ML Software Engineer",
            "Python Backend Engineer",
            "Python Backend Developer",
            "Backend Engineer",
            "Applied Machine Learning Engineer",
        ],
    )
    def test_all_p2_roles(self, role_title: str):
        job = _make_input(title=role_title)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "P2"
        assert res.relevance_status == "ADJACENT"
        assert res.eligibility_status == "ELIGIBLE"

    @pytest.mark.parametrize(
        "role_title",
        [
            "ML Engineer",
            "Machine Learning Engineer",
            "Python Developer",
            "Software Engineer",
            "Software Developer",
            "SDE",
            "Junior Software Engineer",
            "AI Automation Engineer",
            "AI Automation Developer",
        ],
    )
    def test_all_p3_roles(self, role_title: str):
        job = _make_input(title=role_title)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "P3"
        assert res.relevance_status == "ADJACENT"
        assert res.eligibility_status == "ELIGIBLE"

    @pytest.mark.parametrize(
        "role_title",
        [
            "Salesforce Developer",
            "Frontend Developer",
            "React Developer",
            "CSS Specialist",
            "IT Support",
            "Desktop Support",
            "QA Engineer",
            "SDET",
            "Manual Tester",
            "Sales Executive",
            "HR Manager",
            "Recruiter",
            "Marketing Associate",
            "Operations Lead",
            "AI Architect",
            "GenAI Architect",
            "AI Product Manager",
            "AI Business Analyst",
        ],
    )
    def test_hard_excluded_roles(self, role_title: str):
        job = _make_input(title=role_title)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "NOT_RELEVANT"
        assert res.relevance_status == "EXCLUDED"
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXCLUDED_ROLE
        assert REJECTION_EXCLUDED_ROLE in res.reason_codes

    def test_non_matching_role_outside_taxonomy(self):
        job = _make_input(title="Civil Site Supervisor")
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "NOT_RELEVANT"
        assert res.relevance_status == "EXCLUDED"
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXCLUDED_ROLE


class TestJavaDotNetNuance:
    """Validate Section 3.2 Java-only & .NET-only tech stack rules and exceptions."""

    def test_pure_java_developer_rejected(self):
        job = _make_input(
            title="Core Java Developer",
            description="Enterprise banking application with Spring Boot and Oracle DB.",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "NOT_RELEVANT"
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXCLUDED_ROLE

    def test_pure_dotnet_developer_rejected(self):
        job = _make_input(
            title=".NET Developer",
            description="C# ASP.NET MVC monolithic maintenance.",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "NOT_RELEVANT"
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXCLUDED_ROLE

    def test_ai_role_with_java_mention_preserved(self):
        job = _make_input(
            title="AI Engineer (Java/Python)",
            description="Integrate LLMs with legacy Java backend systems.",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.role_priority == "P1"
        assert res.eligibility_status == "ELIGIBLE"

    def test_ambiguous_java_role_with_modern_stack_in_jd_routes_to_review(self):
        job = _make_input(
            title="Backend Developer - Java",
            description="Build modern microservices and integrate LLM agents with LangChain.",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_EXCLUSION_AMBIGUOUS
        assert REVIEW_EXCLUSION_AMBIGUOUS in res.reason_codes


# ==============================================================================
# 2. LOCATION RULES TESTS
# ==============================================================================


class TestLocationRules:
    """Validate P1/P2 hubs, remote handling, unlisted rejections, and ambiguity."""

    @pytest.mark.parametrize(
        ("raw_loc", "norm_loc"),
        [
            ("Mumbai, Maharashtra", "Mumbai"),
            ("Navi Mumbai, India", "Navi Mumbai"),
            ("Thane West", "Thane"),
            ("Pune, Hinjewadi Phase 1", "Pune"),
        ],
    )
    def test_p1_locations_eligible(self, raw_loc: str, norm_loc: str):
        job = _make_input(location=raw_loc, normalized_location=norm_loc)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"
        assert res.evaluation_context["location_tier"] == "P1"

    @pytest.mark.parametrize(
        ("raw_loc", "norm_loc"),
        [
            ("Bengaluru, Karnataka", "Bengaluru"),
            ("Bangalore, India", "Bengaluru"),
            ("Hyderabad, HITEC City", "Hyderabad"),
        ],
    )
    def test_p2_locations_eligible(self, raw_loc: str, norm_loc: str):
        job = _make_input(location=raw_loc, normalized_location=norm_loc)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"
        assert res.evaluation_context["location_tier"] == "P2"

    def test_remote_with_p1_or_p2_hub_is_eligible(self):
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            remote_type="REMOTE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_remote_outside_hubs_routes_to_review(self):
        job = _make_input(
            location="Remote, India",
            normalized_location=None,
            remote_type="REMOTE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_LOCATION_REMOTE_REVIEW
        assert REVIEW_LOCATION_REMOTE_REVIEW in res.reason_codes

    @pytest.mark.parametrize(
        "unlisted_loc",
        [
            "Noida, Uttar Pradesh",
            "Gurgaon, Haryana",
            "Delhi, NCR",
            "Chennai, Tamil Nadu",
            "Kolkata, West Bengal",
            "Ahmedabad, Gujarat",
            "London, UK",
            "Austin, Texas",
            "Tel Aviv, Israel",
            "Tel-Aviv",
            "São Paulo, Brazil",
            "Sao Paulo, Brazil",
            "Paris, France",
            "Tokyo, Japan",
            "Tōkyō, Japan",
        ],
    )
    def test_unlisted_on_site_locations_rejected(self, unlisted_loc: str):
        job = _make_input(
            location=unlisted_loc,
            normalized_location=None,
            remote_type="ON_SITE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_LOCATION_NOT_ALLOWED
        assert REJECTION_LOCATION_NOT_ALLOWED in res.reason_codes

    def test_missing_location_routes_to_review(self):
        job = _make_input(location=None, normalized_location=None, remote_type="UNKNOWN")
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_LOCATION_AMBIGUOUS
        assert REVIEW_LOCATION_AMBIGUOUS in res.reason_codes

    def test_multi_city_ambiguous_location_routes_to_review(self):
        job = _make_input(
            location="Mumbai / Delhi / Noida",
            normalized_location=None,
            remote_type="ON_SITE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_LOCATION_AMBIGUOUS

    @pytest.mark.parametrize(
        "foreign_loc",
        [
            "Tel Aviv, Israel",
            "São Paulo, Brazil",
            "Sao Paulo, Brazil",
            "Paris, France",
            "Tokyo, Japan",
        ],
    )
    def test_remote_foreign_cities_route_to_review(self, foreign_loc: str):
        job = _make_input(
            location=foreign_loc,
            normalized_location=None,
            remote_type="REMOTE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_LOCATION_REMOTE_REVIEW
        assert REVIEW_LOCATION_REMOTE_REVIEW in res.reason_codes

    def test_foreign_city_in_jd_does_not_reject_target_hub_job(self):
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            description="Our company has offices in Paris, Tokyo, and Tel Aviv. This position is in Bengaluru.",
            remote_type="ON_SITE",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"
        assert res.evaluation_context["location_tier"] == "P2"


# ==============================================================================
# 3. EXPERIENCE RULES TESTS
# ==============================================================================


class TestExperienceRules:
    """Validate fresher rules, experience ladder, 1-3y review, and >= 2y exclusion."""

    @pytest.mark.parametrize(
        ("min_y", "max_y", "raw"),
        [
            (Decimal("0.0"), Decimal("0.0"), "Fresher"),
            (Decimal("0.0"), Decimal("1.0"), "0-1 years"),
            (Decimal("0.0"), Decimal("2.0"), "0-2 years"),
            (Decimal("1.0"), Decimal("2.0"), "1-2 years"),
        ],
    )
    def test_eligible_experience_bands(
        self,
        min_y: Decimal,
        max_y: Decimal,
        raw: str,
    ):
        job = _make_input(
            experience_min_years=min_y,
            experience_max_years=max_y,
            experience_raw=raw,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_overlapping_one_to_three_years_routes_to_review(self):
        job = _make_input(
            experience_min_years=Decimal("1.0"),
            experience_max_years=Decimal("3.0"),
            experience_raw="1-3 years",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_EXPERIENCE_AMBIGUOUS
        assert REVIEW_EXPERIENCE_AMBIGUOUS in res.reason_codes

    @pytest.mark.parametrize(
        ("min_y", "max_y", "raw"),
        [
            (Decimal("2.0"), Decimal("3.0"), "2-3 years"),
            (Decimal("2.0"), Decimal("4.0"), "2-4 years"),
            (Decimal("2.0"), Decimal("5.0"), "2-5 years"),
            (Decimal("3.0"), Decimal("5.0"), "3-5 years"),
            (Decimal("4.0"), None, "4+ years"),
        ],
    )
    def test_minimum_experience_ge_two_years_rejected(
        self,
        min_y: Decimal,
        max_y: Decimal | None,
        raw: str,
    ):
        job = _make_input(
            experience_min_years=min_y,
            experience_max_years=max_y,
            experience_raw=raw,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH
        assert REJECTION_EXPERIENCE_TOO_HIGH in res.reason_codes

    @pytest.mark.parametrize(
        "senior_title",
        [
            "Senior AI Engineer",
            "Lead AI Developer",
            "Principal Software Engineer",
            "Staff LLM Engineer",
            "Senior Backend Engineer",
        ],
    )
    def test_senior_title_rejected_as_experience_too_high(self, senior_title: str):
        job = _make_input(title=senior_title)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH
        # Role priority remains preserved orthogonally!
        assert res.role_priority in ("P1", "P2", "P3")

    def test_missing_experience_routes_to_review(self):
        job = _make_input(
            experience_min_years=None,
            experience_max_years=None,
            experience_raw=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_EXPERIENCE_AMBIGUOUS


# ==============================================================================
# 4. SALARY RULES TESTS
# ==============================================================================


class TestSalaryRules:
    """Validate regional minimum thresholds, Mumbai exception, and unknown salary."""

    def test_pune_below_minimum_rejected(self):
        # Pune minimum is ₹3.6 LPA (₹360,000/yr or ₹30,000/mo)
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("3.5"),  # 3.5 LPA < 3.6 LPA
            salary_max=Decimal("3.5"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_pune_monthly_below_minimum_rejected(self):
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("25000.00"),  # ₹25k/mo = ₹3.0 LPA < ₹3.6 LPA
            salary_max=Decimal("25000.00"),
            salary_period="MONTHLY",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_pune_exactly_at_minimum_eligible(self):
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("360000.00"),  # Exactly ₹3.6 LPA
            salary_max=Decimal("360000.00"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_pune_above_minimum_eligible(self):
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("4.5"),  # 4.5 LPA > 3.6 LPA
            salary_max=Decimal("6.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_bengaluru_below_minimum_rejected(self):
        # Bengaluru minimum is ₹5.0 LPA
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("490000.00"),  # 4.9 LPA < 5.0 LPA
            salary_max=Decimal("490000.00"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_bengaluru_exactly_at_minimum_eligible(self):
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("5.0"),  # Exactly 5.0 LPA
            salary_max=Decimal("5.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_bengaluru_above_minimum_eligible(self):
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("700000.00"),  # 7.0 LPA > 5.0 LPA
            salary_max=Decimal("1000000.00"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_hyderabad_below_minimum_rejected(self):
        # Hyderabad minimum is ₹5.0 LPA
        job = _make_input(
            location="Hyderabad, Telangana",
            normalized_location="Hyderabad",
            salary_min=Decimal("4.0"),  # 4.0 LPA < 5.0 LPA
            salary_max=Decimal("4.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_hyderabad_exactly_at_minimum_eligible(self):
        job = _make_input(
            location="Hyderabad, Telangana",
            normalized_location="Hyderabad",
            salary_min=Decimal("500000.00"),
            salary_max=Decimal("500000.00"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    @pytest.mark.parametrize(
        ("mumbai_city", "salary"),
        [
            ("Mumbai", Decimal("200000.00")),  # ₹2.0 LPA
            ("Navi Mumbai", Decimal("250000.00")),  # ₹2.5 LPA
            ("Thane", Decimal("300000.00")),  # ₹3.0 LPA
        ],
    )
    def test_mumbai_hubs_have_no_strict_minimum(self, mumbai_city: str, salary: Decimal):
        job = _make_input(
            location=mumbai_city,
            normalized_location=mumbai_city,
            salary_min=salary,
            salary_max=salary,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_pune_range_crossing_minimum_eligible(self):
        # Range crossing Pune threshold: 3.0 - 4.5 LPA (crosses ₹3.6 LPA) -> ELIGIBLE per Maximum Recall
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("3.0"),
            salary_max=Decimal("4.5"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_pune_monthly_range_crossing_minimum_eligible(self):
        # Monthly range: ₹25,000 - ₹35,000 / month (crosses ₹30k/mo minimum) -> ELIGIBLE
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("25000.00"),
            salary_max=Decimal("35000.00"),
            salary_period="MONTHLY",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_pune_monthly_range_entirely_below_rejected(self):
        # Monthly range: ₹20,000 - ₹28,000 / month (max is below ₹30k/mo minimum) -> REJECTED
        job = _make_input(
            location="Pune, Maharashtra",
            normalized_location="Pune",
            salary_min=Decimal("20000.00"),
            salary_max=Decimal("28000.00"),
            salary_period="MONTHLY",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_bengaluru_range_crossing_minimum_eligible(self):
        # Bengaluru range: 4.0 - 6.0 LPA (crosses ₹5.0 LPA minimum) -> ELIGIBLE
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("4.0"),
            salary_max=Decimal("6.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_bengaluru_monthly_range_crossing_minimum_eligible(self):
        # Bengaluru monthly: ₹35,000 - ₹50,000 / month (crosses ₹41,666/mo = ₹5.0 LPA) -> ELIGIBLE
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("35000.00"),
            salary_max=Decimal("50000.00"),
            salary_period="MONTHLY",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_bengaluru_range_entirely_below_rejected(self):
        # Bengaluru range: 3.0 - 4.8 LPA (entirely below ₹5.0 LPA) -> REJECTED
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("3.0"),
            salary_max=Decimal("4.8"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_hyderabad_range_crossing_minimum_eligible(self):
        # Hyderabad range: 4.5 - 5.5 LPA (crosses ₹5.0 LPA) -> ELIGIBLE
        job = _make_input(
            location="Hyderabad, Telangana",
            normalized_location="Hyderabad",
            salary_min=Decimal("4.5"),
            salary_max=Decimal("5.5"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_only_min_salary_below_rejected(self):
        # Only salary_min specified: 4.0 LPA in Bengaluru (< 5.0 LPA) -> REJECTED
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("4.0"),
            salary_max=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_only_min_salary_at_threshold_eligible(self):
        # Only salary_min specified: 5.0 LPA in Bengaluru (== 5.0 LPA) -> ELIGIBLE
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("5.0"),
            salary_max=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_only_max_salary_below_rejected(self):
        # Only salary_max specified: 4.5 LPA in Bengaluru (< 5.0 LPA) -> REJECTED
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=None,
            salary_max=Decimal("4.5"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_only_max_salary_at_threshold_eligible(self):
        # Only salary_max specified: 5.0 LPA in Bengaluru (== 5.0 LPA) -> ELIGIBLE
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=None,
            salary_max=Decimal("5.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_unknown_salary_never_rejected_routes_to_review(self):
        job = _make_input(
            salary_min=None,
            salary_max=None,
            salary_currency=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_SALARY_UNKNOWN
        assert REVIEW_SALARY_UNKNOWN in res.reason_codes


# ==============================================================================
# 5. FRESHNESS & PUBLICATION DATE TESTS
# ==============================================================================


class TestFreshnessRules:
    """Validate 5-day active feed window, boundaries, missing, and suspicious dates."""

    def test_fresh_job_one_day_old_is_eligible(self):
        job = _make_input(posted_at=T_REF - timedelta(days=1))
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_fresh_job_immediately_inside_window_is_eligible(self):
        # 4 days, 23 hours, 59 minutes old
        posted = T_REF - timedelta(days=4, hours=23, minutes=59)
        job = _make_input(posted_at=posted)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_fresh_job_exactly_on_window_boundary_is_eligible(self):
        # Exactly 5 days old
        posted = T_REF - timedelta(days=5)
        job = _make_input(posted_at=posted)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"

    def test_expired_job_immediately_outside_window_is_expired(self):
        # 5 days + 1 second old
        posted = T_REF - timedelta(days=5, seconds=1)
        job = _make_input(posted_at=posted)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "EXPIRED"
        assert res.rejection_reason == REJECTION_JOB_TOO_OLD
        assert REJECTION_JOB_TOO_OLD in res.reason_codes

    def test_expired_job_seven_days_old_is_expired(self):
        posted = T_REF - timedelta(days=7)
        job = _make_input(posted_at=posted)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "EXPIRED"
        assert res.rejection_reason == REJECTION_JOB_TOO_OLD

    def test_missing_posted_at_routes_to_review(self):
        # Never fill posted_at with collected_at!
        job = _make_input(posted_at=None, collected_at=T_REF)
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        assert REVIEW_POSTED_DATE_SUSPICIOUS in res.reason_codes

    def test_future_posted_at_routes_to_review(self):
        job = _make_input(posted_at=T_REF + timedelta(hours=2))
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS

    def test_implausible_date_older_than_ninety_days_routes_to_review(self):
        job = _make_input(
            posted_at=T_REF - timedelta(days=95),
            collected_at=T_REF,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS

    def test_freshness_missing_posted_at_never_uses_collected_at(self):
        # Even if collected_at was 5 minutes ago, missing posted_at cannot be assumed fresh!
        job = _make_input(
            posted_at=None,
            collected_at=T_REF - timedelta(minutes=5),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        assert res.passed is False

    def test_freshness_past_five_days_with_recent_collected_at_is_expired(self):
        # Late-discovered job: posted 6 days ago, ingested 10 minutes ago
        job = _make_input(
            posted_at=T_REF - timedelta(days=6),
            collected_at=T_REF - timedelta(minutes=10),
            posting_date_verified=True,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "EXPIRED"
        assert res.rejection_reason == REJECTION_JOB_TOO_OLD
        assert res.passed is False

    def test_publication_date_unverified_cannot_make_job_eligible(self):
        # A recent-looking posted_at (1 hour ago) with posting_date_verified=False:
        # Cannot establish active freshness -> routes to REVIEW with POSTED_DATE_SUSPICIOUS
        job = _make_input(
            posted_at=T_REF - timedelta(hours=1),
            posting_date_verified=False,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        assert res.passed is False

    def test_publication_date_unresolved_cannot_make_job_eligible(self):
        # A recent-looking posted_at with posting_date_verified=None (unresolved):
        # Do not silently assume verification succeeded -> routes to REVIEW with POSTED_DATE_SUSPICIOUS
        job = _make_input(
            posted_at=T_REF - timedelta(hours=1),
            posting_date_verified=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        assert res.passed is False

    def test_publication_date_verified_recent_is_eligible(self):
        # Explicitly verified publication date (posting_date_verified=True) within 5 days is ELIGIBLE
        job = _make_input(
            posted_at=T_REF - timedelta(hours=1),
            posting_date_verified=True,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "ELIGIBLE"
        assert res.passed is True

    def test_default_job_filter_input_without_verification_is_unresolved(self):
        # Default instantiation of JobFilterInput has posting_date_verified=None
        # Must route to REVIEW rather than silently passing
        job = JobFilterInput(
            title="AI Engineer",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("600000.00"),
            posted_at=T_REF - timedelta(hours=1),
        )
        assert job.posting_date_verified is None
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        assert res.passed is False


# ==============================================================================
# 6. CONFLICTING RULES & PRECEDENCE TESTS
# ==============================================================================


class TestPrecedenceAndConflicts:
    """Validate strict precedence order when multiple disqualifications apply."""

    def test_role_exclusion_precedes_everything(self):
        # Salesforce Developer + Senior (Exp) + Noida (Loc) + Low Salary + Expired
        job = _make_input(
            title="Senior Salesforce Developer",
            location="Noida, Uttar Pradesh",
            normalized_location=None,
            experience_min_years=Decimal("5.0"),
            salary_min=Decimal("2.0"),
            posted_at=T_REF - timedelta(days=10),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXCLUDED_ROLE
        assert res.role_priority == "NOT_RELEVANT"

    def test_experience_exclusion_precedes_location_exclusion(self):
        # Valid AI role + Senior (Exp) + Noida (Loc)
        job = _make_input(
            title="Lead AI Engineer",
            location="Noida, Uttar Pradesh",
            normalized_location=None,
            experience_min_years=Decimal("4.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH
        assert res.role_priority == "P1"  # Role priority preserved!

    def test_location_exclusion_precedes_salary_exclusion(self):
        # Valid AI role + Entry exp + Chennai (Loc) + Low Salary
        job = _make_input(
            title="AI Engineer",
            location="Chennai, Tamil Nadu",
            normalized_location=None,
            salary_min=Decimal("2.0"),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_LOCATION_NOT_ALLOWED

    def test_salary_exclusion_precedes_expiry(self):
        # Bengaluru location + Salary 4.0 LPA (< 5.0 LPA) + 7 days old
        job = _make_input(
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("4.0"),
            salary_max=Decimal("4.0"),
            posted_at=T_REF - timedelta(days=7),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REJECTED"
        assert res.rejection_reason == REJECTION_SALARY_BELOW_MINIMUM

    def test_expiry_precedes_ambiguity_reviews(self):
        # Missing salary + 7 days old -> EXPIRED, not REVIEW
        job = _make_input(
            salary_min=None,
            salary_max=None,
            posted_at=T_REF - timedelta(days=7),
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "EXPIRED"
        assert res.rejection_reason == REJECTION_JOB_TOO_OLD

    def test_multiple_reviews_collected_in_reason_codes(self):
        # Missing location + missing salary + 1-3y experience
        job = _make_input(
            location=None,
            normalized_location=None,
            salary_min=None,
            salary_max=None,
            experience_min_years=Decimal("1.0"),
            experience_max_years=Decimal("3.0"),
            experience_raw="1-3 years",
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.review_reason == REVIEW_LOCATION_AMBIGUOUS
        assert REVIEW_LOCATION_AMBIGUOUS in res.reason_codes
        assert REVIEW_EXPERIENCE_AMBIGUOUS in res.reason_codes
        assert REVIEW_SALARY_UNKNOWN in res.reason_codes

    def test_all_five_review_reasons_simultaneously(self):
        # 1. missing date (POSTED_DATE_SUSPICIOUS)
        # 2. ambiguous java stack (EXCLUSION_AMBIGUOUS)
        # 3. remote outside hubs (LOCATION_REMOTE_REVIEW)
        # 4. 1-3y experience (EXPERIENCE_AMBIGUOUS)
        # 5. missing salary (SALARY_UNKNOWN)
        job = _make_input(
            title="Backend Developer - Java",
            description="Build modern microservices and integrate LLM agents with LangChain.",
            location="Remote, Anywhere",
            normalized_location=None,
            remote_type="REMOTE",
            experience_min_years=Decimal("1.0"),
            experience_max_years=Decimal("3.0"),
            experience_raw="1-3 years",
            salary_min=None,
            salary_max=None,
            posted_at=None,
        )
        res = evaluate_job_filter(job, reference_time=T_REF)
        assert res.eligibility_status == "REVIEW"
        assert res.passed is False
        assert res.rejection_reason is None
        # Primary review reason is deterministically POSTED_DATE_SUSPICIOUS
        assert res.review_reason == REVIEW_POSTED_DATE_SUSPICIOUS
        # All 5 reasons collected in reason_codes
        assert REVIEW_POSTED_DATE_SUSPICIOUS in res.reason_codes
        assert REVIEW_EXCLUSION_AMBIGUOUS in res.reason_codes
        assert REVIEW_LOCATION_REMOTE_REVIEW in res.reason_codes
        assert REVIEW_EXPERIENCE_AMBIGUOUS in res.reason_codes
        assert REVIEW_SALARY_UNKNOWN in res.reason_codes
        assert len(res.reason_codes) == 5

    def test_seven_stages_precedence_chain(self):
        # Stage 1 (Role Exclusion) > Stage 2 (Experience Too High)
        job_s1_s2 = _make_input(
            title="Senior Salesforce Developer",
            experience_min_years=Decimal("5.0"),
        )
        assert (
            evaluate_job_filter(job_s1_s2, reference_time=T_REF).rejection_reason
            == REJECTION_EXCLUDED_ROLE
        )

        # Stage 2 (Experience Too High) > Stage 3 (Location Not Allowed)
        job_s2_s3 = _make_input(
            title="Principal AI Engineer",
            experience_min_years=Decimal("5.0"),
            location="Noida, Uttar Pradesh",
            normalized_location=None,
        )
        assert (
            evaluate_job_filter(job_s2_s3, reference_time=T_REF).rejection_reason
            == REJECTION_EXPERIENCE_TOO_HIGH
        )

        # Stage 3 (Location Not Allowed) > Stage 4 (Salary Below Minimum)
        job_s3_s4 = _make_input(
            title="AI Engineer",
            location="Noida, Uttar Pradesh",
            normalized_location=None,
            salary_min=Decimal("2.0"),
            salary_max=Decimal("2.0"),
        )
        assert (
            evaluate_job_filter(job_s3_s4, reference_time=T_REF).rejection_reason
            == REJECTION_LOCATION_NOT_ALLOWED
        )

        # Stage 4 (Salary Below Minimum) > Stage 5 (Expiry > 5 days)
        job_s4_s5 = _make_input(
            title="AI Engineer",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=Decimal("3.0"),
            salary_max=Decimal("3.0"),
            posted_at=T_REF - timedelta(days=10),
        )
        assert (
            evaluate_job_filter(job_s4_s5, reference_time=T_REF).rejection_reason
            == REJECTION_SALARY_BELOW_MINIMUM
        )

        # Stage 5 (Expiry > 5 days) > Stage 6 (Review / Ambiguity)
        job_s5_s6 = _make_input(
            title="AI Engineer",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=None,
            salary_max=None,
            posted_at=T_REF - timedelta(days=10),
        )
        res_s5 = evaluate_job_filter(job_s5_s6, reference_time=T_REF)
        assert res_s5.eligibility_status == "EXPIRED"
        assert res_s5.rejection_reason == REJECTION_JOB_TOO_OLD

        # Stage 6 (Review / Ambiguity) > Stage 7 (Eligible)
        job_s6_s7 = _make_input(
            title="AI Engineer",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            salary_min=None,
            salary_max=None,  # Missing salary triggers REVIEW
            posted_at=T_REF - timedelta(days=1),
        )
        res_s6 = evaluate_job_filter(job_s6_s7, reference_time=T_REF)
        assert res_s6.eligibility_status == "REVIEW"
        assert res_s6.passed is False

        # Stage 7 (Eligible)
        job_s7 = _make_input()
        res_s7 = evaluate_job_filter(job_s7, reference_time=T_REF)
        assert res_s7.eligibility_status == "ELIGIBLE"
        assert res_s7.passed is True


# ==============================================================================
# 7. REPEATABILITY & CONTRACT TESTS
# ==============================================================================


class TestRepeatabilityAndContract:
    """Validate 100% deterministic repeatability and model conversion contracts."""

    def test_deterministic_repeatability(self):
        job = _make_input()
        res1 = evaluate_job_filter(job, reference_time=T_REF)
        res2 = evaluate_job_filter(job, reference_time=T_REF)

        assert res1.eligibility_status == res2.eligibility_status
        assert res1.role_priority == res2.role_priority
        assert res1.relevance_status == res2.relevance_status
        assert res1.rejection_reason == res2.rejection_reason
        assert res1.review_reason == res2.review_reason
        assert res1.reason_codes == res2.reason_codes
        assert res1.passed == res2.passed
        assert res1.evaluation_context == res2.evaluation_context

    def test_from_normalized_job_factory(self):
        norm = NormalizedJobData(
            title="GenAI Engineer",
            normalized_title="genai engineer",
            description="Build RAG pipelines with FastAPI.",
            location="Bengaluru, Karnataka",
            normalized_location="Bengaluru",
            remote_type="ON_SITE",
            experience_raw="0-2 years",
            experience_min_years=Decimal("0.0"),
            experience_max_years=Decimal("2.0"),
            salary_min=Decimal("800000.00"),
            salary_max=Decimal("1200000.00"),
            salary_currency="INR",
            salary_period="ANNUAL",
            posted_at=T_REF - timedelta(days=2),
        )
        # By default, posting_date_verified is None (unresolved) -> routes to REVIEW
        job_input_default = JobFilterInput.from_normalized_job(norm, collected_at=T_REF)
        res_default = evaluate_job_filter(job_input_default, reference_time=T_REF)
        assert res_default.role_priority == "P1"
        assert res_default.eligibility_status == "REVIEW"
        assert res_default.review_reason == "POSTED_DATE_SUSPICIOUS"
        assert res_default.passed is False

        # When posting_date_verified is True -> routes to ELIGIBLE
        job_input_verified = JobFilterInput.from_normalized_job(
            norm, collected_at=T_REF, posting_date_verified=True
        )
        res_verified = evaluate_job_filter(job_input_verified, reference_time=T_REF)
        assert res_verified.role_priority == "P1"
        assert res_verified.eligibility_status == "ELIGIBLE"
        assert res_verified.passed is True

    def test_to_filter_decision_kwargs(self):
        job = _make_input()
        res = evaluate_job_filter(job, reference_time=T_REF)
        test_uuid = uuid.uuid4()
        kwargs = res.to_filter_decision_kwargs(test_uuid)

        assert kwargs["job_id"] == test_uuid
        assert kwargs["rule_version"] == DEFAULT_RULE_VERSION
        assert kwargs["final_status"] == "ELIGIBLE"
        assert kwargs["role_priority"] == "P1"
        assert isinstance(kwargs["reason_codes"], list)
        assert isinstance(kwargs["evaluation_context"], dict)

    def test_to_job_status_updates(self):
        job = _make_input()
        res = evaluate_job_filter(job, reference_time=T_REF)
        updates = res.to_job_status_updates()

        assert updates["role_priority"] == "P1"
        assert updates["relevance_status"] == "TARGET"
        assert updates["eligibility_status"] == "ELIGIBLE"
        assert updates["rejection_reason"] is None
        assert updates["review_reason"] is None

    def test_status_semantics_passed_flag_strictly_for_eligible(self):
        # 1. ELIGIBLE -> passed is True
        res_eligible = evaluate_job_filter(_make_input(), reference_time=T_REF)
        assert res_eligible.eligibility_status == "ELIGIBLE"
        assert res_eligible.passed is True
        assert res_eligible.rejection_reason is None
        assert res_eligible.review_reason is None
        assert res_eligible.reason_codes == []

        # 2. REJECTED -> passed is False
        res_rejected = evaluate_job_filter(
            _make_input(title="Salesforce Developer"), reference_time=T_REF
        )
        assert res_rejected.eligibility_status == "REJECTED"
        assert res_rejected.passed is False
        assert res_rejected.rejection_reason == REJECTION_EXCLUDED_ROLE
        assert res_rejected.review_reason is None
        assert res_rejected.reason_codes == [REJECTION_EXCLUDED_ROLE]

        # 3. EXPIRED -> passed is False
        res_expired = evaluate_job_filter(
            _make_input(posted_at=T_REF - timedelta(days=6)), reference_time=T_REF
        )
        assert res_expired.eligibility_status == "EXPIRED"
        assert res_expired.passed is False
        assert res_expired.rejection_reason == REJECTION_JOB_TOO_OLD
        assert res_expired.review_reason is None
        assert res_expired.reason_codes == [REJECTION_JOB_TOO_OLD]

        # 4. REVIEW -> passed is False
        res_review = evaluate_job_filter(
            _make_input(salary_min=None, salary_max=None), reference_time=T_REF
        )
        assert res_review.eligibility_status == "REVIEW"
        assert res_review.passed is False
        assert res_review.rejection_reason is None
        assert res_review.review_reason == REVIEW_SALARY_UNKNOWN
        assert res_review.reason_codes == [REVIEW_SALARY_UNKNOWN]

    def test_role_priority_orthogonality_with_senior_ai_roles(self):
        # P1 role with Senior title: role_priority = P1, eligibility = REJECTED (EXPERIENCE_TOO_HIGH)
        job_p1 = _make_input(title="Senior AI Engineer")
        res_p1 = evaluate_job_filter(job_p1, reference_time=T_REF)
        assert res_p1.role_priority == "P1"
        assert res_p1.relevance_status == "TARGET"
        assert res_p1.eligibility_status == "REJECTED"
        assert res_p1.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH

        # P2 role with Lead title: role_priority = P2, eligibility = REJECTED (EXPERIENCE_TOO_HIGH)
        job_p2 = _make_input(title="Lead Backend Engineer")
        res_p2 = evaluate_job_filter(job_p2, reference_time=T_REF)
        assert res_p2.role_priority == "P2"
        assert res_p2.relevance_status == "ADJACENT"
        assert res_p2.eligibility_status == "REJECTED"
        assert res_p2.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH

        # P3 role with Staff title: role_priority = P3, eligibility = REJECTED (EXPERIENCE_TOO_HIGH)
        job_p3 = _make_input(title="Staff Software Engineer")
        res_p3 = evaluate_job_filter(job_p3, reference_time=T_REF)
        assert res_p3.role_priority == "P3"
        assert res_p3.relevance_status == "ADJACENT"
        assert res_p3.eligibility_status == "REJECTED"
        assert res_p3.rejection_reason == REJECTION_EXPERIENCE_TOO_HIGH

    def test_location_distinct_categories_isolation(self):
        # 1. Approved P1 hub on-site: ELIGIBLE
        res1 = evaluate_job_filter(
            _make_input(
                location="Pune, Maharashtra", normalized_location="Pune", remote_type="ON_SITE"
            ),
            reference_time=T_REF,
        )
        assert res1.eligibility_status == "ELIGIBLE"

        # 2. Approved P2 hub remote: ELIGIBLE
        res2 = evaluate_job_filter(
            _make_input(
                location="Bengaluru, Karnataka",
                normalized_location="Bengaluru",
                remote_type="REMOTE",
            ),
            reference_time=T_REF,
        )
        assert res2.eligibility_status == "ELIGIBLE"

        # 3. Remote outside hubs: REVIEW (LOCATION_REMOTE_REVIEW)
        res3 = evaluate_job_filter(
            _make_input(
                location="Remote, Worldwide", normalized_location=None, remote_type="REMOTE"
            ),
            reference_time=T_REF,
        )
        assert res3.eligibility_status == "REVIEW"
        assert res3.review_reason == REVIEW_LOCATION_REMOTE_REVIEW

        # 4. Missing location: REVIEW (LOCATION_AMBIGUOUS)
        res4 = evaluate_job_filter(
            _make_input(location=None, normalized_location=None, remote_type="ON_SITE"),
            reference_time=T_REF,
        )
        assert res4.eligibility_status == "REVIEW"
        assert res4.review_reason == REVIEW_LOCATION_AMBIGUOUS

        # 5. Multi-city location: REVIEW (LOCATION_AMBIGUOUS)
        res5 = evaluate_job_filter(
            _make_input(
                location="Mumbai / Pune / Delhi", normalized_location=None, remote_type="ON_SITE"
            ),
            reference_time=T_REF,
        )
        assert res5.eligibility_status == "REVIEW"
        assert res5.review_reason == REVIEW_LOCATION_AMBIGUOUS

        # 6. Known unlisted city on-site: REJECTED (LOCATION_NOT_ALLOWED)
        res6 = evaluate_job_filter(
            _make_input(
                location="Noida, Uttar Pradesh", normalized_location=None, remote_type="ON_SITE"
            ),
            reference_time=T_REF,
        )
        assert res6.eligibility_status == "REJECTED"
        assert res6.rejection_reason == REJECTION_LOCATION_NOT_ALLOWED
