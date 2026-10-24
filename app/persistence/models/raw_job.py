"""Raw job and job source reference persistence models."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.models.base import Base

if TYPE_CHECKING:
    from app.persistence.models.job import Job
    from app.persistence.models.run import CollectionRun
    from app.persistence.models.source import CompanySource, Source


class RawJob(Base):
    """Immutable append-only store preserving external payloads exactly as ingested."""

    __tablename__ = "raw_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    company_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("company_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    collection_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("collection_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_job_id_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="NATIVE",
        server_default=text("'NATIVE'"),
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    payload_format: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="JSON",
        server_default=text("'JSON'"),
    )
    raw_body: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Relationships
    company_source: Mapped["CompanySource"] = relationship(
        "CompanySource",
        back_populates="raw_jobs",
    )
    collection_run: Mapped["CollectionRun | None"] = relationship(
        "CollectionRun",
        back_populates="raw_jobs",
    )
    job_source_references: Mapped[list["JobSourceReference"]] = relationship(
        "JobSourceReference",
        back_populates="raw_job",
    )

    __table_args__ = (
        Index("idx_raw_jobs_comp_src_ext", "company_source_id", "source_job_id"),
        Index("idx_raw_jobs_content_hash", "content_hash"),
        Index("idx_raw_jobs_collected_at", "collected_at"),
        Index("idx_raw_jobs_run", "collection_run_id"),
    )


class JobSourceReference(Base):
    """Maps canonical jobs to multi-source platform ingestion events."""

    __tablename__ = "job_source_references"

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
    raw_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("raw_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_job_id_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="NATIVE",
        server_default=text("'NATIVE'"),
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    application_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
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
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # Relationships
    job: Mapped["Job"] = relationship(
        "Job",
        back_populates="source_references",
    )
    raw_job: Mapped["RawJob"] = relationship(
        "RawJob",
        back_populates="job_source_references",
    )
    source: Mapped["Source"] = relationship(
        "Source",
        back_populates="job_source_references",
    )

    __table_args__ = (
        UniqueConstraint("source_id", "source_job_id", name="uq_job_source_references_source_ext"),
        Index("idx_job_source_ref_job", "job_id"),
        Index("idx_job_source_ref_raw", "raw_job_id"),
        Index("idx_job_source_ref_app_url", "application_url"),
    )
