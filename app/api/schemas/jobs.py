"""Pydantic data schemas for Job Feed, Review Queue, and Audit APIs."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JobSourceSummarySchema(BaseModel):
    """ATS source provenance reference for a job vacancy."""

    model_config = ConfigDict(from_attributes=True)

    source_name: str
    source_type: str
    source_job_id: str
    source_url: str
    application_url: str | None = None
    is_primary: bool = False


class FilterDecisionSchema(BaseModel):
    """Immutable audit entry for filter or human review decisions."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule_version: str
    final_status: str
    role_priority: str
    reason_codes: list[str] = Field(default_factory=list)
    evaluation_context: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime


class JobVerificationSummarySchema(BaseModel):
    """7-dimension verification status and confidence score."""

    model_config = ConfigDict(from_attributes=True)

    source_verified: bool
    jd_verified: bool
    application_url_verified: bool = False
    company_verified: bool
    location_verified: bool
    experience_verified: bool
    salary_verified: bool
    posting_date_verified: bool = False
    verification_confidence: float


class JobSummarySchema(BaseModel):
    """Compact canonical job summary for feeds, tables, and card listings."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    company_name: str
    title: str
    normalized_title: str
    location: str | None = None
    normalized_location: str | None = None
    employment_type: str | None = None
    remote_type: str
    role_priority: str
    relevance_status: str
    eligibility_status: str
    rejection_reason: str | None = None
    review_reason: str | None = None
    human_review_status: str = "NOT_REQUIRED"
    application_status: str = "NOT_APPLIED"
    application_url: str | None = None
    experience_min_years: float | None = None
    experience_max_years: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    posted_at: datetime | None = None
    collected_at: datetime
    verification_confidence: float
    primary_source: str | None = None


class JobDetailSchema(JobSummarySchema):
    """Comprehensive canonical job view with full description, 8 dimensions, and audit log."""

    description: str
    responsibilities: list[str] | list[Any] | None = None
    requirements: list[str] | list[Any] | None = None
    skills: list[str] | list[Any] | None = None
    experience_raw: str | None = None

    source_verified: bool
    jd_verified: bool
    application_url_verified: bool
    company_verified: bool
    location_verified: bool
    experience_verified: bool
    salary_verified: bool
    posting_date_verified: bool

    sources: list[JobSourceSummarySchema] = Field(default_factory=list)
    filter_decisions: list[FilterDecisionSchema] = Field(default_factory=list)


class PaginatedResponse[T](BaseModel):
    """Standard pagination envelope for list endpoints."""

    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class JobFilterFacetsSchema(BaseModel):
    """Dynamic facet counts and distribution statistics computed from database."""

    total_jobs: int
    total_eligible: int
    total_review: int
    total_rejected: int
    total_dismissed: int
    eligibility_counts: dict[str, int]
    priority_counts: dict[str, int]
    remote_type_counts: dict[str, int]
    provider_counts: dict[str, int]
    top_locations: list[dict[str, Any]]


class ReviewActionRequest(BaseModel):
    """Payload for human reviewer decisions on jobs in review."""

    action: Literal["APPROVE", "REJECT"]
    notes: str | None = Field(default=None, max_length=1000)
    reviewer: str = Field(default="human_reviewer", min_length=1, max_length=100)


class ReviewActionResponse(BaseModel):
    """Outcome confirmation of a human review decision."""

    job_id: uuid.UUID
    human_review_status: str
    application_status: str
    eligibility_status: str
    message: str
    audit_decision_id: uuid.UUID


class DismissActionRequest(BaseModel):
    """Payload for user dismiss actions."""

    reason: str | None = Field(default="Dismissed by user", max_length=500)
    reviewer: str = Field(default="human_reviewer", min_length=1, max_length=100)


class DismissActionResponse(BaseModel):
    """Outcome confirmation of a job dismissal."""

    job_id: uuid.UUID
    human_review_status: str
    application_status: str
    message: str
    audit_decision_id: uuid.UUID


class ApplicationStatusUpdateRequest(BaseModel):
    """Payload for updating candidate application workflow tracking status."""

    application_status: Literal[
        "NOT_APPLIED",
        "SAVED",
        "APPLIED",
        "INTERVIEW",
        "OFFER",
        "REJECTED_BY_COMPANY",
        "WITHDRAWN",
    ]
    notes: str | None = Field(default=None, max_length=1000)


class ApplicationStatusUpdateResponse(BaseModel):
    """Outcome confirmation of candidate application status update."""

    job_id: uuid.UUID
    application_status: str
    message: str
