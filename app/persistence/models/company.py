"""Company persistence model."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Index, Numeric, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.models.base import Base

if TYPE_CHECKING:
    from app.persistence.models.job import Job
    from app.persistence.models.source import CompanySource


class Company(Base):
    """Canonical legal or operating corporate entity."""

    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    website: Mapped[str | None] = mapped_column(String(512), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sector: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default=text("'UNKNOWN'"),
    )
    business_model: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNKNOWN",
        server_default=text("'UNKNOWN'"),
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    verification_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(3, 2),
        nullable=False,
        default=Decimal("0.50"),
        server_default=text("0.50"),
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
        back_populates="company",
    )
    jobs: Mapped[list["Job"]] = relationship(
        "Job",
        back_populates="company",
    )

    __table_args__ = (
        Index("idx_companies_normalized_name", "normalized_name"),
        Index("idx_companies_sector_model", "sector", "business_model"),
        Index("idx_companies_verified", "is_verified"),
    )
