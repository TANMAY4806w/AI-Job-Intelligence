"""Unit tests for the deterministic job normalization service.

Follows tests/README.md: isolated in-memory unit tests with zero database dependency.
"""

import copy
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.normalization import (
    ExtractedJobPayload,
    JobNormalizationService,
    NormalizedJobData,
    normalize_job_payload,
    normalize_location,
    normalize_title,
    normalize_url,
    parse_publication_date,
)
from app.normalization.experience import parse_experience
from app.normalization.text import clean_bullet_points, clean_html_to_text


class TestTitleNormalization:
    """Tests for advertised title cleaning and Stage 3 normalized title generation."""

    def test_whitespace_and_case_normalization(self) -> None:
        raw = "   Senior   AI   Engineer   "
        cleaned, normalized = normalize_title(raw)
        assert cleaned == "Senior AI Engineer"
        assert normalized == "senior ai engineer"

    def test_seniority_distinction_and_abbreviation_equivalence(self) -> None:
        """Verifies Senior AI Engineer, Junior AI Engineer, and AI Engineer remain distinct,
        while abbreviation variants (Sr., Jr.) map to their canonical equivalent.
        """
        _, norm_senior = normalize_title("Senior AI Engineer")
        _, norm_sr = normalize_title("Sr. AI Engineer")
        _, norm_junior = normalize_title("Junior AI Engineer")
        _, norm_jr = normalize_title("Jr. AI Engineer")
        _, norm_plain = normalize_title("AI Engineer")

        # Abbreviation equivalence
        assert norm_senior == norm_sr == "senior ai engineer"
        assert norm_junior == norm_jr == "junior ai engineer"
        assert norm_plain == "ai engineer"

        # Distinct seniority levels must not collide in Stage 3 deduplication
        assert norm_senior != norm_junior
        assert norm_senior != norm_plain
        assert norm_junior != norm_plain

    def test_promotional_boilerplate_and_tags_removal(self) -> None:
        raw = "Urgent Hiring: Backend Dev [Hiring Urgently!] (Immediate Joiner)"
        cleaned, normalized = normalize_title(raw)
        assert cleaned == "Urgent Hiring: Backend Dev [Hiring Urgently!] (Immediate Joiner)"
        assert normalized == "backend developer"

    def test_seniority_prefixes_and_abbreviation_expansion(self) -> None:
        raw = "Junior Python Dev / SWE"
        cleaned, normalized = normalize_title(raw)
        assert cleaned == "Junior Python Dev / SWE"
        assert normalized == "junior python developer software engineer"

    def test_technical_acronyms_preserved(self) -> None:
        raw = "C++ Backend Eng - AI & ML"
        cleaned, normalized = normalize_title(raw)
        assert cleaned == "C++ Backend Eng - AI & ML"
        assert normalized == "cpp backend engineer ai machine learning"

    def test_empty_or_whitespace_title_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Job title cannot be empty"):
            normalize_title("")

        with pytest.raises(ValueError, match="Job title cannot be empty"):
            normalize_title("   \n\t  ")

    def test_non_string_title_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="Job title must be a string"):
            normalize_title(12345)  # type: ignore[arg-type]


class TestLocationNormalization:
    """Tests for canonical Indian tech hub resolution and ambiguous location handling."""

    @pytest.mark.parametrize(
        ("input_loc", "expected_hub"),
        [
            ("Mumbai, Maharashtra, India", "Mumbai"),
            ("Bombay", "Mumbai"),
            ("Mumbai Suburban", "Mumbai"),
            ("Navi Mumbai, Maharashtra", "Navi Mumbai"),
            ("New Bombay", "Navi Mumbai"),
            ("Thane West, Maharashtra", "Thane"),
            ("Pune, Maharashtra", "Pune"),
            ("Hinjewadi, Pune", "Pune"),
            ("Bengaluru, Karnataka", "Bengaluru"),
            ("Bangalore", "Bengaluru"),
            ("BLR", "Bengaluru"),
            ("Whitefield, Bengaluru", "Bengaluru"),
            ("Hyderabad, Telangana", "Hyderabad"),
            ("Secunderabad", "Hyderabad"),
            ("Gachibowli, Hyderabad", "Hyderabad"),
        ],
    )
    def test_canonical_target_hubs_resolution(self, input_loc: str, expected_hub: str) -> None:
        cleaned, normalized, _ = normalize_location(input_loc)
        assert cleaned == input_loc
        assert normalized == expected_hub

    @pytest.mark.parametrize(
        ("input_loc", "expected_city"),
        [
            ("Delhi, NCR", "Delhi"),
            ("New Delhi, India", "Delhi"),
            ("Noida, Uttar Pradesh", "Noida"),
            ("Greater Noida", "Noida"),
            ("Gurugram, Haryana", "Gurugram"),
            ("Gurgaon", "Gurugram"),
            ("Chennai, Tamil Nadu", "Chennai"),
            ("Madras", "Chennai"),
            ("Kolkata, West Bengal", "Kolkata"),
            ("Ahmedabad, Gujarat", "Ahmedabad"),
            ("Chandigarh, Punjab", "Chandigarh"),
            ("Mohali", "Chandigarh"),
            ("Jaipur, Rajasthan", "Jaipur"),
            ("Kochi, Kerala", "Kochi"),
            ("Trivandrum", "Kochi"),
            ("Indore, Madhya Pradesh", "Indore"),
            ("Coimbatore, Tamil Nadu", "Coimbatore"),
            ("Tel Aviv, Israel", "Tel Aviv"),
            ("Tel-Aviv", "Tel Aviv"),
            ("TLV, Israel", "Tel Aviv"),
            ("São Paulo, Brazil", "São Paulo"),
            ("Sao Paulo, Brazil", "São Paulo"),
            ("Paris, France", "Paris"),
            ("Tokyo, Japan", "Tokyo"),
            ("Tōkyō, Japan", "Tokyo"),
        ],
    )
    def test_known_non_target_cities_resolution(self, input_loc: str, expected_city: str) -> None:
        cleaned, normalized, _ = normalize_location(input_loc)
        assert cleaned == input_loc
        assert normalized == expected_city

    def test_location_classification_distinguishes_non_target_from_ambiguous(self) -> None:
        """Verifies target hubs, known out-of-scope cities, and ambiguous/missing locations
        are distinctly represented for filtering rule evaluation.
        """
        # Target Hub (P1/P2)
        _, norm_blr, _ = normalize_location("Bengaluru, Karnataka")
        assert norm_blr == "Bengaluru"

        # Known Out-of-Scope City (Allows LOCATION_NOT_ALLOWED rejection)
        _, norm_delhi, _ = normalize_location("Delhi, NCR")
        assert norm_delhi == "Delhi"

        # Ambiguous / Conflicting Multi-City (Routes to LOCATION_AMBIGUOUS review)
        _, norm_ambig, _ = normalize_location("Mumbai or Delhi")
        assert norm_ambig is None

        # Missing Location (Routes to LOCATION_AMBIGUOUS review)
        _, norm_none, _ = normalize_location(None)
        assert norm_none is None

    def test_distinguishes_navi_mumbai_from_mumbai(self) -> None:
        _, norm_navi, _ = normalize_location("Navi Mumbai")
        _, norm_mumbai, _ = normalize_location("Mumbai")
        assert norm_navi == "Navi Mumbai"
        assert norm_mumbai == "Mumbai"
        assert norm_navi != norm_mumbai

    @pytest.mark.parametrize(
        "empty_loc",
        [None, "", "   ", "N/A", "none", "null", "unknown", "not specified"],
    )
    def test_missing_or_sentinel_location_returns_null(self, empty_loc: str | None) -> None:
        cleaned, normalized, remote_hint = normalize_location(empty_loc)
        assert cleaned is None
        assert normalized is None
        assert remote_hint is None

    def test_ambiguous_multi_city_location_preserves_null(self) -> None:
        # Multi-city listing without single definitive target hub
        cleaned, normalized, _ = normalize_location("Mumbai, Bengaluru, and Pune")
        assert cleaned == "Mumbai, Bengaluru, and Pune"
        # Must be NULL per Maximum Recall Principle, never a fabricated city
        assert normalized is None

    def test_remote_work_detection(self) -> None:
        cleaned, normalized, remote_hint = normalize_location("Remote - India")
        assert cleaned == "Remote - India"
        assert normalized is None
        assert remote_hint == "REMOTE"

        # Hub combined with remote
        cleaned2, normalized2, remote_hint2 = normalize_location("Bengaluru (Work from Home)")
        assert cleaned2 == "Bengaluru (Work from Home)"
        assert normalized2 == "Bengaluru"
        assert remote_hint2 == "REMOTE"

    def test_description_foreign_cities_do_not_leak_into_location_normalization(self) -> None:
        """Verify that mentioning foreign cities in JD text does not pollute location normalization."""
        service = JobNormalizationService()
        payload = {
            "title": "AI Engineer",
            "description": (
                "Our global team spans Paris, Tokyo, and Tel Aviv. "
                "However, this role is strictly on-site in Bengaluru, India."
            ),
            "location": "Bengaluru, India",
        }
        res = service.normalize(payload)
        assert res.normalized_location == "Bengaluru"
        assert res.title == "AI Engineer"


class TestUrlNormalization:
    """Tests for application URL canonicalization and parameter preservation."""

    def test_strips_tracking_query_params_and_fragments(self) -> None:
        raw_url = (
            "HTTPS://Boards.Greenhouse.io/Company/Jobs/12345/?"
            "utm_source=linkedin&utm_medium=job_board&gh_src=test&fbclid=123#apply"
        )
        normalized = normalize_url(raw_url)
        # Scheme and host lowercased, path case preserved, trailing slash trimmed, tracking stripped
        assert normalized == "https://boards.greenhouse.io/Company/Jobs/12345"

    def test_urls_differing_only_by_tracking_params_are_identical(self) -> None:
        url_linkedin = "https://example.com/jobs/42?utm_source=linkedin&utm_medium=cpc"
        url_twitter = "https://example.com/jobs/42?utm_source=twitter&fbclid=xyz"
        url_email = "https://example.com/jobs/42?gh_src=email&ref=share#apply"

        norm_linkedin = normalize_url(url_linkedin)
        norm_twitter = normalize_url(url_twitter)
        norm_email = normalize_url(url_email)

        assert norm_linkedin == norm_twitter == norm_email == "https://example.com/jobs/42"

    def test_meaningful_query_parameters_preserved_and_prevent_collision(self) -> None:
        url_job_1 = "https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=101&token=xyz"
        url_job_2 = "https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=102&token=xyz"

        norm_1 = normalize_url(url_job_1)
        norm_2 = normalize_url(url_job_2)

        assert norm_1 == "https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=101&token=xyz"
        assert norm_2 == "https://boards.greenhouse.io/embed/job_app?for=acme&gh_jid=102&token=xyz"
        assert norm_1 != norm_2

    def test_http_scheme_not_silently_upgraded_to_https(self) -> None:
        url_http = "http://jobs.example.com/careers/backend-dev"
        normalized = normalize_url(url_http)
        assert normalized == "http://jobs.example.com/careers/backend-dev"
        assert normalized.startswith("http://")

    def test_strips_default_ports(self) -> None:
        assert normalize_url("https://example.com:443/careers/1") == "https://example.com/careers/1"
        assert normalize_url("http://example.com:80/careers/1") == "http://example.com/careers/1"

    def test_missing_or_empty_url_returns_none(self) -> None:
        assert normalize_url(None) is None
        assert normalize_url("") is None
        assert normalize_url("   ") is None

    def test_malformed_url_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Only HTTP and HTTPS are supported"):
            normalize_url("ftp://example.com/jobs/1")

        with pytest.raises(ValueError, match="Only HTTP and HTTPS are supported"):
            normalize_url("javascript:void(0)")

        with pytest.raises(ValueError, match="Invalid URL without host"):
            normalize_url("https://")


class TestPublicationDateParsing:
    """Tests for timezone-aware date parsing and missing date handling."""

    def test_iso_8601_utc_and_offset_strings(self) -> None:
        # UTC 'Z' string
        dt1 = parse_publication_date("2026-10-24T12:00:00Z")
        assert dt1 == datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)

        # Offset string (+05:30) converted to UTC
        dt2 = parse_publication_date("2026-10-24T17:30:00+05:30")
        assert dt2 == datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)

    def test_date_only_string_and_date_object(self) -> None:
        dt1 = parse_publication_date("2026-10-24")
        assert dt1 == datetime(2026, 10, 24, 0, 0, 0, tzinfo=UTC)

        dt2 = parse_publication_date(date(2026, 10, 24))
        assert dt2 == datetime(2026, 10, 24, 0, 0, 0, tzinfo=UTC)

    def test_rfc_2822_date_string(self) -> None:
        dt = parse_publication_date("Sat, 24 Oct 2026 12:00:00 +0000")
        assert dt == datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)

    def test_unix_epoch_seconds_and_milliseconds(self) -> None:
        # Seconds
        dt1 = parse_publication_date(1792843200)
        assert dt1 is not None and dt1.tzinfo == UTC

        # Milliseconds (> 1e11)
        dt2 = parse_publication_date(1792843200000)
        assert dt2 is not None and dt2 == dt1

    def test_missing_publication_date_returns_none_without_fabrication(self) -> None:
        assert parse_publication_date(None) is None
        assert parse_publication_date("") is None
        assert parse_publication_date("   ") is None

    def test_invalid_date_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unparseable publication date"):
            parse_publication_date("yesterday")

        with pytest.raises(ValueError, match="Invalid non-positive timestamp"):
            parse_publication_date(-100)

    def test_unsupported_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="Unsupported publication date type"):
            parse_publication_date({"date": "2026-10-24"})


class TestTextSanitization:
    """Tests for plain-text formatting and HTML-to-text cleaning."""

    def test_clean_html_to_text_removes_scripts_styles_and_tags(self) -> None:
        html_input = """
        <html>
            <head>
                <style>body { color: red; }</style>
                <script>alert("spam");</script>
            </head>
            <body>
                <h1>Job Opportunity</h1>
                <p>We are seeking a <strong>Python Developer</strong> to join our team.</p>
                <!-- Recruitment comment -->
                <ul>
                    <li>Build FastAPI services</li>
                    <li>Design PostgreSQL schemas</li>
                </ul>
            </body>
        </html>
        """
        cleaned = clean_html_to_text(html_input)
        assert "alert" not in cleaned
        assert "color: red" not in cleaned
        assert "<!--" not in cleaned
        assert "Job Opportunity" in cleaned
        assert "We are seeking a Python Developer to join our team." in cleaned
        assert "• Build FastAPI services" in cleaned
        assert "• Design PostgreSQL schemas" in cleaned

    def test_clean_bullet_points_from_list_and_multiline_string(self) -> None:
        items = ["• Develop AI pipelines", "- Deploy with Docker", "3. Write tests"]
        cleaned = clean_bullet_points(items)
        assert cleaned == [
            "Develop AI pipelines",
            "Deploy with Docker",
            "Write tests",
        ]


class TestExperienceAndCompensation:
    """Tests for experience band and salary parsing."""

    def test_experience_fresher_and_ranges(self) -> None:
        _, min1, max1 = parse_experience("Fresher")
        assert min1 == Decimal("0.0") and max1 == Decimal("0.0")

        _, min2, max2 = parse_experience("0-2 years")
        assert min2 == Decimal("0.0") and max2 == Decimal("2.0")

        _, min3, max3 = parse_experience("3+ yrs")
        assert min3 == Decimal("3.0") and max3 is None

    def test_compensation_lpa_and_monthly(self) -> None:
        service = JobNormalizationService()

        # 3.6 LPA single
        data1 = service.normalize(
            {
                "title": "AI Engineer",
                "description": "Job details",
                "company_name": "Acme Corp",
                "salary_raw": "₹3.6 LPA",
            }
        )
        assert data1.salary_min == Decimal("360000.00")
        assert data1.salary_max == Decimal("360000.00")
        assert data1.salary_currency == "INR"
        assert data1.salary_period == "ANNUAL"

        # Monthly compensation
        data2 = service.normalize(
            {
                "title": "Backend Engineer",
                "description": "Job details",
                "company_name": "Acme Corp",
                "salary_raw": "₹30,000 / month",
            }
        )
        assert data2.salary_min == Decimal("30000.00")
        assert data2.salary_max == Decimal("30000.00")
        assert data2.salary_currency == "INR"
        assert data2.salary_period == "MONTHLY"

        # Undisclosed compensation must preserve salary_currency as None (no fabrication)
        data3 = service.normalize(
            {
                "title": "Backend Engineer",
                "description": "Job details",
                "company_name": "Acme Corp",
            }
        )
        assert data3.salary_min is None
        assert data3.salary_max is None
        assert data3.salary_currency is None
        assert data3.salary_period is None


class TestJobNormalizationService:
    """Comprehensive tests for the pure JobNormalizationService."""

    def test_json_derived_job_payload_normalization(self) -> None:
        service = JobNormalizationService()
        company_uuid = uuid.uuid4()

        payload = {
            "title": "  Junior AI / Backend Engineer - Python [Hiring Urgently!]  ",
            "company_name": "  Acme Technologies Pvt Ltd  ",
            "company_id": company_uuid,
            "location": "Bengaluru, Karnataka, India",
            "description": "Build high-performance AI services with Python and PostgreSQL.",
            "application_url": "https://boards.greenhouse.io/acme/jobs/12345?utm_source=linkedin",
            "posted_at": "2026-10-24T12:00:00Z",
            "employment_type": "full-time",
            "remote_type": "hybrid",
            "responsibilities": ["• Design REST APIs", "• Build RAG pipelines"],
            "requirements": ["3+ years Python", "PostgreSQL expertise"],
            "skills": ["Python", "FastAPI", "Python", "Docker"],
            "experience_raw": "0-2 years",
            "salary_raw": "6 - 10 LPA",
            "payload_format": "JSON",
        }

        normalized = service.normalize(payload)

        assert isinstance(normalized, NormalizedJobData)
        assert normalized.title == "Junior AI / Backend Engineer - Python [Hiring Urgently!]"
        assert normalized.normalized_title == "junior ai backend engineer python"
        assert normalized.company_name == "Acme Technologies Pvt Ltd"
        assert normalized.company_id == company_uuid
        assert normalized.location == "Bengaluru, Karnataka, India"
        assert normalized.normalized_location == "Bengaluru"
        assert normalized.employment_type == "FULL_TIME"
        assert normalized.remote_type == "HYBRID"
        assert normalized.application_url == "https://boards.greenhouse.io/acme/jobs/12345"
        assert normalized.posted_at == datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
        assert normalized.responsibilities == ["Design REST APIs", "Build RAG pipelines"]
        assert normalized.skills == ["Python", "FastAPI", "Docker"]  # Deduplicated
        assert normalized.experience_min_years == Decimal("0.0")
        assert normalized.experience_max_years == Decimal("2.0")
        assert normalized.salary_min == Decimal("600000.00")
        assert normalized.salary_max == Decimal("1000000.00")
        assert normalized.salary_currency == "INR"
        assert normalized.salary_period == "ANNUAL"

    def test_html_parser_derived_payload_normalization(self) -> None:
        company_uuid = uuid.uuid4()
        extracted = ExtractedJobPayload(
            title="Senior ML Engineer",
            company_name="Apex AI Labs",
            company_id=company_uuid,
            location="Pune, Maharashtra",
            description="<p>We are hiring an ML Engineer.</p><ul><li>Train LLMs</li><li>Fine-tune models</li></ul>",
            application_url="https://apexai.com/careers/ml-101?gh_src=share#apply",
            payload_format="HTML",
        )

        normalized = normalize_job_payload(extracted)

        assert normalized.title == "Senior ML Engineer"
        assert normalized.normalized_title == "senior machine learning engineer"
        assert normalized.company_id == company_uuid
        assert normalized.location == "Pune, Maharashtra"
        assert normalized.normalized_location == "Pune"
        assert "<p>" not in normalized.description
        assert "We are hiring an ML Engineer." in normalized.description
        assert "• Train LLMs" in normalized.description
        assert normalized.application_url == "https://apexai.com/careers/ml-101"

    def test_to_job_kwargs_and_company_resolution(self) -> None:
        """Verifies separation of company identity resolution from normalized job attributes."""
        service = JobNormalizationService()
        company_uuid = uuid.uuid4()

        # Payload with pre-resolved company
        norm_with_comp = service.normalize(
            {
                "title": "AI Engineer",
                "description": "Build agentic models",
                "company_name": "Acme Labs",
                "company_id": company_uuid,
            }
        )
        kwargs = norm_with_comp.to_job_kwargs()
        assert kwargs["company_id"] == company_uuid
        assert kwargs["company_name"] == "Acme Labs"
        assert kwargs["title"] == "AI Engineer"

        # Payload without company info normalizes pure content without failure
        norm_without_comp = service.normalize(
            {
                "title": "AI Engineer",
                "description": "Build agentic models",
            }
        )
        assert norm_without_comp.company_id is None
        assert norm_without_comp.company_name is None

        # Calling to_job_kwargs without resolved company raises clear ValueError
        with pytest.raises(ValueError, match="Cannot construct Job model kwargs without"):
            norm_without_comp.to_job_kwargs()

        # Supplying company resolution to to_job_kwargs succeeds
        kwargs2 = norm_without_comp.to_job_kwargs(
            company_id=company_uuid,
            company_name="Acme Labs",
        )
        assert kwargs2["company_id"] == company_uuid
        assert kwargs2["company_name"] == "Acme Labs"

    def test_missing_optional_fields_retained_as_null(self) -> None:
        """Validates that missing location, publication date, and salary remain NULL without errors."""
        service = JobNormalizationService()

        minimal_payload = {
            "title": "Software Engineer",
            "company_name": "Startup Inc",
            "description": "Exciting early-stage role.",
            "location": None,
            "posted_at": None,
        }

        normalized = service.normalize(minimal_payload)

        assert normalized.title == "Software Engineer"
        assert normalized.normalized_title == "software engineer"
        assert normalized.location is None
        assert normalized.normalized_location is None
        assert normalized.posted_at is None
        assert normalized.application_url is None
        assert normalized.salary_min is None
        assert normalized.salary_max is None
        assert normalized.salary_currency is None
        assert normalized.remote_type == "UNKNOWN"

    def test_malformed_inputs_raise_appropriate_errors(self) -> None:
        service = JobNormalizationService()

        # Missing required title
        with pytest.raises(ValueError, match="Job title is required"):
            service.normalize({"company_name": "Acme", "description": "Some description"})

        # Missing required description
        with pytest.raises(ValueError, match="Job description is required"):
            service.normalize({"title": "Engineer", "company_name": "Acme"})

        # Malformed application URL
        with pytest.raises(ValueError, match="Only HTTP and HTTPS are supported"):
            service.normalize(
                {
                    "title": "Engineer",
                    "company_name": "Acme",
                    "description": "Desc",
                    "application_url": "ftp://bad-url",
                }
            )

        # Malformed publication date
        with pytest.raises(ValueError, match="Unparseable publication date"):
            service.normalize(
                {
                    "title": "Engineer",
                    "company_name": "Acme",
                    "description": "Desc",
                    "posted_at": "not-a-valid-date",
                }
            )

        # Unsupported payload format
        with pytest.raises(ValueError, match="Unsupported payload format"):
            service.normalize(
                {
                    "title": "Engineer",
                    "company_name": "Acme",
                    "description": "Desc",
                    "payload_format": "XML",
                }
            )

    def test_deterministic_output_for_repeated_runs(self) -> None:
        service = JobNormalizationService()
        company_uuid = uuid.uuid4()
        payload = {
            "title": "AI Software Engineer [Immediate Opening]",
            "company_name": "Deep Tech Innovations",
            "company_id": company_uuid,
            "location": "BLR",
            "description": "<p>Build agentic workflows.</p>",
            "application_url": "https://careers.deeptech.ai/jobs/42?utm_source=twitter&b=2&a=1",
            "posted_at": "2026-10-24T00:00:00Z",
            "skills": ["Python", "LangChain", "PostgreSQL"],
            "salary_raw": "5.0 LPA",
            "payload_format": "HTML",
        }

        result1 = service.normalize(payload)
        result2 = service.normalize(payload)

        assert result1.model_dump() == result2.model_dump()
        assert result1.to_job_kwargs() == result2.to_job_kwargs()

    def test_source_raw_payload_is_not_mutated(self) -> None:
        """Guarantees that normalization never mutates input dictionaries or raw payloads."""
        service = JobNormalizationService()

        original_payload = {
            "title": "  Backend Dev  ",
            "company_name": "  Tech Corp  ",
            "location": "Bangalore",
            "description": "<p>Description</p>",
            "application_url": "https://example.com/apply?utm_source=email",
            "skills": ["Python", "FastAPI"],
            "metadata": {"nested_key": "nested_value"},
        }
        # Deep copy for verification
        payload_snapshot = copy.deepcopy(original_payload)

        _ = service.normalize(original_payload)

        # Input dictionary must be 100% identical before and after
        assert original_payload == payload_snapshot
        assert original_payload["title"] == "  Backend Dev  "
        assert original_payload["skills"] == ["Python", "FastAPI"]
