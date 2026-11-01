"""Live PostgreSQL integration tests for Email Notification Delivery and De-duplication.

Boundary: End-to-end integration tests using PostgreSQL test database (job_intelligence_test).
Validates:
1. Target vacancy qualification: Only P1/P2 priority with ELIGIBLE or REVIEW statuses qualify.
2. Exclusion of human-dismissed and candidate-dismissed jobs using latest review state.
3. Zero database writes during --dry-run execution.
4. Pre-send claim persistence and at-most-once delivery de-duplication.
5. SMTP failure isolation and diagnostic failure tracking without session rollback.
"""

import smtplib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.notifications.email_service import EmailNotificationService
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.notification import NotificationDelivery
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.notification_repository import (
    NotificationDeliveryRepository,
)
from app.persistence.session import get_sessionmaker

pytestmark = pytest.mark.integration


async def _seed_notification_test_jobs() -> dict[str, uuid.UUID]:
    """Seed diverse vacancies with varied priorities, eligibility, and human review states."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        src = Source(
            source_name="greenhouse",
            source_type="ATS",
            base_url="https://boards-api.greenhouse.io",
            is_active=True,
        )
        session.add(src)
        await session.flush()

        comp = Company(
            name="OpenAI",
            normalized_name="openai",
            website="https://openai.com",
            industry="Artificial Intelligence",
            sector="Technology",
            is_verified=True,
            confidence=Decimal("1.00"),
        )
        session.add(comp)
        await session.flush()

        cs = CompanySource(
            company_id=comp.id,
            source_id=src.id,
            ats_identifier="openai",
            career_url="https://openai.com/careers",
            discovery_method="REGISTRY",
            is_active=True,
        )
        session.add(cs)
        await session.flush()

        # Helper to create canonical jobs
        async def _create_job(
            title: str,
            role_priority: str,
            eligibility_status: str,
            application_status: str = "NOT_APPLIED",
        ) -> Job:
            raw = RawJob(
                company_source_id=cs.id,
                source_job_id=f"job-{uuid.uuid4().hex[:8]}",
                source_url="https://boards.greenhouse.io/openai/1",
                raw_body=b'{"title": "test"}',
                raw_payload={"title": "test"},
                content_hash=uuid.uuid4().hex,
            )
            session.add(raw)
            await session.flush()

            j = Job(
                company_id=comp.id,
                company_name="OpenAI",
                title=title,
                normalized_title=title.lower(),
                location="San Francisco, CA",
                normalized_location="san francisco",
                employment_type="FULL_TIME",
                remote_type="HYBRID",
                description="We are seeking world-class talent to advance beneficial AI.",
                salary_min=Decimal("200000.00"),
                salary_max=Decimal("280000.00"),
                salary_currency="USD",
                salary_period="YEAR",
                posted_at=datetime.now(UTC),
                collected_at=datetime.now(UTC),
                application_url="https://openai.com/careers/apply",
                application_status=application_status,
                role_priority=role_priority,
                relevance_status="TARGET",
                eligibility_status=eligibility_status,
                source_verified=True,
                jd_verified=True,
                company_verified=True,
                location_verified=True,
                experience_verified=True,
                salary_verified=True,
                verification_confidence=Decimal("0.95"),
            )
            session.add(j)
            await session.flush()

            ref = JobSourceReference(
                job_id=j.id,
                raw_job_id=raw.id,
                source_id=src.id,
                source_job_id=raw.source_job_id,
                source_url=raw.source_url,
                application_url=j.application_url,
                is_primary=True,
            )
            session.add(ref)

            vr = VerificationRecord(
                job_id=j.id,
                source_verified=True,
                jd_verified=True,
                application_url_verified=True,
                company_verified=True,
                location_verified=True,
                experience_verified=True,
                salary_verified=True,
                posting_date_verified=True,
                verification_confidence=Decimal("0.95"),
                passed=True,
                audit_trail={},
            )
            session.add(vr)
            return j

        # 1. P1 ELIGIBLE -> Qualifies for notification!
        job_p1_eligible = await _create_job("Staff Agentic AI Engineer", "P1", "ELIGIBLE")

        # 2. P2 REVIEW -> Qualifies for notification!
        job_p2_review = await _create_job("Senior Applied ML Infrastructure", "P2", "REVIEW")

        # 3. P3 ELIGIBLE -> Fails role priority qualification (P3 excluded from notifications)
        job_p3_eligible = await _create_job("General Frontend Developer", "P3", "ELIGIBLE")

        # 4. P1 REJECTED -> Fails eligibility qualification (REJECTED excluded)
        job_p1_rejected = await _create_job("Senior Salesforce Specialist", "P1", "REJECTED")

        # 5. P1 Human Dismissed -> Fails qualification due to latest HUMAN_REVIEW_V1 decision == DISMISSED
        job_p1_dismissed = await _create_job("Principal AI Researcher", "P1", "ELIGIBLE")
        d_dismiss = FilterDecision(
            job_id=job_p1_dismissed.id,
            rule_version="HUMAN_REVIEW_V1",
            final_status="DISMISSED",
            role_priority="P1",
            reason_codes=["HUMAN_DISMISS"],
            evaluation_context={"reason": "Not interested"},
            evaluated_at=datetime.now(UTC),
        )
        session.add(d_dismiss)

        # 6. P2 Candidate Dismissed -> Fails qualification due to application_status == DISMISSED
        job_p2_app_dismissed = await _create_job(
            "AI Platform Backend Engineer", "P2", "ELIGIBLE", application_status="DISMISSED"
        )

        await session.commit()

        return {
            "p1_eligible": job_p1_eligible.id,
            "p2_review": job_p2_review.id,
            "p3_eligible": job_p3_eligible.id,
            "p1_rejected": job_p1_rejected.id,
            "p1_dismissed": job_p1_dismissed.id,
            "p2_app_dismissed": job_p2_app_dismissed.id,
        }


@pytest.mark.asyncio
async def test_integration_find_eligible_jobs_filtering() -> None:
    """Verify notification query selects ONLY qualifying P1/P2 ELIGIBLE/REVIEW vacancies while excluding dismissed."""
    seeded = await _seed_notification_test_jobs()

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        repo = NotificationDeliveryRepository(session)
        candidates = await repo.find_eligible_jobs_pending_notification("target@example.com")
        candidate_ids = {c.id for c in candidates}

    # Exactly P1 eligible and P2 review must qualify
    assert candidate_ids == {seeded["p1_eligible"], seeded["p2_review"]}
    assert seeded["p3_eligible"] not in candidate_ids
    assert seeded["p1_rejected"] not in candidate_ids
    assert seeded["p1_dismissed"] not in candidate_ids
    assert seeded["p2_app_dismissed"] not in candidate_ids


@pytest.mark.asyncio
async def test_integration_batch_dry_run_zero_writes() -> None:
    """Verify batch execution in dry-run mode previews candidates with zero database writes."""
    await _seed_notification_test_jobs()

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        service = EmailNotificationService(session)
        batch_res = await service.process_batch(
            recipient_email="target@example.com",
            dry_run=True,
        )

    assert batch_res.is_dry_run is True
    assert batch_res.total_candidates == 2
    assert batch_res.total_skipped == 2
    assert batch_res.total_delivered == 0

    # Confirm 0 records were written to notification_deliveries table
    async with session_factory() as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert len(deliveries) == 0


@pytest.mark.asyncio
async def test_integration_batch_live_dispatch_and_duplicate_prevention() -> None:
    """Verify atomic delivery claiming, SMTP dispatch, and second-run duplicate prevention."""
    await _seed_notification_test_jobs()

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        service = EmailNotificationService(session)
        service.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch(
            "app.notifications.email_service._send_smtp_payload",
            return_value="250 OK Message queued",
        ):
            batch_1 = await service.process_batch(
                recipient_email="target@example.com",
                dry_run=False,
            )

    assert batch_1.total_candidates == 2
    assert batch_1.total_delivered == 2
    assert batch_1.total_failed == 0

    # Verify rows in PostgreSQL
    async with session_factory() as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert len(deliveries) == 2
        for d in deliveries:
            assert d.status == "SENT"
            assert d.channel == "EMAIL"
            assert d.recipient_email == "target@example.com"
            assert d.sent_at is not None
            assert d.delivered_at is None, (
                "delivered_at must remain NULL because SMTP 250 only confirms relay acceptance"
            )
            assert d.claimed_at is not None

    # Second Run: Duplicate prevention must detect already-delivered jobs and find 0 pending
    async with session_factory() as session:
        service_2 = EmailNotificationService(session)
        service_2.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch("app.notifications.email_service._send_smtp_payload") as mock_smtp:
            batch_2 = await service_2.process_batch(
                recipient_email="target@example.com",
                dry_run=False,
            )
            mock_smtp.assert_not_called()

    assert batch_2.total_candidates == 0
    assert batch_2.total_delivered == 0


@pytest.mark.asyncio
async def test_integration_smtp_failure_handling() -> None:
    """Verify SMTP connection exceptions transition delivery row to FAILED with error diagnostic."""
    await _seed_notification_test_jobs()

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        service = EmailNotificationService(session)
        service.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch(
            "app.notifications.email_service._send_smtp_payload",
            side_effect=smtplib.SMTPConnectError(421, b"Connection refused by remote host"),
        ):
            batch = await service.process_batch(
                recipient_email="target@example.com",
                limit=1,
                dry_run=False,
            )

    assert batch.total_candidates >= 1
    assert batch.total_failed == 1
    assert batch.total_delivered == 0

    # Verify status in database
    async with session_factory() as session:
        deliveries = (await session.execute(select(NotificationDelivery))).scalars().all()
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.status == "FAILED"
        assert d.failed_at is not None
        assert d.retry_count == 1
        assert "Connection refused" in (d.error_message or "")


@pytest.mark.asyncio
async def test_integration_stale_claim_recovery_and_retry_suppression() -> None:
    """Verify stale claims older than 10m are swept to FAILED (is_ambiguous=True) and retries suppressed."""
    from datetime import timedelta

    job_ids = await _seed_notification_test_jobs()
    eligible_job_id = job_ids["p1_eligible"]

    session_factory = get_sessionmaker()
    # 1. Insert an artificial stale claim from a crashed worker
    async with session_factory() as session:
        stale_delivery = NotificationDelivery(
            job_id=eligible_job_id,
            recipient_email="target@example.com",
            channel="EMAIL",
            status="CLAIMED",
            claimed_at=datetime.now(UTC) - timedelta(minutes=15),
        )
        session.add(stale_delivery)
        await session.commit()

    # 2. Run repository stale claim sweep
    async with session_factory() as session:
        repo = NotificationDeliveryRepository(session)
        swept = await repo.sweep_stale_claims(stale_threshold_seconds=600)
        await session.commit()
        assert len(swept) == 1
        assert swept[0].status == "FAILED"
        assert swept[0].is_ambiguous is True
        assert swept[0].can_retry is False
        assert swept[0].delivery_metadata.get("can_retry") is False

    # 3. Query pending vacancies: The ambiguous failure must NOT be automatically retried
    async with session_factory() as session:
        repo = NotificationDeliveryRepository(session)
        pending = await repo.find_eligible_jobs_pending_notification(
            recipient_email="target@example.com",
            channel="EMAIL",
        )
        # The swept job must be excluded because automatic retries are suppressed
        assert all(j.id != eligible_job_id for j in pending)


@pytest.mark.asyncio
async def test_integration_concurrent_workers_claim_race_safety() -> None:
    """Verify that when two workers race to claim the same job concurrently, only one proceeds to SMTP dispatch."""
    import asyncio
    from unittest.mock import MagicMock

    job_ids = await _seed_notification_test_jobs()
    eligible_job_id = job_ids["p1_eligible"]

    session_factory = get_sessionmaker()

    # Retrieve detached Job object to pass to both workers
    async with session_factory() as session:
        job = (await session.execute(select(Job).where(Job.id == eligible_job_id))).scalar_one()

    # Track how many times SMTP socket dispatch is actually invoked
    smtp_mock = MagicMock(return_value="250 OK Message queued")

    async def _worker_run(worker_name: str) -> NotificationDelivery | None:
        async with session_factory() as session:
            service = EmailNotificationService(session)
            service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
            with patch(
                "app.notifications.email_service._send_smtp_payload",
                smtp_mock,
            ):
                return await service.dispatch_single_job_notification(
                    job, "concurrent_target@example.com"
                )

    # Launch both workers concurrently
    result_1, result_2 = await asyncio.gather(
        _worker_run("Worker-1"),
        _worker_run("Worker-2"),
    )

    # Exactly one SMTP dispatch must occur across the concurrent execution
    assert smtp_mock.call_count == 1, (
        f"Expected exactly 1 SMTP send_message call, got {smtp_mock.call_count}"
    )

    # Both workers should have received a non-null delivery record
    assert result_1 is not None
    assert result_2 is not None

    # Exactly one record exists in PostgreSQL with unique constraint intact
    async with session_factory() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.job_id == eligible_job_id,
                        NotificationDelivery.recipient_email == "concurrent_target@example.com",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.status in ("SENT", "CLAIMED")
        assert d.sent_at is not None
        assert d.delivered_at is None


@pytest.mark.asyncio
async def test_integration_permanent_5xx_failure_retry_suppressed() -> None:
    """Verify permanent 5xx SMTP rejections mark can_retry=False and suppress automatic retries."""
    job_ids = await _seed_notification_test_jobs()
    eligible_job_id = job_ids["p1_eligible"]

    session_factory = get_sessionmaker()

    async with session_factory() as session:
        job = (await session.execute(select(Job).where(Job.id == eligible_job_id))).scalar_one()
        service = EmailNotificationService(session)
        service.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch(
            "app.notifications.email_service._send_smtp_payload",
            side_effect=smtplib.SMTPRecipientsRefused(
                {"perm_target@example.com": (550, b"User unknown / mailbox not found")}
            ),
        ):
            res = await service.dispatch_single_job_notification(job, "perm_target@example.com")

    assert res is not None
    assert res.status == "FAILED"
    assert res.can_retry is False
    assert res.is_ambiguous is False
    assert "Permanent SMTP recipient rejection" in (res.error_message or "")

    # Query pending vacancies: The permanent failure must NOT be retried
    async with session_factory() as session:
        repo = NotificationDeliveryRepository(session)
        pending = await repo.find_eligible_jobs_pending_notification(
            recipient_email="perm_target@example.com",
            channel="EMAIL",
        )
        assert all(j.id != eligible_job_id for j in pending)


@pytest.mark.asyncio
async def test_integration_stale_claim_smtp_accepted_crash_prevents_duplicate_dispatch() -> None:
    """Simulate SMTP relay acceptance followed by worker crash before database update.

    Validates:
    1. Pre-send CLAIMED record was committed before opening SMTP socket.
    2. SMTP accepts email (250 OK), but worker terminates before SENT commit.
    3. The claim becomes stale (> 10m).
    4. A subsequent worker sweeps/inspects the claim, classifies it as AMBIGUOUS (can_retry=False).
    5. The subsequent worker NEVER sends a duplicate email over SMTP.
    """
    from datetime import timedelta
    from unittest.mock import MagicMock

    job_ids = await _seed_notification_test_jobs()
    eligible_job_id = job_ids["p1_eligible"]

    session_factory = get_sessionmaker()

    # Retrieve detached Job object
    async with session_factory() as session:
        job = (await session.execute(select(Job).where(Job.id == eligible_job_id))).scalar_one()

    # Step 1: Worker 1 claims the delivery and sends SMTP payload, but crashes before mark_sent
    async with session_factory() as session:
        repo = NotificationDeliveryRepository(session)
        # Pre-send claim committed
        claimed = await repo.create_claim(
            job_id=eligible_job_id,
            recipient_email="crash_target@example.com",
            channel="EMAIL",
            delivery_metadata={"worker": "worker_1"},
        )
        # Simulate that this happened 15 minutes ago
        claimed.claimed_at = datetime.now(UTC) - timedelta(minutes=15)
        await session.commit()

    # Step 2: Verify that Worker 1's claim is visible in DB as CLAIMED
    async with session_factory() as session:
        d = await NotificationDeliveryRepository(session).get_by_job_recipient_channel(
            eligible_job_id, "crash_target@example.com", channel="EMAIL"
        )
        assert d is not None
        assert d.status == "CLAIMED"

    # Step 3: Worker 2 runs. It must detect the stale claim, mark it FAILED with
    # is_ambiguous=True and can_retry=False, and NEVER dispatch to SMTP.
    smtp_mock = MagicMock(return_value="250 OK Message queued")

    async with session_factory() as session:
        service_2 = EmailNotificationService(session)
        service_2.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch("app.notifications.email_service._send_smtp_payload", smtp_mock):
            res_2 = await service_2.dispatch_single_job_notification(
                job, "crash_target@example.com"
            )

    # Assert Worker 2 DID NOT call SMTP!
    smtp_mock.assert_not_called()
    assert res_2 is not None
    assert res_2.status == "FAILED"
    assert res_2.is_ambiguous is True
    assert res_2.can_retry is False
    assert "Stale claim expired" in (res_2.error_message or "")

    # Step 4: Worker 3 runs later. It must see is_ambiguous=True and also NOT call SMTP.
    async with session_factory() as session:
        service_3 = EmailNotificationService(session)
        service_3.settings.EMAIL_NOTIFICATIONS_ENABLED = True

        with patch("app.notifications.email_service._send_smtp_payload", smtp_mock):
            res_3 = await service_3.dispatch_single_job_notification(
                job, "crash_target@example.com"
            )

    smtp_mock.assert_not_called()
    assert res_3 is not None
    assert res_3.status == "FAILED"
    assert res_3.is_ambiguous is True
    assert res_3.can_retry is False

    # Step 5: Direct DB check: exactly 1 delivery row exists and is marked FAILED (ambiguous)
    async with session_factory() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery).where(
                        NotificationDelivery.job_id == eligible_job_id,
                        NotificationDelivery.recipient_email == "crash_target@example.com",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(deliveries) == 1
        final_d = deliveries[0]
        assert final_d.status == "FAILED"
        assert final_d.is_ambiguous is True
        assert final_d.can_retry is False
        assert final_d.sent_at is None
        assert final_d.delivered_at is None
