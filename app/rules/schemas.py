"""Data contracts and schemas for the deterministic job filtering engine.

Boundary: 100% deterministic, in-memory domain models.
Defines typed inputs and auditable evaluation results for rule execution
without database, network, or external dependencies.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Role Priority Tiers per JOB_FILTERS_V1.md and V1_SPECIFICATION.md
RolePriority = Literal["P1", "P2", "P3", "NOT_RELEVANT"]

# Relevance Alignment Status per DATABASE_SCHEMA_V1.md and JOB_DATA_CONTRACT.md
RelevanceStatus = Literal["TARGET", "ADJACENT", "EXCLUDED"]

# Operational Eligibility Status per DATABASE_SCHEMA_V1.md and JOB_FILTERS_V1.md
EligibilityStatus = Literal["ELIGIBLE", "REVIEW", "REJECTED", "EXPIRED"]

# Machine-readable Rejection Reason Codes per JOB_FILTERS_V1.md Section 9
REJECTION_LOCATION_NOT_ALLOWED = "LOCATION_NOT_ALLOWED"
REJECTION_EXPERIENCE_TOO_HIGH = "EXPERIENCE_TOO_HIGH"
REJECTION_EXCLUDED_ROLE = "EXCLUDED_ROLE"
REJECTION_JOB_TOO_OLD = "JOB_TOO_OLD"
REJECTION_SALARY_BELOW_MINIMUM = "SALARY_BELOW_MINIMUM"

# Machine-readable Review Reason Codes per JOB_FILTERS_V1.md Section 9 and VERIFICATION_V1.md
REVIEW_EXPERIENCE_AMBIGUOUS = "EXPERIENCE_AMBIGUOUS"
REVIEW_LOCATION_REMOTE_REVIEW = "LOCATION_REMOTE_REVIEW"
REVIEW_LOCATION_AMBIGUOUS = "LOCATION_AMBIGUOUS"
REVIEW_SALARY_UNKNOWN = "SALARY_UNKNOWN"
REVIEW_EXCLUSION_AMBIGUOUS = "EXCLUSION_AMBIGUOUS"
REVIEW_POSTED_DATE_SUSPICIOUS = "POSTED_DATE_SUSPICIOUS"
REVIEW_SOURCE_CONFLICT_REVIEW = "SOURCE_CONFLICT_REVIEW"
REVIEW_COMPANY_UNVERIFIED = "COMPANY_UNVERIFIED"

# Default rule engine version
DEFAULT_RULE_VERSION = "V1.0"


class JobFilterInput(BaseModel):
    """Normalized in-memory job attributes presented for deterministic filtering.

    Boundary: Technology-agnostic, database-independent typed input structure.
    Can be constructed directly or generated from NormalizedJobData.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore",
    )

    title: str = Field(..., description="Reported job title")
    normalized_title: str | None = Field(
        default=None,
        description="Standardized lowercase title if pre-computed by normalization layer",
    )
    description: str = Field(
        default="",
        description="Job description plain text or body",
    )
    company_name: str | None = Field(
        default=None,
        description="Hiring company name",
    )
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Company UUID if resolved",
    )
    location: str | None = Field(
        default=None,
        description="Reported raw location string",
    )
    normalized_location: str | None = Field(
        default=None,
        description="Canonical Indian tech hub city (Mumbai, Navi Mumbai, Thane, Pune, Bengaluru, Hyderabad)",
    )
    employment_type: str | None = Field(
        default=None,
        description="Standardized employment type (e.g. FULL_TIME, CONTRACT)",
    )
    remote_type: str = Field(
        default="UNKNOWN",
        description="Work arrangement: ON_SITE, HYBRID, REMOTE, UNKNOWN",
    )
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
    salary_min: Decimal | None = Field(
        default=None,
        description="Extracted minimum salary",
    )
    salary_max: Decimal | None = Field(
        default=None,
        description="Extracted maximum salary",
    )
    salary_currency: str | None = Field(
        default="INR",
        description="Salary currency code (default: INR)",
    )
    salary_period: str | None = Field(
        default=None,
        description="Salary pay cadence: ANNUAL, MONTHLY, HOURLY, UNKNOWN",
    )
    posted_at: datetime | None = Field(
        default=None,
        description="Reported publication timestamp; NULL if unknown",
    )
    collected_at: datetime | None = Field(
        default=None,
        description="Ingestion/discovery timestamp; metadata only",
    )
    posting_date_verified: bool | None = Field(
        default=None,
        description="Whether publication date authenticity is verified (True), failed (False), or unresolved (None)",
    )
    extra_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary additional context",
    )

    @classmethod
    def from_normalized_job(
        cls,
        normalized: Any,
        *,
        collected_at: datetime | None = None,
        posting_date_verified: bool | None = None,
    ) -> "JobFilterInput":
        """Factory to build JobFilterInput from NormalizedJobData or equivalent schema."""
        return cls(
            title=getattr(normalized, "title", ""),
            normalized_title=getattr(normalized, "normalized_title", None),
            description=getattr(normalized, "description", ""),
            company_name=getattr(normalized, "company_name", None),
            company_id=getattr(normalized, "company_id", None),
            location=getattr(normalized, "location", None),
            normalized_location=getattr(normalized, "normalized_location", None),
            employment_type=getattr(normalized, "employment_type", None),
            remote_type=getattr(normalized, "remote_type", "UNKNOWN"),
            experience_raw=getattr(normalized, "experience_raw", None),
            experience_min_years=getattr(normalized, "experience_min_years", None),
            experience_max_years=getattr(normalized, "experience_max_years", None),
            salary_min=getattr(normalized, "salary_min", None),
            salary_max=getattr(normalized, "salary_max", None),
            salary_currency=getattr(normalized, "salary_currency", "INR"),
            salary_period=getattr(normalized, "salary_period", None),
            posted_at=getattr(normalized, "posted_at", None),
            collected_at=collected_at,
            posting_date_verified=posting_date_verified,
        )


class JobFilterResult(BaseModel):
    """Deterministic filtering outcome and immutable audit record.

    Boundary: Output contract satisfying both FilterDecision persistence
    and canonical Job status updates.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    eligibility_status: EligibilityStatus = Field(
        ...,
        description="Operational eligibility outcome: ELIGIBLE, REVIEW, REJECTED, EXPIRED",
    )
    role_priority: RolePriority = Field(
        ...,
        description="Assigned role priority tier: P1, P2, P3, NOT_RELEVANT",
    )
    relevance_status: RelevanceStatus = Field(
        ...,
        description="High-level match alignment: TARGET, ADJACENT, EXCLUDED",
    )
    rejection_reason: str | None = Field(
        default=None,
        description="Primary machine-readable rejection reason code if REJECTED or EXPIRED",
    )
    review_reason: str | None = Field(
        default=None,
        description="Primary machine-readable review reason code if REVIEW",
    )
    reason_codes: list[str] = Field(
        default_factory=list,
        description="Complete ordered list of machine-readable reason codes triggered",
    )
    passed: bool = Field(
        ...,
        description="True if job is ELIGIBLE; False if REVIEW, REJECTED, or EXPIRED",
    )
    rule_version: str = Field(
        default=DEFAULT_RULE_VERSION,
        description="Rule set identifier for audit trail",
    )
    explanation: str = Field(
        ...,
        description="Human-readable decision explanation",
    )
    evaluation_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value snapshot of evaluated inputs for FilterDecision.evaluation_context",
    )
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when evaluation was executed",
    )

    def to_filter_decision_kwargs(self, job_id: uuid.UUID) -> dict[str, Any]:
        """Convert result into kwargs for FilterDecision model instantiation."""
        return {
            "job_id": job_id,
            "rule_version": self.rule_version,
            "final_status": self.eligibility_status,
            "role_priority": self.role_priority,
            "reason_codes": self.reason_codes,
            "evaluation_context": self.evaluation_context,
            "evaluated_at": self.evaluated_at,
        }

    def to_job_status_updates(self) -> dict[str, Any]:
        """Convert result into dictionary of fields for canonical Job status updates."""
        return {
            "role_priority": self.role_priority,
            "relevance_status": self.relevance_status,
            "eligibility_status": self.eligibility_status,
            "rejection_reason": self.rejection_reason,
            "review_reason": self.review_reason,
        }
