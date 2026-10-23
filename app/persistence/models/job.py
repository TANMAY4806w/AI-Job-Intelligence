"""Canonical Job persistence model."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.models.base import Base

if TYPE_CHECKING:
    from app.persistence.models.company import Company
    from app.persistence.models.decision import (
        AIAnalysisResult,
        FilterDecision,
        VerificationRecord,
    )
    from app.persistence.models.raw_job import JobSourceReference


class Job(Base):
    """Canonical normalized representation of a unique job vacancy."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("companies.id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    remote_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default=text("'UNKNOWN'"),
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    responsibilities: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    requirements: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    skills: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    experience_raw: Mapped[str | None] = mapped_column(String(128), nullable=True)
    experience_min_years: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 1),
        nullable=True,
    )
    experience_max_years: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 1),
        nullable=True,
    )
    salary_min: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    salary_max: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2),
        nullable=True,
    )
    salary_currency: Mapped[str | None] = mapped_column(
        String(3),
        nullable=True,
        default="INR",
        server_default=text("'INR'"),
    )
    salary_period: Mapped[str | None] = mapped_column(String(16), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )
    application_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="NOT_APPLIED",
        server_default=text("'NOT_APPLIED'"),
    )
    role_priority: Mapped[str] = mapped_column(String(16), nullable=False)
    relevance_status: Mapped[str] = mapped_column(String(16), nullable=False)
    eligibility_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="NEW",
        server_default=text("'NEW'"),
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    jd_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    company_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    location_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    experience_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    salary_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2),
        nullable=False,
    )

    # Relationships
    company: Mapped["Company"] = relationship(
        "Company",
        back_populates="jobs",
    )
    source_references: Mapped[list["JobSourceReference"]] = relationship(
        "JobSourceReference",
        back_populates="job",
    )
    verification_records: Mapped[list["VerificationRecord"]] = relationship(
        "VerificationRecord",
        back_populates="job",
    )
    filter_decisions: Mapped[list["FilterDecision"]] = relationship(
        "FilterDecision",
        back_populates="job",
    )
    ai_analysis_results: Mapped[list["AIAnalysisResult"]] = relationship(
        "AIAnalysisResult",
        back_populates="job",
    )

    __table_args__ = (
        Index("idx_jobs_eligibility_priority", "eligibility_status", "role_priority"),
        Index("idx_jobs_posted_at", text("posted_at DESC NULLS LAST")),
        Index("idx_jobs_company_id", "company_id"),
        Index(
            "idx_jobs_dedup_composite",
            "company_id",
            "normalized_title",
            "normalized_location",
        ),
        Index("idx_jobs_verification", "verification_confidence", "jd_verified"),
    )
