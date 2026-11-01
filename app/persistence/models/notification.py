"""Notification delivery persistence model."""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
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


class NotificationDelivery(Base):
    """Tracks asynchronous notification delivery claims, status, and audit timestamps.

    Maintains unique constraint across (job_id, recipient_email, channel) to prevent
    duplicate dispatch attempts. Separated completely from deterministic eligibility
    and human triage metrics.

    Timestamp Semantics:
    - claimed_at: Recorded when worker locks record prior to SMTP dispatch attempt.
    - sent_at: Recorded when SMTP relay server acknowledges message with 250 OK.
    - delivered_at: Kept NULL for standard SMTP relay dispatches, as 250 OK confirms
      relay acceptance but does not guarantee final inbox arrival. Reserved for verifiable
      inbox delivery webhooks if available.
    - failed_at: Recorded upon definite failure or ambiguous timeout.
    """

    __tablename__ = "notification_deliveries"

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
    recipient_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="EMAIL",
        server_default=text("'EMAIL'"),
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    delivery_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
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
    job: Mapped["Job"] = relationship(
        "Job",
        back_populates="notification_deliveries",
    )

    @property
    def is_ambiguous(self) -> bool:
        """Indicate whether this delivery resulted in an ambiguous network outcome."""
        return bool(self.delivery_metadata.get("is_ambiguous", False))

    @property
    def can_retry(self) -> bool:
        """Indicate whether automatic retry is permitted for this delivery attempt."""
        return bool(self.delivery_metadata.get("can_retry", False))

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "recipient_email",
            "channel",
            name="uq_notification_deliveries_job_recipient_channel",
        ),
        Index("idx_notification_deliveries_job_id", "job_id"),
        Index("idx_notification_deliveries_status_created", "status", "created_at"),
    )
