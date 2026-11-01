"""Pydantic data schemas for Company and Source endpoints."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class CompanySourceSummarySchema(BaseModel):
    """Configured ATS integration for a company."""

    model_config = ConfigDict(from_attributes=True)

    source_name: str
    source_type: str
    ats_identifier: str | None = None
    career_url: str | None = None
    is_active: bool = True


class CompanySummarySchema(BaseModel):
    """Company profile summary including ATS sources and active vacancy count."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    normalized_name: str
    website: str | None = None
    industry: str | None = None
    sector: str
    is_verified: bool
    confidence: float
    sources: list[CompanySourceSummarySchema] = Field(default_factory=list)
    active_jobs_count: int = 0
