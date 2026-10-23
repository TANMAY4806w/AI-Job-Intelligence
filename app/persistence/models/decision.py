"""Verification, filter decision, and AI analysis result persistence models."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.models.base import Base

if TYPE_CHECKING:
    from app.persistence.models.job import Job


class VerificationRecord(Base):
    """Evaluation outcomes of independent verification dimensions."""

    __tablename__ = "verification_records"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verification_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    source_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    jd_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    application_url_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    company_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    location_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    experience_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    salary_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    posting_date_verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2),
        nullable=False,
    )
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reasons: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    review_reasons: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    audit_trail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Relationships
    job: Mapped["Job"] = relationship(
        "Job",
        back_populates="verification_records",
    )

    __table_args__ = (
        Index("idx_verification_job_time", "job_id", text("verification_run_at DESC")),
        Index("idx_verification_confidence", "verification_confidence"),
    )


class FilterDecision(Base):
    """Immutable audit record documenting deterministic filter rule execution."""

    __tablename__ = "filter_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    final_status: Mapped[str] = mapped_column(String(32), nullable=False)
    role_priority: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_codes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evaluation_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relationships
    job: Mapped["Job"] = relationship(
        "Job",
        back_populates="filter_decisions",
    )

    __table_args__ = (
        Index("idx_filter_decisions_job_time", "job_id", text("evaluated_at DESC")),
        Index(
            "idx_filter_decisions_version_status",
            "rule_version",
            "final_status",
        ),
    )


class AIAnalysisResult(Base):
    """Key-value extraction cache and audit store for semantic LLM operations."""

    __tablename__ = "ai_analysis_results"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    cache_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    structured_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2), nullable=True)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    is_fallback: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relationships
    job: Mapped["Job | None"] = relationship(
        "Job",
        back_populates="ai_analysis_results",
    )

    __table_args__ = (
        Index("idx_ai_cache_key", "cache_key"),
        Index("idx_ai_provider_model", "provider", "model"),
    )
