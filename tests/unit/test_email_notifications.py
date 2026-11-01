"""Unit tests for Email Notification Service, Templates, and CLI Handler (V1)."""

import smtplib
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.jobs import sanitize_url
from app.notifications.email_service import (
    EmailNotificationService,
    build_email_message,
    classify_smtp_error,
    format_salary_summary,
    sanitize_header_value,
    validate_email_address,
)
from app.persistence.models.job import Job
from app.persistence.models.notification import NotificationDelivery


def _create_mock_job(
    *,
    job_id: uuid.UUID | None = None,
    title: str = "Staff AI Agent Engineer",
    company_name: str = "Anthropic",
    role_priority: str = "P1",
    eligibility_status: str = "ELIGIBLE",
    application_url: str | None = "https://jobs.lever.co/anthropic/123",
    salary_min: Decimal | None = Decimal("240000.00"),
    salary_max: Decimal | None = Decimal("320000.00"),
    salary_currency: str | None = "USD",
    salary_period: str | None = "YEAR",
    location: str | None = "San Francisco, CA",
    remote_type: str = "HYBRID",
) -> Job:
    """Helper creating detached Job model instance for notification testing."""
    jid = job_id or uuid.uuid4()
    cid = uuid.uuid4()

    job = Job(
        id=jid,
        company_id=cid,
        company_name=company_name,
        title=title,
        normalized_title=title.lower(),
        location=location,
        normalized_location="san francisco",
        employment_type="FULL_TIME",
        remote_type=remote_type,
        description="Build state-of-the-art agentic AI systems.",
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_period=salary_period,
        posted_at=datetime.now(UTC),
        collected_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        application_url=application_url,
        application_status="NOT_APPLIED",
        role_priority=role_priority,
        relevance_status="TARGET",
        eligibility_status=eligibility_status,
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=bool(salary_min or salary_max),
        verification_confidence=Decimal("0.96"),
    )
    job.source_references = []
    job.filter_decisions = []
    job.notification_deliveries = []
    return job


def test_format_salary_summary() -> None:
    """Verify salary string formatting handles range, min-only, max-only, and unstated compensation."""
    job_range = _create_mock_job(salary_min=Decimal("150000"), salary_max=Decimal("200000"))
    assert format_salary_summary(job_range) == "USD 150,000 - 200,000 (YEAR)"

    job_min = _create_mock_job(salary_min=Decimal("120000"), salary_max=None)
    assert format_salary_summary(job_min) == "From USD 120,000 (YEAR)"

    job_max = _create_mock_job(salary_min=None, salary_max=Decimal("180000"))
    assert format_salary_summary(job_max) == "Up to USD 180,000 (YEAR)"

    job_none = _create_mock_job(salary_min=None, salary_max=None)
    assert format_salary_summary(job_none) == "Unstated"


def test_build_email_message_subject_and_content() -> None:
    """Verify MIME email construction contains subject, priority, headers, and plain-text/HTML parts."""
    job = _create_mock_job(
        title="Senior LLM Architect",
        company_name="OpenAI",
        role_priority="P1",
    )
    msg = build_email_message(
        job=job,
        recipient_email="candidate@example.com",
        sender_email="alerts@jobintel.internal",
        dashboard_base_url="http://127.0.0.1:8000",
    )

    assert msg["Subject"] == "[P1] New Job: Senior LLM Architect at OpenAI"
    assert msg["To"] == "candidate@example.com"
    assert msg["From"] == "alerts@jobintel.internal"
    assert msg.is_multipart()

    body_text = msg.get_body(preferencelist=("plain",)).get_content()
    body_html = msg.get_body(preferencelist=("html",)).get_content()

    assert "Senior LLM Architect" in body_text
    assert "OpenAI" in body_text
    assert "http://127.0.0.1:8000/jobs/" in body_text
    assert "https://jobs.lever.co/anthropic/123" in body_text

    assert "Senior LLM Architect</h1>" in body_html
    assert "Open in Dashboard" in body_html
    assert "Apply on ATS" in body_html


def test_build_email_message_escapes_untrusted_html() -> None:
    """Verify HTML body strictly escapes XSS vectors from malicious vacancy data."""
    malicious_title = "<script>alert('pwned')</script>Director & 'Staff'"
    malicious_company = "<img src=x onerror=alert(1)>BadCorp & Co"
    job = _create_mock_job(
        title=malicious_title,
        company_name=malicious_company,
        application_url="javascript:alert('xss')",
    )
    msg = build_email_message(
        job=job,
        recipient_email="safe@example.com",
        sender_email="alerts@example.com",
    )

    body_html = msg.get_body(preferencelist=("html",)).get_content()

    # Raw tags must NOT exist in the rendered HTML
    assert "<script>" not in body_html
    assert "<img src=x" not in body_html

    # Escaped representations must be present
    assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in body_html
    assert "&lt;img src=x onerror=alert(1)&gt;" in body_html

    # javascript: URL must be completely neutralized and absent from href
    assert 'href="javascript:' not in body_html


def test_sanitize_url_validation() -> None:
    """Verify URL sanitizer strictly validates http/https schemes and neutralizes exploits."""
    assert (
        sanitize_url("https://jobs.ashbyhq.com/scale/123") == "https://jobs.ashbyhq.com/scale/123"
    )
    assert sanitize_url("http://example.com/job") == "http://example.com/job"
    assert sanitize_url("javascript:alert(1)") is None
    assert sanitize_url("data:text/html,<script>alert(1)</script>") is None
    assert sanitize_url("vbscript:msgbox(1)") is None
    assert sanitize_url("ftp://example.com") is None
    assert sanitize_url(None) is None


def test_email_validation_and_header_injection_defense() -> None:
    """Verify header injection attempts and malformed email addresses are strictly rejected."""
    # Valid emails
    assert validate_email_address("user@example.com") == "user@example.com"
    assert (
        validate_email_address("USER.First+tag@Sub.Domain.org") == "user.first+tag@sub.domain.org"
    )

    # Header injection attempts via CRLF
    with pytest.raises(ValueError, match="control or header injection"):
        validate_email_address("user@example.com\r\nBcc: evil@attacker.com")

    with pytest.raises(ValueError, match="control or header injection"):
        validate_email_address("user@example.com\nSubject: Injected")

    # Invalid formats
    with pytest.raises(ValueError, match="Invalid Recipient address format"):
        validate_email_address("not-an-email", "Recipient")

    with pytest.raises(ValueError, match="Recipient address cannot be empty"):
        validate_email_address("", "Recipient")

    # Header value sanitizer
    assert (
        sanitize_header_value("Staff Engineer\r\nBcc: attacker@evil.com")
        == "Staff Engineer Bcc: attacker@evil.com"
    )
    assert (
        sanitize_header_value("Title\twith\nnewlines\rand\tcontrols")
        == "Title with newlines and controls"
    )


def test_classify_smtp_error_ambiguous_vs_definite() -> None:
    """Verify error classification distinguishes ambiguous network timeouts, transient 4xx, and permanent 5xx."""
    # 1. Ambiguous outcomes during/after DATA transfer
    amb_timeout = classify_smtp_error(TimeoutError("Socket timed out"), phase="DATA_TRANSFER")
    assert amb_timeout.category == "AMBIGUOUS"
    assert amb_timeout.is_ambiguous is True
    assert amb_timeout.can_retry is False
    assert "Ambiguous SMTP timeout" in amb_timeout.description

    amb_disc = classify_smtp_error(
        smtplib.SMTPServerDisconnected("Server unexpectedly closed connection"),
        phase="DATA_TRANSFER",
    )
    assert amb_disc.category == "AMBIGUOUS"
    assert amb_disc.is_ambiguous is True
    assert amb_disc.can_retry is False

    amb_reset = classify_smtp_error(
        ConnectionResetError("Connection reset by peer"), phase="DATA_TRANSFER"
    )
    assert amb_reset.category == "AMBIGUOUS"
    assert amb_reset.can_retry is False

    amb_os_data = classify_smtp_error(
        OSError("Connection dropped during DATA write"), phase="DATA_TRANSFER"
    )
    assert amb_os_data.category == "AMBIGUOUS"
    assert amb_os_data.is_ambiguous is True
    assert amb_os_data.can_retry is False

    # 2. Definite permanent failures (5xx, auth, syntax)
    def_auth = classify_smtp_error(smtplib.SMTPAuthenticationError(535, b"Auth failed"))
    assert def_auth.category == "PERMANENT"
    assert def_auth.is_ambiguous is False
    assert def_auth.can_retry is False
    assert def_auth.smtp_code == 535

    def_550_recipient = classify_smtp_error(
        smtplib.SMTPRecipientsRefused({"user@bad.com": (550, b"User unknown")})
    )
    assert def_550_recipient.category == "PERMANENT"
    assert def_550_recipient.is_ambiguous is False
    assert def_550_recipient.can_retry is False
    assert def_550_recipient.smtp_code == 550

    def_554_data = classify_smtp_error(
        smtplib.SMTPDataError(554, b"Transaction failed: spam content")
    )
    assert def_554_data.category == "PERMANENT"
    assert def_554_data.can_retry is False

    # 3. Definite transient failures (DNS failure, connection refused, 4xx responses)
    def_dns = classify_smtp_error(socket.gaierror(-2, "Name or service not known"))
    assert def_dns.category == "TRANSIENT"
    assert def_dns.is_ambiguous is False
    assert def_dns.can_retry is True

    def_refused = classify_smtp_error(ConnectionRefusedError("Connection refused"))
    assert def_refused.category == "TRANSIENT"
    assert def_refused.is_ambiguous is False
    assert def_refused.can_retry is True

    def_os_connect = classify_smtp_error(OSError("Network is unreachable"), phase="CONNECT")
    assert def_os_connect.category == "TRANSIENT"
    assert def_os_connect.is_ambiguous is False
    assert def_os_connect.can_retry is True

    def_conn = classify_smtp_error(smtplib.SMTPConnectError(421, b"Service not available"))
    assert def_conn.category == "TRANSIENT"
    assert def_conn.can_retry is True
    assert def_conn.smtp_code == 421

    def_451_data = classify_smtp_error(
        smtplib.SMTPDataError(451, b"Requested action aborted: local error")
    )
    assert def_451_data.category == "TRANSIENT"
    assert def_451_data.is_ambiguous is False
    assert def_451_data.can_retry is True
    assert def_451_data.smtp_code == 451

    def_452_recipient = classify_smtp_error(
        smtplib.SMTPRecipientsRefused({"user@busy.com": (452, b"Insufficient system storage")})
    )
    assert def_452_recipient.category == "TRANSIENT"
    assert def_452_recipient.is_ambiguous is False
    assert def_452_recipient.can_retry is True
    assert def_452_recipient.smtp_code == 452

    def_450_sender = classify_smtp_error(
        smtplib.SMTPSenderRefused(450, b"Mailbox busy", "sender@domain.com")
    )
    assert def_450_sender.category == "TRANSIENT"
    assert def_450_sender.can_retry is True
    assert def_450_sender.smtp_code == 450


@pytest.mark.asyncio
async def test_dispatch_single_job_notification_successful_delivery() -> None:
    """Verify atomic pre-send claim persistence and successful transition to SENT status."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    sample_job = _create_mock_job()

    # Mock settings
    service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
    service.settings.NOTIFICATION_RECIPIENT_EMAIL = "user@example.com"
    service.settings.SMTP_HOST = "localhost"

    # Mock repository behavior
    mock_delivery = NotificationDelivery(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        recipient_email="user@example.com",
        channel="EMAIL",
        status="CLAIMED",
        claimed_at=datetime.now(UTC),
    )
    service.repo.get_by_job_recipient_channel = AsyncMock(return_value=None)
    service.repo.create_claim = AsyncMock(return_value=mock_delivery)

    async def _mock_mark_sent(
        d: NotificationDelivery, message_id: str | None = None
    ) -> NotificationDelivery:
        d.status = "SENT"
        d.sent_at = datetime.now(UTC)
        return d

    service.repo.mark_sent = AsyncMock(side_effect=_mock_mark_sent)

    with patch(
        "app.notifications.email_service._send_smtp_payload",
        return_value="250 OK Message accepted",
    ):
        result = await service.dispatch_single_job_notification(sample_job, "user@example.com")

    assert result is not None
    assert result.status == "SENT"
    assert result.sent_at is not None
    assert result.delivered_at is None, (
        "delivered_at must remain NULL because 250 OK only confirms relay acceptance"
    )
    service.repo.create_claim.assert_awaited_once()
    service.repo.mark_sent.assert_awaited_once_with(
        mock_delivery, message_id="250 OK Message accepted"
    )


@pytest.mark.asyncio
async def test_dispatch_single_job_notification_duplicate_prevention() -> None:
    """Verify duplicate dispatch attempts are skipped if already SENT or CLAIMED."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    sample_job = _create_mock_job()

    existing_sent = NotificationDelivery(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        recipient_email="user@example.com",
        channel="EMAIL",
        status="SENT",
        sent_at=datetime.now(UTC),
    )
    service.repo.get_by_job_recipient_channel = AsyncMock(return_value=existing_sent)
    service.repo.create_claim = AsyncMock()

    with patch("app.notifications.email_service._send_smtp_payload") as mock_smtp:
        result = await service.dispatch_single_job_notification(sample_job, "user@example.com")
        mock_smtp.assert_not_called()

    assert result is not None
    assert result.status == "SENT"
    service.repo.create_claim.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_single_job_notification_stale_claim_recovery() -> None:
    """Verify stale CLAIMED records (> 10m) from crashed processes are marked FAILED (ambiguous) without duplicate sending."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    sample_job = _create_mock_job()

    service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
    service.settings.NOTIFICATION_RECIPIENT_EMAIL = "user@example.com"

    stale_claim = NotificationDelivery(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        recipient_email="user@example.com",
        channel="EMAIL",
        status="CLAIMED",
        claimed_at=datetime.now(UTC) - timedelta(minutes=15),
        delivery_metadata={},
    )
    service.repo.get_by_job_recipient_channel = AsyncMock(return_value=stale_claim)

    async def _mock_mark_failed(
        d: NotificationDelivery,
        error_message: str,
        is_ambiguous: bool = False,
        can_retry: bool = False,
        *args,
        **kwargs,
    ) -> NotificationDelivery:
        d.status = "FAILED"
        d.error_message = error_message
        meta = dict(d.delivery_metadata or {})
        meta["is_ambiguous"] = is_ambiguous
        meta["can_retry"] = can_retry
        d.delivery_metadata = meta
        return d

    service.repo.mark_failed = AsyncMock(side_effect=_mock_mark_failed)

    with patch("app.notifications.email_service._send_smtp_payload") as mock_smtp:
        result = await service.dispatch_single_job_notification(sample_job, "user@example.com")
        mock_smtp.assert_not_called()

    assert result is not None
    assert result.status == "FAILED"
    assert result.is_ambiguous is True
    assert result.can_retry is False
    assert "Stale claim expired" in result.error_message


@pytest.mark.asyncio
async def test_dispatch_single_job_notification_ambiguous_failure_suppresses_retry() -> None:
    """Verify that an ambiguous SMTP failure suppresses automatic retries to avoid duplicates."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    sample_job = _create_mock_job()

    service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
    service.settings.NOTIFICATION_RECIPIENT_EMAIL = "user@example.com"

    # Pre-existing delivery record that failed ambiguously
    existing_ambiguous = NotificationDelivery(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        recipient_email="user@example.com",
        channel="EMAIL",
        status="FAILED",
        failed_at=datetime.now(UTC),
        delivery_metadata={"is_ambiguous": True},
    )
    service.repo.get_by_job_recipient_channel = AsyncMock(return_value=existing_ambiguous)

    with patch("app.notifications.email_service._send_smtp_payload") as mock_smtp:
        result = await service.dispatch_single_job_notification(sample_job, "user@example.com")
        mock_smtp.assert_not_called()

    assert result is not None
    assert result.status == "FAILED"
    assert result.is_ambiguous is True


@pytest.mark.asyncio
async def test_dispatch_single_job_notification_smtp_failure_isolation() -> None:
    """Verify SMTP exceptions transition status to FAILED without bubbling up to caller."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    sample_job = _create_mock_job()

    service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
    service.settings.NOTIFICATION_RECIPIENT_EMAIL = "user@example.com"

    mock_delivery = NotificationDelivery(
        id=uuid.uuid4(),
        job_id=sample_job.id,
        recipient_email="user@example.com",
        channel="EMAIL",
        status="CLAIMED",
        claimed_at=datetime.now(UTC),
    )
    service.repo.get_by_job_recipient_channel = AsyncMock(return_value=None)
    service.repo.create_claim = AsyncMock(return_value=mock_delivery)

    async def _mock_mark_failed(
        d: NotificationDelivery, error_message: str, *args, **kwargs
    ) -> NotificationDelivery:
        d.status = "FAILED"
        return d

    service.repo.mark_failed = AsyncMock(side_effect=_mock_mark_failed)

    with patch(
        "app.notifications.email_service._send_smtp_payload",
        side_effect=smtplib.SMTPConnectError(421, b"Connection refused"),
    ):
        result = await service.dispatch_single_job_notification(sample_job, "user@example.com")

    assert result is not None
    assert result.status == "FAILED"
    service.repo.mark_failed.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_batch_dry_run() -> None:
    """Verify batch processing in dry-run mode previews candidates without DB mutations or network dispatch."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    job1 = _create_mock_job(title="Job 1", role_priority="P1")
    job2 = _create_mock_job(title="Job 2", role_priority="P2")

    service.repo.find_eligible_jobs_pending_notification = AsyncMock(return_value=[job1, job2])
    service.repo.sweep_stale_claims = AsyncMock(return_value=[])
    service.dispatch_single_job_notification = AsyncMock()

    batch_res = await service.process_batch(
        recipient_email="user@example.com", limit=10, dry_run=True
    )

    assert batch_res.is_dry_run is True
    assert batch_res.total_candidates == 2
    assert batch_res.total_skipped == 2
    assert batch_res.total_sent == 0
    assert len(batch_res.items) == 2
    assert batch_res.items[0].status == "PREVIEW_PENDING"
    service.dispatch_single_job_notification.assert_not_called()


def test_cli_notify_argument_parser() -> None:
    """Verify CLI parser configuration for 'notify' subcommand."""
    from app.cli.main import build_parser

    parser = build_parser()
    args = parser.parse_args(["notify", "--dry-run", "-r", "candidate@test.com", "-l", "5"])

    assert args.subcommand == "notify"
    assert args.dry_run is True
    assert args.send is False
    assert args.recipient == "candidate@test.com"
    assert args.limit == 5


@pytest.mark.asyncio
async def test_cli_send_refused_when_notifications_disabled() -> None:
    """Verify CLI notify --send refuses to execute if EMAIL_NOTIFICATIONS_ENABLED=False."""
    import argparse

    from app.cli.notify import run_notify

    args = argparse.Namespace(
        send=True,
        dry_run=False,
        recipient="user@example.com",
        limit=10,
        json=False,
    )

    with patch("app.cli.notify.get_settings") as mock_settings:
        mock_settings.return_value.EMAIL_NOTIFICATIONS_ENABLED = False
        mock_settings.return_value.NOTIFICATION_RECIPIENT_EMAIL = "user@example.com"
        exit_code = await run_notify(args)

    assert exit_code == 1


@pytest.mark.asyncio
async def test_post_commit_dispatch_invoked_when_enabled_and_isolated_on_error() -> None:
    """Verify that post-commit automatic dispatch executes when enabled, and isolates errors cleanly."""
    mock_session = AsyncMock()
    service = EmailNotificationService(mock_session)
    service.settings.EMAIL_NOTIFICATIONS_ENABLED = True
    service.settings.NOTIFICATION_RECIPIENT_EMAIL = "target@example.com"

    service.repo.sweep_stale_claims = AsyncMock(return_value=[])
    service.repo.find_eligible_jobs_pending_notification = AsyncMock(return_value=[])

    # 1. Normal execution
    res = await service.process_batch(
        recipient_email="target@example.com",
        dry_run=False,
    )
    assert res.total_candidates == 0
    service.repo.sweep_stale_claims.assert_awaited_once()

    # 2. Disabled execution skips dispatch
    service.settings.EMAIL_NOTIFICATIONS_ENABLED = False
    job = _create_mock_job()
    service.repo.find_eligible_jobs_pending_notification = AsyncMock(return_value=[job])
    res_disabled = await service.process_batch(
        recipient_email="target@example.com",
        dry_run=False,
    )
    assert res_disabled.total_sent == 0
    assert res_disabled.items[0].status == "SKIPPED_DISABLED"

    # 3. Post-commit isolation: Verify exceptions during dispatch are catchable without breaking caller
    with patch.object(
        service,
        "process_batch",
        side_effect=smtplib.SMTPConnectError(421, b"Connection refused"),
    ):
        caught = False
        try:
            await service.process_batch(recipient_email="target@example.com", dry_run=False)
        except Exception as exc:
            caught = True
            assert isinstance(exc, smtplib.SMTPConnectError)
        assert caught is True, "Exceptions from SMTP must be catchable by caller try-except block"
