"""Email notification service and SMTP dispatch engine (V1).

Boundary & Architectural Directives:
1. Web dashboard remains the primary interface and source of truth.
2. Email is the only notification channel. Absolutely no Telegram implementation.
3. Uses Python standard library (smtplib, email.message) with zero external dependencies.
4. Pre-send claim persistence guarantees at-most-once dispatch initiation and prevents duplicate emails.
5. SMTP failures are isolated and never roll back core ingestion or collection transactions.
6. HTML templates strictly escape untrusted inputs (JDs, titles, company names) and sanitize URLs.
7. SMTP 250 OK marks a message as SENT (SMTP accepted for relay), not guaranteed inbox delivery.
8. Ambiguous network outcomes (timeouts, abrupt disconnects) suppress automatic retries to prevent duplicate delivery.
"""

import asyncio
import contextlib
import html
import logging
import re
import smtplib
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.jobs import sanitize_url
from app.config import get_settings
from app.persistence.models.job import Job
from app.persistence.models.notification import NotificationDelivery
from app.persistence.repositories.notification_repository import (
    NotificationDeliveryRepository,
)

logger = logging.getLogger("app.notifications.email")

EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


def validate_email_address(email: str, field_name: str = "Email") -> str:
    """Validate email address format and reject any control or header injection characters."""
    if not email or not isinstance(email, str):
        raise ValueError(f"{field_name} address cannot be empty.")

    # Strictly reject any CR, LF, or ASCII control characters
    if any(ord(c) < 32 or ord(c) == 127 for c in email):
        raise ValueError(
            f"Invalid {field_name} address: control or header injection characters detected."
        )

    cleaned = email.strip().lower()
    if not EMAIL_PATTERN.match(cleaned):
        raise ValueError(f"Invalid {field_name} address format: '{cleaned}'")
    return cleaned


def sanitize_header_value(value: str) -> str:
    """Strip CR, LF, and control characters from header values to prevent header injection."""
    if not value:
        return ""
    cleaned = re.sub(r"[\r\n\t]+", " ", str(value))
    cleaned = "".join(c for c in cleaned if ord(c) >= 32 and ord(c) != 127)
    return cleaned.strip()


@dataclass(frozen=True)
class SmtpFailureClassification:
    """Detailed diagnostic classification of an SMTP failure."""

    category: str  # "AMBIGUOUS", "TRANSIENT", "PERMANENT"
    is_ambiguous: bool
    can_retry: bool
    smtp_code: int | None
    description: str

    def __iter__(self):
        """Allow tuple unpacking (is_ambiguous, description) for backward compatibility."""
        return iter((self.is_ambiguous, self.description))


def classify_smtp_error(exc: Exception, phase: str | None = None) -> SmtpFailureClassification:
    """Distinguish ambiguous network outcomes, definite transient errors, and permanent rejections.

    Classification & Retry Policy:
    1. TRANSIENT (is_ambiguous=False, can_retry=True):
       Connection refused before handshake, DNS resolution failures, network unreachable during connect,
       or SMTP 4xx temporary response codes (e.g. 421 service not available, 450 mailbox busy, 452 storage full).
       The message was definitely not accepted. Bounded retries (up to 2 attempts) are permitted.
    2. AMBIGUOUS (is_ambiguous=True, can_retry=False):
       Network timeouts, unexpected connection disconnects during DATA transmission or after
       message submission, or socket resets during transfer. The server may have already accepted
       the message before the client lost the connection. Automatic retries are STRICTLY SUPPRESSED
       to guarantee at-most-once delivery.
    3. PERMANENT (is_ambiguous=False, can_retry=False):
       Authentication failures (535), 5xx permanent SMTP rejections (e.g. 550 recipient unknown,
       554 transaction failed, 501 syntax error), or invalid email formats. Retrying cannot succeed.
       Automatic retries are STRICTLY SUPPRESSED.
    """
    exc_type = exc.__class__.__name__
    current_phase = phase or getattr(exc, "smtp_phase", None)

    # 1. Definite transient connection / DNS errors before SMTP message transmission
    if isinstance(exc, (ConnectionRefusedError, socket.gaierror, socket.herror)):
        return SmtpFailureClassification(
            category="TRANSIENT",
            is_ambiguous=False,
            can_retry=True,
            smtp_code=None,
            description=f"Definite connection failure before SMTP transmission ({exc_type}): {exc}",
        )
    if isinstance(exc, smtplib.SMTPConnectError):
        code = getattr(exc, "smtp_code", 421)
        return SmtpFailureClassification(
            category="TRANSIENT",
            is_ambiguous=False,
            can_retry=True,
            smtp_code=code,
            description=f"Definite SMTP connection failure ({code}): {exc}",
        )

    # 2. Permanent authentication failure
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        code = getattr(exc, "smtp_code", 535)
        return SmtpFailureClassification(
            category="PERMANENT",
            is_ambiguous=False,
            can_retry=False,
            smtp_code=code,
            description=f"Permanent SMTP authentication failure ({code}): credentials rejected. Automatic retry suppressed.",
        )

    # 3. Recipient refusal: Inspect recipient SMTP response codes
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code = None
        if getattr(exc, "recipients", None):
            for _, err_tuple in exc.recipients.items():
                if isinstance(err_tuple, tuple) and len(err_tuple) > 0:
                    code = err_tuple[0]
                    break
        if code and 400 <= code < 500:
            return SmtpFailureClassification(
                category="TRANSIENT",
                is_ambiguous=False,
                can_retry=True,
                smtp_code=code,
                description=f"Transient SMTP recipient rejection ({code}): {exc}",
            )
        return SmtpFailureClassification(
            category="PERMANENT",
            is_ambiguous=False,
            can_retry=False,
            smtp_code=code or 550,
            description=f"Permanent SMTP recipient rejection ({code or 550}): {exc}. Automatic retry suppressed.",
        )

    # 4. Sender refusal / Data error / Response exceptions
    if isinstance(
        exc,
        (
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
            smtplib.SMTPResponseException,
        ),
    ):
        code = getattr(exc, "smtp_code", None)
        if code and 400 <= code < 500:
            return SmtpFailureClassification(
                category="TRANSIENT",
                is_ambiguous=False,
                can_retry=True,
                smtp_code=code,
                description=f"Transient SMTP response ({code}): {exc}",
            )
        return SmtpFailureClassification(
            category="PERMANENT",
            is_ambiguous=False,
            can_retry=False,
            smtp_code=code or 550,
            description=f"Permanent SMTP response ({code or 550}): {exc}. Automatic retry suppressed.",
        )

    # 5. Phase-aware connection errors:
    # If failure occurred strictly during pre-transmission phases (CONNECT, HANDSHAKE, STARTTLS, AUTH)
    if current_phase in ("CONNECT", "HANDSHAKE", "STARTTLS", "AUTH") and isinstance(
        exc, (OSError, smtplib.SMTPException)
    ):
        return SmtpFailureClassification(
            category="TRANSIENT",
            is_ambiguous=False,
            can_retry=True,
            smtp_code=getattr(exc, "smtp_code", None),
            description=f"Definite pre-transmission failure during {current_phase} ({exc_type}): {exc}",
        )

    # 6. Ambiguous outcomes during or after DATA transfer (or unknown phase):
    # Network timeout, disconnect during/after data transmission, or socket reset
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SmtpFailureClassification(
            category="AMBIGUOUS",
            is_ambiguous=True,
            can_retry=False,
            smtp_code=None,
            description=f"Ambiguous SMTP timeout ({exc_type}) during {current_phase or 'DATA'}: {exc}. Message may have been queued by relay. Automatic retry suppressed.",
        )
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return SmtpFailureClassification(
            category="AMBIGUOUS",
            is_ambiguous=True,
            can_retry=False,
            smtp_code=None,
            description=f"Ambiguous SMTP disconnect ({exc_type}): server closed connection during or after DATA transfer. Automatic retry suppressed.",
        )
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return SmtpFailureClassification(
            category="AMBIGUOUS",
            is_ambiguous=True,
            can_retry=False,
            smtp_code=None,
            description=f"Ambiguous socket error ({exc_type}) during or after DATA transfer: {exc}. Automatic retry suppressed.",
        )
    if isinstance(exc, OSError):
        # An unclassified OSError during DATA_TRANSFER or unknown phase is ambiguous
        return SmtpFailureClassification(
            category="AMBIGUOUS",
            is_ambiguous=True,
            can_retry=False,
            smtp_code=None,
            description=f"Ambiguous network error ({exc_type}) during {current_phase or 'DATA_TRANSFER'}: {exc}. Automatic retry suppressed.",
        )

    # 7. Default fallback for unknown socket/transmission errors: treat as ambiguous
    return SmtpFailureClassification(
        category="AMBIGUOUS",
        is_ambiguous=True,
        can_retry=False,
        smtp_code=None,
        description=f"Unclassified SMTP error ({exc_type}): {exc}. Automatic retry suppressed.",
    )


@dataclass(frozen=True)
class NotificationItemSummary:
    """Summary of an individual job notification evaluation or dispatch."""

    job_id: uuid.UUID
    title: str
    company_name: str
    role_priority: str
    eligibility_status: str
    recipient_email: str
    status: str
    error_message: str | None = None
    dashboard_url: str = ""
    application_url: str | None = None


@dataclass
class EmailDispatchBatchResult:
    """Aggregated outcome of an email notification batch execution."""

    total_candidates: int = 0
    total_sent: int = 0
    total_failed: int = 0
    total_skipped: int = 0
    is_dry_run: bool = False
    items: list[NotificationItemSummary] = field(default_factory=list)

    @property
    def total_delivered(self) -> int:
        """Backward-compatible alias for total_sent."""
        return self.total_sent


def format_salary_summary(job: Job) -> str:
    """Format human-readable salary summary preserving stated bounds."""
    if job.salary_min is not None and job.salary_max is not None:
        return f"{job.salary_currency or 'USD'} {job.salary_min:,.0f} - {job.salary_max:,.0f} ({job.salary_period or 'yr'})"
    if job.salary_min is not None:
        return f"From {job.salary_currency or 'USD'} {job.salary_min:,.0f} ({job.salary_period or 'yr'})"
    if job.salary_max is not None:
        return f"Up to {job.salary_currency or 'USD'} {job.salary_max:,.0f} ({job.salary_period or 'yr'})"
    return "Unstated"


def build_email_message(
    job: Job,
    recipient_email: str,
    sender_email: str | None = None,
    dashboard_base_url: str | None = None,
) -> EmailMessage:
    """Construct a multipart MIME email message with sanitized HTML and plain-text alternatives.

    All untrusted content is strictly escaped to prevent HTML injection and XSS.
    Headers are validated and sanitized to prevent email-header injection attacks.
    External ATS links are validated against approved http/https schemes.
    """
    settings = get_settings()
    valid_recipient = validate_email_address(recipient_email, "Recipient")
    valid_sender = validate_email_address(
        sender_email or settings.NOTIFICATION_SENDER_EMAIL, "Sender"
    )

    base_url = (dashboard_base_url or settings.DASHBOARD_BASE_URL).rstrip("/")
    dashboard_job_url = f"{base_url}/jobs/{job.id}"
    safe_ats_url = sanitize_url(job.application_url)

    # Safe escaped fields for HTML
    escaped_title = html.escape(job.title)
    escaped_company = html.escape(job.company_name)
    escaped_location = html.escape(job.location or "Location unstated")
    escaped_remote = html.escape((job.remote_type or "UNKNOWN").capitalize())
    escaped_priority = html.escape(job.role_priority)
    escaped_salary = html.escape(format_salary_summary(job))

    priority_color = "#059669" if job.role_priority == "P1" else "#2563eb"
    priority_label = "P1 Core Match" if job.role_priority == "P1" else "P2 Standard Match"

    clean_title = sanitize_header_value(job.title)
    clean_company = sanitize_header_value(job.company_name)
    clean_priority = sanitize_header_value(job.role_priority)

    msg = EmailMessage()
    msg["Subject"] = f"[{clean_priority}] New Job: {clean_title} at {clean_company}"
    msg["From"] = valid_sender
    msg["To"] = valid_recipient
    msg["Date"] = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")

    # Plain text version
    text_lines = [
        f"NEW JOB DISCOVERY: {job.title}",
        "=" * 60,
        f"Company:       {job.company_name}",
        f"Priority:      {job.role_priority} ({priority_label})",
        f"Workplace:     {job.remote_type.capitalize()}",
        f"Location:      {job.location or 'Location unstated'}",
        f"Compensation:  {format_salary_summary(job)}",
        f"Eligibility:   {job.eligibility_status}",
        "",
        f"View on Dashboard: {dashboard_job_url}",
    ]
    if safe_ats_url:
        text_lines.append(f"Apply on ATS:      {safe_ats_url}")
    text_lines.extend(
        [
            "",
            "---",
            "AI-Job-Intelligence Deterministic Notification Engine",
        ]
    )
    plain_text_content = "\n".join(text_lines)
    msg.set_content(plain_text_content)

    # HTML version with strict escaping and styled CTAs
    ats_button_html = ""
    if safe_ats_url:
        ats_button_html = f"""
        <a href="{safe_ats_url}" style="background-color: #4f46e5; color: #ffffff; padding: 10px 18px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 13px; display: inline-block; margin-right: 8px;">
            Apply on ATS &rarr;
        </a>
        """

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{escaped_title}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #0f172a; margin: 0; padding: 24px;">
    <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 16px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
        <div style="background-color: #0f172a; padding: 20px 24px; color: #ffffff;">
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; font-weight: 600;">AI-Job-Intelligence Alert</div>
            <h1 style="font-size: 20px; font-weight: bold; margin: 6px 0 0 0; color: #ffffff;">{escaped_title}</h1>
            <div style="font-size: 14px; color: #cbd5e1; margin-top: 4px;">at {escaped_company}</div>
        </div>

        <div style="padding: 24px;">
            <div style="margin-bottom: 20px;">
                <span style="background-color: {priority_color}; color: #ffffff; font-size: 11px; font-weight: bold; padding: 4px 10px; border-radius: 6px; display: inline-block;">
                    {escaped_priority} &bull; {priority_label}
                </span>
                <span style="background-color: #f1f5f9; color: #334155; font-size: 11px; font-weight: 600; padding: 4px 10px; border-radius: 6px; display: inline-block; margin-left: 6px;">
                    {escaped_remote}
                </span>
            </div>

            <table style="width: 100%; border-collapse: collapse; margin-bottom: 24px;">
                <tr>
                    <td style="padding: 8px 0; color: #64748b; font-size: 13px; width: 120px; font-weight: 500;">Location</td>
                    <td style="padding: 8px 0; color: #1e293b; font-size: 13px; font-weight: 600;">{escaped_location}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #64748b; font-size: 13px; font-weight: 500;">Compensation</td>
                    <td style="padding: 8px 0; color: #1e293b; font-size: 13px; font-weight: 600;">{escaped_salary}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #64748b; font-size: 13px; font-weight: 500;">Eligibility</td>
                    <td style="padding: 8px 0; color: #1e293b; font-size: 13px; font-weight: 600;">{html.escape(job.eligibility_status)}</td>
                </tr>
            </table>

            <div style="margin-top: 24px; padding-top: 20px; border-top: 1px solid #f1f5f9;">
                {ats_button_html}
                <a href="{dashboard_job_url}" style="background-color: #f1f5f9; color: #1e293b; padding: 10px 18px; text-decoration: none; border-radius: 8px; font-weight: bold; font-size: 13px; display: inline-block;">
                    Open in Dashboard &rarr;
                </a>
            </div>
        </div>

        <div style="background-color: #f8fafc; padding: 14px 24px; border-top: 1px solid #e2e8f0; font-size: 12px; color: #64748b; text-align: center;">
            AI-Job-Intelligence Notification Engine &bull; Deterministic Vacancy Match
        </div>
    </div>
</body>
</html>"""
    msg.add_alternative(html_content, subtype="html")
    return msg


def _send_smtp_payload(msg: EmailMessage) -> str:
    """Synchronous socket dispatch to configured SMTP host with phase tracking.

    Executed inside asyncio.to_thread to maintain asynchronous event loop responsiveness.
    Never logs SMTP credentials or passwords.
    """
    settings = get_settings()
    phase = "CONNECT"
    server = smtplib.SMTP(timeout=settings.SMTP_TIMEOUT_SECONDS)
    try:
        server.connect(settings.SMTP_HOST, settings.SMTP_PORT)
        phase = "HANDSHAKE"
        server.ehlo_or_helo_if_needed()
        if settings.SMTP_USE_TLS:
            phase = "STARTTLS"
            server.starttls()
            server.ehlo_or_helo_if_needed()
        if settings.SMTP_USERNAME and settings.SMTP_PASSWORD:
            phase = "AUTH"
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        phase = "DATA_TRANSFER"
        response = server.send_message(msg)
        phase = "COMPLETED"
        return str(response)
    except Exception as exc:
        exc.smtp_phase = phase
        raise
    finally:
        with contextlib.suppress(Exception):
            server.close()


class EmailNotificationService:
    """Coordinates email notification lifecycle, de-duplication, claiming, and SMTP dispatch."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = NotificationDeliveryRepository(session)
        self.settings = get_settings()

    async def dispatch_single_job_notification(
        self,
        job: Job,
        recipient_email: str | None = None,
        stale_claim_timeout_seconds: int = 600,
    ) -> NotificationDelivery | None:
        """Dispatch a single vacancy notification with atomic pre-send claim.

        Ensures at-most-once delivery attempt initiation. Catches all SMTP errors
        to prevent rolling back caller transactions.
        """
        raw_recipient = recipient_email or self.settings.NOTIFICATION_RECIPIENT_EMAIL
        if not raw_recipient:
            logger.warning("No notification recipient email configured. Skipping dispatch.")
            return None

        try:
            recipient = validate_email_address(raw_recipient, "Recipient")
        except ValueError as val_err:
            logger.error("Invalid recipient email '%s': %s", raw_recipient, val_err)
            return None

        # Check existing delivery record
        existing = await self.repo.get_by_job_recipient_channel(job.id, recipient, channel="EMAIL")
        if existing:
            if existing.status in ("SENT", "DELIVERED", "SMTP_ACCEPTED"):
                logger.info(
                    "Job %s already sent to %s. Skipping duplicate.",
                    job.id,
                    recipient,
                )
                return existing
            if existing.status == "CLAIMED":
                # Inspect whether claim is stale (e.g. prior process crashed)
                stale_threshold = datetime.now(UTC) - timedelta(seconds=stale_claim_timeout_seconds)
                if existing.claimed_at and existing.claimed_at < stale_threshold:
                    logger.warning(
                        "Stale claim found for job %s. Transitioning to FAILED (ambiguous).",
                        job.id,
                    )
                    existing = await self.repo.mark_failed(
                        existing,
                        error_message="Stale claim expired: process terminated or timed out before completion. Automatic retry suppressed.",
                        is_ambiguous=True,
                        can_retry=False,
                    )
                    await self.session.commit()
                    return existing
                logger.info("Job %s currently claimed by another process. Skipping.", job.id)
                return existing
            if existing.status == "FAILED":
                if existing.is_ambiguous:
                    logger.info(
                        "Job %s had ambiguous failure. Automatic retry suppressed.",
                        job.id,
                    )
                    return existing
                if not existing.can_retry:
                    logger.info(
                        "Job %s had permanent failure (can_retry=False). Automatic retry suppressed.",
                        job.id,
                    )
                    return existing
                if existing.retry_count >= 2:
                    logger.info(
                        "Job %s reached max retries (%d). Skipping.",
                        job.id,
                        existing.retry_count,
                    )
                    return existing

        # Global enable check: If disabled, do NOT create dummy claims or attempt dispatch
        if not self.settings.EMAIL_NOTIFICATIONS_ENABLED:
            logger.info(
                "EMAIL_NOTIFICATIONS_ENABLED is False. Skipping dispatch for job %s.",
                job.id,
            )
            return None

        # Step 1: Pre-send claim persistence (claim before opening SMTP socket)
        try:
            delivery = await self.repo.create_claim(
                job.id,
                recipient,
                channel="EMAIL",
                delivery_metadata={"smtp_host": self.settings.SMTP_HOST},
            )
            # Commit claim immediately to release lock and persist pre-send state
            await self.session.commit()
        except IntegrityError:
            # Race condition: Another worker concurrently claimed this composite key
            await self.session.rollback()
            existing = await self.repo.get_by_job_recipient_channel(
                job.id, recipient, channel="EMAIL"
            )
            logger.info(
                "Concurrent claim race detected for job %s; skipping duplicate dispatch.",
                job.id,
            )
            return existing

        # Step 2: Construct and validate email payload
        try:
            msg = build_email_message(
                job=job,
                recipient_email=recipient,
                sender_email=self.settings.NOTIFICATION_SENDER_EMAIL,
                dashboard_base_url=self.settings.DASHBOARD_BASE_URL,
            )
        except ValueError as build_err:
            logger.warning("Failed to construct email for job %s: %s", job.id, build_err)
            delivery = await self.repo.mark_failed(
                delivery,
                error_message=f"Email construction failed: {build_err}",
                is_ambiguous=False,
                can_retry=False,
            )
            await self.session.commit()
            return delivery

        # Step 3: Dispatch over SMTP
        try:
            smtp_response = await asyncio.to_thread(_send_smtp_payload, msg)
            delivery = await self.repo.mark_sent(delivery, message_id=smtp_response)
            await self.session.commit()
            logger.info(
                "Notification successfully sent (SMTP accepted) for job %s to %s",
                job.id,
                recipient,
            )
        except Exception as exc:
            cls = classify_smtp_error(exc)
            logger.warning(
                "SMTP failure for job %s (category=%s, can_retry=%s, code=%s): %s",
                job.id,
                cls.category,
                cls.can_retry,
                cls.smtp_code,
                cls.description,
            )
            delivery = await self.repo.mark_failed(
                delivery,
                error_message=cls.description,
                is_ambiguous=cls.is_ambiguous,
                can_retry=cls.can_retry,
                smtp_code=cls.smtp_code,
            )
            await self.session.commit()

        return delivery

    async def process_batch(
        self,
        *,
        recipient_email: str | None = None,
        limit: int = 20,
        dry_run: bool = False,
    ) -> EmailDispatchBatchResult:
        """Find pending eligible vacancies and execute preview or atomic dispatch."""
        raw_recipient = recipient_email or self.settings.NOTIFICATION_RECIPIENT_EMAIL
        result = EmailDispatchBatchResult(is_dry_run=dry_run)

        if not raw_recipient:
            logger.warning("Recipient email is empty. Cannot process notification batch.")
            return result

        try:
            recipient = validate_email_address(raw_recipient, "Recipient")
        except ValueError as val_err:
            logger.error("Invalid recipient email '%s': %s", raw_recipient, val_err)
            return result

        # Step 0: Sweep any stale claims from crashed processes
        try:
            await self.repo.sweep_stale_claims(stale_threshold_seconds=600)
            await self.session.commit()
        except Exception as sweep_err:
            logger.warning("Error sweeping stale claims: %s", sweep_err)

        candidates = await self.repo.find_eligible_jobs_pending_notification(
            recipient_email=recipient,
            channel="EMAIL",
            limit=limit,
        )
        result.total_candidates = len(candidates)

        for job in candidates:
            dashboard_url = f"{self.settings.DASHBOARD_BASE_URL.rstrip('/')}/jobs/{job.id}"
            if dry_run:
                result.items.append(
                    NotificationItemSummary(
                        job_id=job.id,
                        title=job.title,
                        company_name=job.company_name,
                        role_priority=job.role_priority,
                        eligibility_status=job.eligibility_status,
                        recipient_email=recipient,
                        status="PREVIEW_PENDING",
                        dashboard_url=dashboard_url,
                        application_url=sanitize_url(job.application_url),
                    )
                )
                result.total_skipped += 1
            else:
                if not self.settings.EMAIL_NOTIFICATIONS_ENABLED:
                    logger.info("EMAIL_NOTIFICATIONS_ENABLED is False. Skipping live dispatch.")
                    result.items.append(
                        NotificationItemSummary(
                            job_id=job.id,
                            title=job.title,
                            company_name=job.company_name,
                            role_priority=job.role_priority,
                            eligibility_status=job.eligibility_status,
                            recipient_email=recipient,
                            status="SKIPPED_DISABLED",
                            error_message="Email notifications disabled in application settings.",
                            dashboard_url=dashboard_url,
                            application_url=sanitize_url(job.application_url),
                        )
                    )
                    result.total_skipped += 1
                    continue

                delivery = await self.dispatch_single_job_notification(
                    job=job,
                    recipient_email=recipient,
                )
                if delivery and delivery.status in (
                    "SENT",
                    "DELIVERED",
                    "SMTP_ACCEPTED",
                ):
                    result.total_sent += 1
                    status_str = "SENT"
                else:
                    result.total_failed += 1
                    status_str = delivery.status if delivery else "FAILED"

                result.items.append(
                    NotificationItemSummary(
                        job_id=job.id,
                        title=job.title,
                        company_name=job.company_name,
                        role_priority=job.role_priority,
                        eligibility_status=job.eligibility_status,
                        recipient_email=recipient,
                        status=status_str,
                        error_message=delivery.error_message if delivery else "Unknown failure",
                        dashboard_url=dashboard_url,
                        application_url=sanitize_url(job.application_url),
                    )
                )

        return result
