"""Typed contracts, reason codes, and schemas for verification evaluation.

Boundary: 100% pure domain models and evaluation contracts.
Follows the eight verification dimensions defined in rules/VERIFICATION_V1.md.
Independent of SQLAlchemy, persistence repositories, network I/O, scraping, and LLMs.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Machine-readable Failure Reason Codes (Hard Technical Failures)
# ---------------------------------------------------------------------------
FAILURE_SOURCE_INVALID = "SOURCE_INVALID"
FAILURE_JD_NOT_FOUND = "JD_NOT_FOUND"
FAILURE_JD_EXPIRED = "JD_EXPIRED"

# ---------------------------------------------------------------------------
# Machine-readable Review Reason Codes (Information Ambiguity / Discrepancy)
# ---------------------------------------------------------------------------
REVIEW_APPLICATION_LINK_BROKEN = "APPLICATION_LINK_BROKEN"
REVIEW_COMPANY_UNVERIFIED = "COMPANY_UNVERIFIED"
REVIEW_LOCATION_REMOTE_REVIEW = "LOCATION_REMOTE_REVIEW"
REVIEW_LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"
REVIEW_EXPERIENCE_AMBIGUOUS = "EXPERIENCE_AMBIGUOUS"
REVIEW_SALARY_UNKNOWN = "SALARY_UNKNOWN"
REVIEW_POSTED_DATE_SUSPICIOUS = "POSTED_DATE_SUSPICIOUS"
REVIEW_SOURCE_CONFLICT_REVIEW = "SOURCE_CONFLICT_REVIEW"

# ---------------------------------------------------------------------------
# Verification Confidence Tiers
# ---------------------------------------------------------------------------
ConfidenceTier = Literal["HIGH", "MODERATE", "LOW"]


@dataclass(frozen=True)
class DimensionResult:
    """Evaluation result for an individual verification dimension."""

    dimension_name: str
    verified: bool
    weight: Decimal
    score: Decimal
    failure_reasons: list[str]
    review_reasons: list[str]
    details: dict[str, Any]


class JobVerificationInput(BaseModel):
    """Normalized evidence presented to the deterministic verification evaluator.

    Represents explicit observations from ingestion, normalization, or checkers.
    Never fabricates findings; missing fields remain unverified or trigger review.
    """

    model_config = ConfigDict(frozen=True)

    job_id: uuid.UUID | None = Field(
        default=None,
        description="Optional canonical Job UUID being verified",
    )

    # Dimension 1: Source Validity
    source_id: uuid.UUID | None = Field(
        default=None,
        description="Origin source platform identifier",
    )
    source_name: str | None = Field(
        default=None,
        description="Origin source platform name (e.g., 'greenhouse', 'lever', 'ashby')",
    )
    source_type: str | None = Field(
        default=None,
        description="Source classification type (e.g., 'ATS', 'DIRECT', 'JOB_BOARD')",
    )
    is_source_approved: bool | None = Field(
        default=None,
        description="Explicit registry confirmation of source legitimacy",
    )
    payload_format_valid: bool | None = Field(
        default=None,
        description="Whether payload wire protocol matched expected schema",
    )

    # Dimension 2: Actual JD Existence
    description: str | None = Field(
        default=None,
        description="Full job description body text",
    )
    http_status_code: int | None = Field(
        default=None,
        description="HTTP status code from source page fetch",
    )
    is_page_active: bool | None = Field(
        default=None,
        description="Whether the job page is currently active (not 404/expired/tombstone)",
    )
    is_tombstone: bool = Field(
        default=False,
        description="Whether page indicates posting has been closed/removed",
    )
    is_expired: bool = Field(
        default=False,
        description="Whether page explicitly marks job expired",
    )

    # Dimension 3: Application URL Validity
    application_url: str | None = Field(
        default=None,
        description="Direct application or ATS URL endpoint",
    )
    application_url_status: int | None = Field(
        default=None,
        description="HTTP status returned when probing application URL",
    )
    application_url_reachable: bool | None = Field(
        default=None,
        description="Explicit reachability observation for application URL",
    )
    source_url: str | None = Field(
        default=None,
        description="Fallback source discovery URL",
    )
    source_url_status: int | None = Field(
        default=None,
        description="HTTP status returned when probing fallback source URL",
    )
    source_url_reachable: bool | None = Field(
        default=None,
        description="Explicit reachability observation for fallback source URL",
    )

    # Dimension 4: Company Legitimacy
    company_name: str | None = Field(
        default=None,
        description="Hiring company display name",
    )
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Company entity ID in database registry",
    )
    is_company_registry_verified: bool | None = Field(
        default=None,
        description="Whether hiring company exists in verified company registry",
    )
    company_domain: str | None = Field(
        default=None,
        description="Hiring company official domain name",
    )
    has_verifiable_domain: bool | None = Field(
        default=None,
        description="Whether company domain is verified and authentic",
    )

    # Dimension 5: Location Consistency
    location: str | None = Field(
        default=None,
        description="Reported raw location string",
    )
    normalized_location: str | None = Field(
        default=None,
        description="Standardized canonical city/tier string",
    )
    remote_type: str = Field(
        default="UNKNOWN",
        description="Work arrangement: ON_SITE, HYBRID, REMOTE, UNKNOWN",
    )
    company_location: str | None = Field(
        default=None,
        description="Company headquarters or primary operational base",
    )

    # Dimension 6: Experience Consistency
    experience_raw: str | None = Field(
        default=None,
        description="Stated raw experience text (e.g., '0-2 years', 'Fresher')",
    )
    experience_min_years: Decimal | None = Field(
        default=None,
        description="Extracted minimum required experience in years",
    )
    experience_max_years: Decimal | None = Field(
        default=None,
        description="Extracted maximum required experience in years",
    )
    is_senior_lead_title: bool = Field(
        default=False,
        description="Whether title explicitly specifies Senior, Lead, Principal, etc.",
    )

    # Dimension 7: Salary Statedness
    salary_min: Decimal | None = Field(
        default=None,
        description="Disclosed minimum compensation value",
    )
    salary_max: Decimal | None = Field(
        default=None,
        description="Disclosed maximum compensation value",
    )
    salary_currency: str | None = Field(
        default="INR",
        description="Salary currency code (default: INR)",
    )
    salary_period: str | None = Field(
        default=None,
        description="Compensation cadence: ANNUAL, MONTHLY, HOURLY, UNKNOWN",
    )

    # Dimension 8: Posting Date Plausibility & Authenticity
    posted_at: datetime | None = Field(
        default=None,
        description="Reported original publication timestamp; NULL if unknown",
    )
    collected_at: datetime | None = Field(
        default=None,
        description="Ingestion/discovery timestamp; metadata only",
    )
    publication_date_corroborated: bool | None = Field(
        default=None,
        description="Explicit external verification/publisher corroboration of publication timestamp",
    )
    posting_date_verified: bool | None = Field(
        default=None,
        description="Explicit caller assertion of publication date authenticity",
    )

    # Cross-Source Consistency
    has_source_conflicts: bool = Field(
        default=False,
        description="Whether multiple sources report conflicting material attributes",
    )
    source_conflicts: list[str] = Field(
        default_factory=list,
        description="List of specific conflicting attributes (e.g. ['salary', 'experience'])",
    )

    @classmethod
    def from_job(
        cls,
        job: Any,
        *,
        source_id: uuid.UUID | None = None,
        source_name: str | None = None,
        source_type: str | None = None,
        is_source_approved: bool | None = None,
        payload_format_valid: bool | None = None,
        http_status_code: int | None = None,
        is_page_active: bool | None = None,
        is_tombstone: bool = False,
        is_expired: bool = False,
        application_url_status: int | None = None,
        application_url_reachable: bool | None = None,
        source_url: str | None = None,
        source_url_status: int | None = None,
        source_url_reachable: bool | None = None,
        is_company_registry_verified: bool | None = None,
        company_domain: str | None = None,
        has_verifiable_domain: bool | None = None,
        company_location: str | None = None,
        is_senior_lead_title: bool = False,
        publication_date_corroborated: bool | None = None,
        posting_date_verified: bool | None = None,
        has_source_conflicts: bool = False,
        source_conflicts: list[str] | None = None,
    ) -> "JobVerificationInput":
        """Factory to build JobVerificationInput from a canonical Job persistence entity and explicit observations."""
        return cls(
            job_id=getattr(job, "id", None),
            # Dimension 1
            source_id=source_id,
            source_name=source_name,
            source_type=source_type,
            is_source_approved=is_source_approved,
            payload_format_valid=payload_format_valid,
            # Dimension 2
            description=getattr(job, "description", None),
            http_status_code=http_status_code,
            is_page_active=is_page_active,
            is_tombstone=is_tombstone,
            is_expired=is_expired,
            # Dimension 3
            application_url=getattr(job, "application_url", None),
            application_url_status=application_url_status,
            application_url_reachable=application_url_reachable,
            source_url=source_url,
            source_url_status=source_url_status,
            source_url_reachable=source_url_reachable,
            # Dimension 4
            company_name=getattr(job, "company_name", None),
            company_id=getattr(job, "company_id", None),
            is_company_registry_verified=is_company_registry_verified,
            company_domain=company_domain,
            has_verifiable_domain=has_verifiable_domain,
            # Dimension 5
            location=getattr(job, "location", None),
            normalized_location=getattr(job, "normalized_location", None),
            remote_type=getattr(job, "remote_type", "UNKNOWN") or "UNKNOWN",
            company_location=company_location,
            # Dimension 6
            experience_raw=getattr(job, "experience_raw", None),
            experience_min_years=getattr(job, "experience_min_years", None),
            experience_max_years=getattr(job, "experience_max_years", None),
            is_senior_lead_title=is_senior_lead_title,
            # Dimension 7
            salary_min=getattr(job, "salary_min", None),
            salary_max=getattr(job, "salary_max", None),
            salary_currency=getattr(job, "salary_currency", "INR") or "INR",
            salary_period=getattr(job, "salary_period", None),
            # Dimension 8
            posted_at=getattr(job, "posted_at", None),
            collected_at=getattr(job, "collected_at", None),
            publication_date_corroborated=publication_date_corroborated,
            posting_date_verified=posting_date_verified,
            # Conflicts
            has_source_conflicts=has_source_conflicts,
            source_conflicts=source_conflicts or [],
        )

    @classmethod
    def from_normalized_job(
        cls,
        normalized: Any,
        *,
        job_id: uuid.UUID | None = None,
        source_id: uuid.UUID | None = None,
        source_name: str | None = None,
        source_type: str | None = None,
        is_source_approved: bool | None = None,
        payload_format_valid: bool | None = None,
        http_status_code: int | None = None,
        is_page_active: bool | None = None,
        is_tombstone: bool = False,
        is_expired: bool = False,
        application_url: str | None = None,
        application_url_status: int | None = None,
        application_url_reachable: bool | None = None,
        source_url: str | None = None,
        source_url_status: int | None = None,
        source_url_reachable: bool | None = None,
        is_company_registry_verified: bool | None = None,
        company_domain: str | None = None,
        has_verifiable_domain: bool | None = None,
        company_location: str | None = None,
        is_senior_lead_title: bool = False,
        collected_at: datetime | None = None,
        publication_date_corroborated: bool | None = None,
        posting_date_verified: bool | None = None,
        has_source_conflicts: bool = False,
        source_conflicts: list[str] | None = None,
    ) -> "JobVerificationInput":
        """Factory to build JobVerificationInput from NormalizedJobData and explicit observations."""
        return cls(
            job_id=job_id,
            source_id=source_id,
            source_name=source_name,
            source_type=source_type,
            is_source_approved=is_source_approved,
            payload_format_valid=payload_format_valid,
            description=getattr(normalized, "description", None),
            http_status_code=http_status_code,
            is_page_active=is_page_active,
            is_tombstone=is_tombstone,
            is_expired=is_expired,
            application_url=application_url or getattr(normalized, "application_url", None),
            application_url_status=application_url_status,
            application_url_reachable=application_url_reachable,
            source_url=source_url,
            source_url_status=source_url_status,
            source_url_reachable=source_url_reachable,
            company_name=getattr(normalized, "company_name", None),
            company_id=getattr(normalized, "company_id", None),
            is_company_registry_verified=is_company_registry_verified,
            company_domain=company_domain,
            has_verifiable_domain=has_verifiable_domain,
            location=getattr(normalized, "location", None),
            normalized_location=getattr(normalized, "normalized_location", None),
            remote_type=getattr(normalized, "remote_type", "UNKNOWN") or "UNKNOWN",
            company_location=company_location,
            experience_raw=getattr(normalized, "experience_raw", None),
            experience_min_years=getattr(normalized, "experience_min_years", None),
            experience_max_years=getattr(normalized, "experience_max_years", None),
            is_senior_lead_title=is_senior_lead_title,
            salary_min=getattr(normalized, "salary_min", None),
            salary_max=getattr(normalized, "salary_max", None),
            salary_currency=getattr(normalized, "salary_currency", "INR") or "INR",
            salary_period=getattr(normalized, "salary_period", None),
            posted_at=getattr(normalized, "posted_at", None),
            collected_at=collected_at,
            publication_date_corroborated=publication_date_corroborated,
            posting_date_verified=posting_date_verified,
            has_source_conflicts=has_source_conflicts,
            source_conflicts=source_conflicts or [],
        )


class JobVerificationResult(BaseModel):
    """Immutable evaluation result returned by the pure verification engine.

    Represents all eight dimensions, composite confidence metric, overall passed
    status, reason codes, and diagnostic audit trail.
    """

    model_config = ConfigDict(frozen=True)

    # Dimension booleans (matching VerificationRecord schema)
    source_verified: bool = Field(description="Platform legitimacy check outcome")
    jd_verified: bool = Field(description="Actual JD existence check outcome")
    application_url_verified: bool = Field(description="Application URL reachability outcome")
    company_verified: bool = Field(description="Hiring company entity legitimacy outcome")
    location_verified: bool = Field(description="Location consistency outcome")
    experience_verified: bool = Field(description="Experience target band consistency outcome")
    salary_verified: bool = Field(description="Salary statedness outcome")
    posting_date_verified: bool = Field(
        description="Publication date plausibility and authenticity outcome"
    )

    # Aggregate metrics
    verification_confidence: Decimal = Field(
        description="Weighted composite confidence score in range [0.00, 1.00]",
    )
    confidence_tier: ConfidenceTier = Field(
        description="Confidence tier: HIGH (>=0.85), MODERATE (0.60-0.84), LOW (<0.60)",
    )
    passed: bool = Field(
        description="True if critical dimensions (JD authenticity and source validity) passed",
    )
    failure_reasons: list[str] | None = Field(
        default=None,
        description="Hard technical failure codes (e.g. ['SOURCE_INVALID', 'JD_NOT_FOUND'])",
    )
    review_reasons: list[str] | None = Field(
        default=None,
        description="Ambiguity / discrepancy review codes (e.g. ['SALARY_UNKNOWN', 'EXPERIENCE_AMBIGUOUS'])",
    )

    # Per-dimension audit breakdown
    dimensions: dict[str, DimensionResult] = Field(
        default_factory=dict,
        description="Per-dimension detailed evaluation results",
    )
    audit_trail: dict[str, Any] = Field(
        default_factory=dict,
        description="Machine-readable diagnostic audit payload for VerificationRecord.audit_trail",
    )

    def to_verification_record_kwargs(
        self,
        job_id: uuid.UUID,
        verification_run_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Convert result into keyword arguments suitable for instantiating a VerificationRecord ORM model."""
        return {
            "job_id": job_id,
            "verification_run_at": verification_run_at or datetime.now(UTC),
            "source_verified": self.source_verified,
            "jd_verified": self.jd_verified,
            "application_url_verified": self.application_url_verified,
            "company_verified": self.company_verified,
            "location_verified": self.location_verified,
            "experience_verified": self.experience_verified,
            "salary_verified": self.salary_verified,
            "posting_date_verified": self.posting_date_verified,
            "verification_confidence": self.verification_confidence,
            "passed": self.passed,
            "failure_reasons": self.failure_reasons,
            "review_reasons": self.review_reasons,
            "audit_trail": self.audit_trail,
        }

    def to_job_verification_updates(self) -> dict[str, Any]:
        """Convert result into keyword updates for the canonical Job verification summary columns."""
        return {
            "source_verified": self.source_verified,
            "jd_verified": self.jd_verified,
            "company_verified": self.company_verified,
            "location_verified": self.location_verified,
            "experience_verified": self.experience_verified,
            "salary_verified": self.salary_verified,
            "verification_confidence": self.verification_confidence,
        }
