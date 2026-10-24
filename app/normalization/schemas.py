"""Data contracts and schemas for the job normalization layer."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractedJobPayload(BaseModel):
    """Raw or parser-extracted job payload before normalization.

    Boundary: Represents technology-agnostic extracted job attributes captured from
    external ATS JSON APIs or HTML-parser extraction pipelines.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore",
    )

    title: str = Field(..., description="Reported job title")
    description: str = Field(..., description="Job description text or HTML body")
    company_name: str | None = Field(
        default=None,
        description="Hiring company name as reported or inferred from source",
    )
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Foreign key UUID referencing existing Company if known",
    )
    location: str | None = Field(default=None, description="Reported location string")
    application_url: str | None = Field(
        default=None,
        description="Direct application or ATS URL",
    )
    source_url: str | None = Field(
        default=None,
        description="URL where the posting was discovered",
    )
    posted_at: datetime | str | int | float | None = Field(
        default=None,
        description="Reported publication timestamp, date string, or epoch",
    )
    employment_type: str | None = Field(
        default=None,
        description="Reported employment type",
    )
    remote_type: str | None = Field(
        default=None,
        description="Reported work arrangement / remote type",
    )
    responsibilities: list[str] | str | None = Field(
        default=None,
        description="Parsed responsibilities / duties",
    )
    requirements: list[str] | str | None = Field(
        default=None,
        description="Parsed requirements / prerequisites",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Extracted or stated skill tags",
    )
    experience_raw: str | None = Field(
        default=None,
        description="Stated experience string (e.g., '0-2 years', 'Fresher')",
    )
    experience_min_years: Decimal | float | int | str | None = Field(
        default=None,
        description="Extracted minimum experience in years",
    )
    experience_max_years: Decimal | float | int | str | None = Field(
        default=None,
        description="Extracted maximum experience in years",
    )
    salary_min: Decimal | float | int | str | None = Field(
        default=None,
        description="Stated minimum compensation",
    )
    salary_max: Decimal | float | int | str | None = Field(
        default=None,
        description="Stated maximum compensation",
    )
    salary_currency: str | None = Field(
        default=None,
        description="Compensation currency code (ISO 4217)",
    )
    salary_period: str | None = Field(
        default=None,
        description="Compensation cadence: ANNUAL, MONTHLY, HOURLY",
    )
    payload_format: Literal["JSON", "HTML"] = Field(
        default="JSON",
        description="Source payload format (JSON API or HTML page extraction)",
    )
    source_job_id: str | None = Field(
        default=None,
        description="Platform-specific identifier",
    )
    extra_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional unstructured source attributes",
    )


class NormalizedJobData(BaseModel):
    """Deterministic normalized representation of a job vacancy.

    Boundary: Standardized attributes ready for canonical Job persistence and Stage 3
    composite deduplication matching.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    title: str = Field(..., description="Cleaned raw title as advertised")
    normalized_title: str = Field(
        ...,
        description="Standardized lowercase title for matching and classification",
    )
    company_name: str | None = Field(
        default=None,
        description="Cleaned company display name if provided or resolved",
    )
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Canonical Company reference UUID if supplied",
    )
    location: str | None = Field(
        default=None,
        description="Cleaned raw location string; NULL if absent or ambiguous",
    )
    normalized_location: str | None = Field(
        default=None,
        description="Canonical Indian tech hub city (Mumbai, Navi Mumbai, Thane, Pune, Bengaluru, Hyderabad); NULL if outside hubs or unresolvable",
    )
    employment_type: str | None = Field(
        default=None,
        description="Standardized employment type: FULL_TIME, PART_TIME, CONTRACT, INTERNSHIP, UNKNOWN, or NULL",
    )
    remote_type: str = Field(
        default="UNKNOWN",
        description="Work arrangement: ON_SITE, HYBRID, REMOTE, UNKNOWN",
    )
    description: str = Field(
        ...,
        description="Cleaned plain-text job description body",
    )
    responsibilities: list[str] | None = Field(
        default=None,
        description="Cleaned list of duty bullet points",
    )
    requirements: list[str] | None = Field(
        default=None,
        description="Cleaned list of prerequisite bullet points",
    )
    skills: list[str] | None = Field(
        default=None,
        description="Deduplicated list of skills / technologies",
    )
    experience_raw: str | None = Field(
        default=None,
        description="Reported experience string",
    )
    experience_min_years: Decimal | None = Field(
        default=None,
        description="Normalized minimum experience in years",
    )
    experience_max_years: Decimal | None = Field(
        default=None,
        description="Normalized maximum experience in years",
    )
    salary_min: Decimal | None = Field(
        default=None,
        description="Normalized minimum compensation",
    )
    salary_max: Decimal | None = Field(
        default=None,
        description="Normalized maximum compensation",
    )
    salary_currency: str | None = Field(
        default=None,
        description="Currency code (e.g. INR, USD); NULL when salary is unstated",
    )
    salary_period: str | None = Field(
        default=None,
        description="Cadence: ANNUAL, MONTHLY, HOURLY, UNKNOWN",
    )
    posted_at: datetime | None = Field(
        default=None,
        description="Timezone-aware publication timestamp; NULL if unstated or invalid",
    )
    application_url: str | None = Field(
        default=None,
        description="Canonical normalized ATS or application URL",
    )

    def to_job_kwargs(
        self,
        *,
        company_id: uuid.UUID | None = None,
        company_name: str | None = None,
    ) -> dict[str, Any]:
        """Convert normalized attributes into keyword arguments for Job(**kwargs).

        Explicitly separates company identity resolution from job content:
        - company_id: Required by Job schema (foreign key). Must be supplied on self
          or passed explicitly to this method.
        - company_name: Required by Job schema. Must be supplied on self or passed
          explicitly to this method.

        Returns:
            dict[str, Any]: Direct kwargs for Job model instantiation.

        Raises:
            ValueError: If company_id or company_name is unresolved.
        """
        cid = company_id or self.company_id
        if cid is None:
            raise ValueError("Cannot construct Job model kwargs without a resolved company_id")
        cname = company_name or self.company_name
        if not cname:
            raise ValueError("Cannot construct Job model kwargs without a resolved company_name")

        data = self.model_dump()
        data["company_id"] = cid
        data["company_name"] = cname
        return data
