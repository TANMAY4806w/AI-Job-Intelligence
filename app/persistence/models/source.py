"""Source and CompanySource persistence models."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.models.base import Base

if TYPE_CHECKING:
    from app.persistence.models.company import Company
    from app.persistence.models.raw_job import JobSourceReference, RawJob
    from app.persistence.models.run import CollectionRun


class Source(Base):
    """Master catalog of collection platforms and ATS engines."""

    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    source_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    rate_limit_rpm: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=60,
        server_default=text("60"),
    )
    requires_browser: Mapped[bool] = mapped_column(
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )

    # Relationships
    company_sources: Mapped[list["CompanySource"]] = relationship(
        "CompanySource",
        back_populates="source",
    )
    job_source_references: Mapped[list["JobSourceReference"]] = relationship(
        "JobSourceReference",
        back_populates="source",
    )
    collection_runs: Mapped[list["CollectionRun"]] = relationship(
        "CollectionRun",
        back_populates="source",
    )

    __table_args__ = (Index("idx_sources_active", "is_active", "source_type"),)


class CompanySource(Base):
    """Mapping of companies to detected ATS boards and endpoints."""

    __tablename__ = "company_sources"

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
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ats_identifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    career_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2),
        nullable=False,
        default=Decimal("1.00"),
        server_default=text("1.00"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    last_discovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_collected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
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

    # Relationships
    company: Mapped["Company"] = relationship(
        "Company",
        back_populates="company_sources",
    )
    source: Mapped["Source"] = relationship(
        "Source",
        back_populates="company_sources",
    )
    raw_jobs: Mapped[list["RawJob"]] = relationship(
        "RawJob",
        back_populates="company_source",
    )
    collection_runs: Mapped[list["CollectionRun"]] = relationship(
        "CollectionRun",
        back_populates="company_source",
    )

    __table_args__ = (
        CheckConstraint(
            "ats_identifier IS NOT NULL OR career_url IS NOT NULL OR api_endpoint IS NOT NULL",
            name="chk_company_sources_has_locator",
        ),
        # Partial unique indexes
        Index(
            "uq_company_sources_ats",
            "company_id",
            "source_id",
            "ats_identifier",
            unique=True,
            postgresql_where=text("ats_identifier IS NOT NULL"),
        ),
        Index(
            "uq_company_sources_career_url",
            "company_id",
            "source_id",
            "career_url",
            unique=True,
            postgresql_where=text("ats_identifier IS NULL AND career_url IS NOT NULL"),
        ),
        Index(
            "uq_company_sources_api_endpoint",
            "company_id",
            "source_id",
            "api_endpoint",
            unique=True,
            postgresql_where=text(
                "ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NOT NULL"
            ),
        ),
        Index(
            "uq_company_sources_bare_mapping",
            "company_id",
            "source_id",
            unique=True,
            postgresql_where=text(
                "ats_identifier IS NULL AND career_url IS NULL AND api_endpoint IS NULL"
            ),
        ),
        # Operational query indexes
        Index("idx_company_sources_lookup", "company_id", "source_id"),
        Index(
            "idx_company_sources_ats",
            "source_id",
            "ats_identifier",
            postgresql_where=text("ats_identifier IS NOT NULL"),
        ),
        Index("idx_company_sources_schedule", "is_active", "last_collected_at"),
    )
