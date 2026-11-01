"""Notification delivery asynchronous persistence repository."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import selectinload

from app.persistence.models.decision import FilterDecision
from app.persistence.models.job import Job
from app.persistence.models.notification import NotificationDelivery
from app.persistence.models.raw_job import JobSourceReference
from app.persistence.repositories.base import BaseRepository


class NotificationDeliveryRepository(BaseRepository):
    """Repository managing asynchronous notification delivery claims, status, and de-duplication."""

    async def get_by_job_recipient_channel(
        self,
        job_id: uuid.UUID,
        recipient_email: str,
        channel: str = "EMAIL",
    ) -> NotificationDelivery | None:
        """Retrieve a delivery record by composite unique key."""
        stmt = select(NotificationDelivery).where(
            NotificationDelivery.job_id == job_id,
            NotificationDelivery.recipient_email == recipient_email.strip().lower(),
            NotificationDelivery.channel == channel.strip().upper(),
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_claim(
        self,
        job_id: uuid.UUID,
        recipient_email: str,
        channel: str = "EMAIL",
        delivery_metadata: dict[str, Any] | None = None,
    ) -> NotificationDelivery:
        """Persist or re-claim a pre-send delivery claim to enforce at-most-once dispatch semantics.

        Preserves database-level composite uniqueness on (job_id, recipient_email, channel) by
        updating an existing record if a retryable definite failure is being retried.
        """
        normalized_recipient = recipient_email.strip().lower()
        normalized_channel = channel.strip().upper()
        now = datetime.now(UTC)

        existing = await self.get_by_job_recipient_channel(
            job_id=job_id,
            recipient_email=normalized_recipient,
            channel=normalized_channel,
        )
        if existing:
            existing.status = "CLAIMED"
            existing.claimed_at = now
            existing.updated_at = now
            meta = dict(existing.delivery_metadata or {})
            if delivery_metadata:
                meta.update(delivery_metadata)
            meta["is_ambiguous"] = False
            existing.delivery_metadata = meta
            await self.session.flush()
            return existing

        delivery = NotificationDelivery(
            job_id=job_id,
            recipient_email=normalized_recipient,
            channel=normalized_channel,
            status="CLAIMED",
            claimed_at=now,
            delivery_metadata=delivery_metadata or {},
        )
        self.session.add(delivery)
        await self.session.flush()
        return delivery

    async def mark_sent(
        self,
        delivery: NotificationDelivery,
        message_id: str | None = None,
    ) -> NotificationDelivery:
        """Transition claimed delivery record to SENT status upon SMTP 250 acknowledgment.

        Note: Status SENT indicates that the local/upstream SMTP relay acknowledged acceptance
        of the message (250 OK). This guarantees message handoff to the mail transfer agent,
        but does NOT guarantee delivery to the recipient's inbox.

        Timestamp Semantics:
        - sent_at: Populated with current UTC timestamp upon 250 OK.
        - delivered_at: Kept NULL because standard SMTP relay handoffs do not confirm final
          inbox receipt.
        """
        now = datetime.now(UTC)
        delivery.status = "SENT"
        delivery.sent_at = now
        delivery.delivered_at = (
            None  # Kept NULL: 250 OK indicates relay acceptance, not confirmed inbox receipt
        )
        delivery.updated_at = now
        meta = dict(delivery.delivery_metadata or {})
        meta["is_ambiguous"] = False
        meta["can_retry"] = False
        if message_id:
            meta["message_id"] = message_id
        delivery.delivery_metadata = meta
        await self.session.flush()
        return delivery

    async def mark_delivered(
        self,
        delivery: NotificationDelivery,
        message_id: str | None = None,
    ) -> NotificationDelivery:
        """Backward-compatible alias for mark_sent."""
        return await self.mark_sent(delivery, message_id=message_id)

    async def mark_failed(
        self,
        delivery: NotificationDelivery,
        error_message: str,
        is_ambiguous: bool = False,
        can_retry: bool = False,
        smtp_code: int | None = None,
    ) -> NotificationDelivery:
        """Transition delivery record to FAILED status with diagnostic error details.

        Args:
            delivery: The delivery record.
            error_message: Detailed diagnostic error message.
            is_ambiguous: If True, indicates an ambiguous network outcome (e.g. timeout during DATA,
                          unexpected connection disconnect) where the relay server may have accepted
                          the message. Automatic retries are suppressed for ambiguous failures to
                          prevent duplicate email delivery.
            can_retry: If True, indicates a definite transient failure (e.g. 4xx temporary error,
                       connect refused) where bounded retries are permitted. If False (e.g. 5xx
                       permanent rejection, credential error), retries are suppressed.
            smtp_code: Optional integer SMTP response code.
        """
        now = datetime.now(UTC)
        delivery.status = "FAILED"
        delivery.failed_at = now
        delivery.error_message = error_message
        delivery.retry_count += 1
        meta = dict(delivery.delivery_metadata or {})
        meta["is_ambiguous"] = is_ambiguous
        meta["can_retry"] = can_retry
        if smtp_code is not None:
            meta["smtp_code"] = smtp_code
        delivery.delivery_metadata = meta
        delivery.updated_at = now
        await self.session.flush()
        return delivery

    async def sweep_stale_claims(
        self,
        stale_threshold_seconds: int = 600,
    ) -> list[NotificationDelivery]:
        """Sweep abandoned CLAIMED records caused by process crashes or unexpected terminations.

        Because the outcome of an interrupted process is inherently ambiguous (the message
        may have been transmitted to the SMTP relay before the crash), swept stale claims
        are transitioned to FAILED with is_ambiguous=True. Automatic retries are suppressed
        to guarantee at-most-once delivery.
        """
        from datetime import timedelta

        threshold = datetime.now(UTC) - timedelta(seconds=stale_threshold_seconds)
        stmt = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.status == "CLAIMED",
                NotificationDelivery.claimed_at < threshold,
            )
            .with_for_update(skip_locked=True)
        )
        result = await self.session.execute(stmt)
        stale_records = list(result.scalars().all())
        now = datetime.now(UTC)
        for rec in stale_records:
            rec.status = "FAILED"
            rec.failed_at = now
            rec.error_message = (
                "Stale claim expired: process terminated or timed out before completion. "
                "Automatic retry suppressed to guarantee at-most-once delivery."
            )
            rec.retry_count += 1
            meta = dict(rec.delivery_metadata or {})
            meta["is_ambiguous"] = True
            meta["can_retry"] = False
            meta["stale_claim_swept"] = True
            rec.delivery_metadata = meta
            rec.updated_at = now
        if stale_records:
            await self.session.flush()
        return stale_records

    async def find_eligible_jobs_pending_notification(
        self,
        recipient_email: str,
        channel: str = "EMAIL",
        limit: int = 50,
    ) -> Sequence[Job]:
        """Query qualifying vacancies eligible for notification that have not been claimed or delivered.

        Criteria:
        1. role_priority in ('P1', 'P2')
        2. eligibility_status in ('ELIGIBLE', 'REVIEW')
        3. Excludes dismissed jobs using the latest HUMAN_REVIEW_V1 decision
        4. Excludes jobs with existing CLAIMED or DELIVERED records for this recipient & channel
        """
        normalized_recipient = recipient_email.strip().lower()
        normalized_channel = channel.strip().upper()

        # Correlated scalar subquery for latest human review decision
        latest_human_decision = (
            select(FilterDecision.final_status)
            .where(
                FilterDecision.job_id == Job.id,
                FilterDecision.rule_version == "HUMAN_REVIEW_V1",
            )
            .order_by(FilterDecision.evaluated_at.desc(), FilterDecision.id.desc())
            .limit(1)
            .scalar_subquery()
        )

        # Clauses
        clauses = [
            Job.role_priority.in_(["P1", "P2"]),
            Job.eligibility_status.in_(["ELIGIBLE", "REVIEW"]),
            Job.application_status != "DISMISSED",
            or_(
                latest_human_decision.is_(None),
                latest_human_decision != "DISMISSED",
            ),
            ~Job.notification_deliveries.any(
                and_(
                    NotificationDelivery.recipient_email == normalized_recipient,
                    NotificationDelivery.channel == normalized_channel,
                    or_(
                        NotificationDelivery.status.in_(
                            ["CLAIMED", "SENT", "DELIVERED", "SMTP_ACCEPTED"]
                        ),
                        and_(
                            NotificationDelivery.status == "FAILED",
                            or_(
                                NotificationDelivery.retry_count >= 2,
                                NotificationDelivery.delivery_metadata["is_ambiguous"].astext
                                == "true",
                                NotificationDelivery.delivery_metadata["can_retry"].astext
                                == "false",
                            ),
                        ),
                    ),
                )
            ),
        ]

        stmt = (
            select(Job)
            .options(
                selectinload(Job.company),
                selectinload(Job.source_references).selectinload(JobSourceReference.source),
                selectinload(Job.notification_deliveries),
            )
            .where(and_(*clauses))
            .order_by(Job.posted_at.desc().nullslast(), Job.collected_at.desc())
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        return result.scalars().all()
