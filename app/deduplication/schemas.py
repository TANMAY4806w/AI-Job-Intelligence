"""Data contracts and schemas for the job deduplication service.

Boundary: Strongly typed models for deduplication candidates, actions, stages, and
audit trail results. Decoupled from transport and storage layers.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.normalization.schemas import NormalizedJobData


class DeduplicationAction(StrEnum):
    """Outcome of evaluating an incoming job candidate against existing canonical jobs."""

    MATCH = "MATCH"
    AMBIGUOUS = "AMBIGUOUS"
    NEW = "NEW"


class DeduplicationStage(StrEnum):
    """The deduplication stage at which resolution was reached."""

    STAGE_1_EXACT_SOURCE_ID = "STAGE_1_EXACT_SOURCE_ID"
    STAGE_2_EXACT_URL = "STAGE_2_EXACT_URL"
    STAGE_3_COMPOSITE = "STAGE_3_COMPOSITE"
    NONE = "NONE"


class DeduplicationCandidate(BaseModel):
    """An incoming normalized job posting undergoing duplicate resolution."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    source_id: uuid.UUID = Field(
        ...,
        description="UUID of the platform source (e.g. Greenhouse, Lever, LinkedIn)",
    )
    source_job_id: str = Field(
        ...,
        description=(
            "External job ID on the source platform or deterministic 'gen_<sha256>' convention. "
            "The 'gen_' prefix is a pipeline convention to distinguish synthetic hashes from native platform keys."
        ),
    )
    source_job_id_type: str = Field(
        default="NATIVE",
        description=(
            "Identifier classification: NATIVE for platform-assigned IDs, GENERATED for synthetic URL/body hashes. "
            "Stage 1 matching operates on (source_id, source_job_id) under the operational convention "
            "that native keys and synthetic prefixes do not collide."
        ),
    )
    source_url: str = Field(
        ...,
        description="URL where the posting was discovered",
    )
    application_url: str | None = Field(
        default=None,
        description="Direct application or ATS URL",
    )
    company_id: uuid.UUID = Field(
        ...,
        description="Foreign key UUID referencing the canonical Company",
    )
    company_name: str = Field(
        ...,
        description="Cleaned company display name",
    )
    title: str = Field(
        ...,
        description="Reported raw job title",
    )
    normalized_title: str = Field(
        ...,
        description="Standardized lowercase title used for Stage 3 composite matching",
    )
    location: str | None = Field(
        default=None,
        description="Reported raw location string",
    )
    normalized_location: str | None = Field(
        default=None,
        description="Canonical Indian tech hub city; NULL if outside hubs or unresolvable",
    )
    posted_at: datetime | None = Field(
        default=None,
        description="Publication timestamp; NULL if unstated or invalid",
    )
    raw_job_id: uuid.UUID | None = Field(
        default=None,
        description="UUID referencing the persistent RawJob record if available",
    )

    @classmethod
    def from_normalized(
        cls,
        normalized: "NormalizedJobData",
        *,
        source_id: uuid.UUID,
        source_job_id: str,
        source_url: str,
        company_id: uuid.UUID,
        raw_job_id: uuid.UUID | None = None,
        source_job_id_type: str = "NATIVE",
        application_url: str | None = None,
    ) -> "DeduplicationCandidate":
        """Factory creating a candidate from NormalizedJobData and ingestion context."""
        return cls(
            source_id=source_id,
            source_job_id=source_job_id,
            source_job_id_type=source_job_id_type,
            source_url=source_url,
            application_url=application_url or normalized.application_url,
            company_id=company_id,
            company_name=normalized.company_name or "Unknown Company",
            title=normalized.title,
            normalized_title=normalized.normalized_title,
            location=normalized.location,
            normalized_location=normalized.normalized_location,
            posted_at=normalized.posted_at,
            raw_job_id=raw_job_id,
        )


class DeduplicationResult(BaseModel):
    """The decision and audit trail resulting from multi-stage deduplication evaluation."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    action: DeduplicationAction = Field(
        ...,
        description="Resolution action: MATCH, AMBIGUOUS, or NEW",
    )
    stage: DeduplicationStage = Field(
        ...,
        description="Deduplication stage where the decisive outcome occurred",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score: 1.0 (Stage 1), 0.98 (Stage 2), 0.90 (Stage 3), 0.0 (NEW)",
    )
    canonical_job_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of the matched canonical Job if action is MATCH",
    )
    matched_job: Any | None = Field(
        default=None,
        description="The matched canonical Job entity if available",
    )
    candidate_job_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="UUIDs of candidate canonical jobs evaluated or conflicting",
    )
    review_reason: str | None = Field(
        default=None,
        description="Reason code if action is AMBIGUOUS (e.g. DUPLICATE_AMBIGUOUS)",
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured machine-readable audit trail of all evaluated stages",
    )

    @property
    def is_duplicate(self) -> bool:
        """True if the candidate decisively matched an existing canonical job."""
        return self.action == DeduplicationAction.MATCH

    @property
    def is_ambiguous(self) -> bool:
        """True if the candidate encountered conflicting or insufficient evidence."""
        return self.action == DeduplicationAction.AMBIGUOUS

    @property
    def is_new(self) -> bool:
        """True if the candidate does not match any existing job and should be created as new."""
        return self.action == DeduplicationAction.NEW
