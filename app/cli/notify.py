"""Email notification CLI command handler.

Boundary: Dispatches or previews outbound email alerts for newly discovered,
relevant vacancies meeting P1/P2 priority and ELIGIBLE/REVIEW criteria.
Strict Safety:
1. --dry-run: Preview-only mode with 0 network calls and 0 database writes.
2. --send: Dispatches notifications with pre-send claim persistence to prevent duplicates.
3. Isolated execution: Notification failures never affect stored jobs or collection state.
4. Notifications disabled by default; --send cannot bypass the disabled setting.
"""

import argparse
import json
import logging
import sys
from typing import Any

from app.config import get_settings
from app.notifications.email_service import (
    EmailDispatchBatchResult,
    EmailNotificationService,
    validate_email_address,
)
from app.persistence.session import session_scope

logger = logging.getLogger("app.cli.notify")


def format_notify_summary(result: EmailDispatchBatchResult, recipient: str) -> str:
    """Format human-readable tabular summary of notification batch."""
    mode_str = "PLAN-ONLY DRY RUN (PREVIEW)" if result.is_dry_run else "LIVE DISPATCH"
    lines = [
        "=" * 80,
        f"EMAIL NOTIFICATION BATCH SUMMARY — {mode_str}",
        "=" * 80,
        f"Target Recipient:    {recipient}",
        f"Eligible Candidates: {result.total_candidates}",
    ]
    if result.is_dry_run:
        lines.append(f"Previewed (Skipped): {result.total_skipped}")
    else:
        lines.append(f"Successfully Sent:   {result.total_sent}")
        lines.append(f"Failed / Deferred:   {result.total_failed}")

    lines.append("-" * 80)
    lines.append(f"{'PRIORITY':<10} {'COMPANY':<18} {'STATUS':<16} {'TITLE':<32}")
    lines.append("-" * 80)

    for item in result.items:
        lines.append(
            f"{item.role_priority:<10} {item.company_name[:17]:<18} {item.status[:15]:<16} {item.title[:30]:<32}"
        )
        if item.error_message:
            lines.append(f"   └─ Error: {item.error_message}")
        if item.dashboard_url:
            lines.append(f"   └─ Dashboard: {item.dashboard_url}")

    lines.append("=" * 80)
    lines.append(
        "Note: Status SENT indicates acceptance by upstream SMTP relay (250 OK).\n"
        "Acceptance confirms message handoff but does not guarantee inbox delivery."
    )
    lines.append("=" * 80)
    return "\n".join(lines)


async def run_notify(args: argparse.Namespace) -> int:
    """Async entrypoint executing notify preview or send workflow."""
    settings = get_settings()
    raw_recipient = args.recipient or settings.NOTIFICATION_RECIPIENT_EMAIL

    if not raw_recipient:
        sys.stderr.write(
            "Error: No notification recipient email provided.\n"
            "Specify --recipient <email> or set NOTIFICATION_RECIPIENT_EMAIL in your environment/.env.\n"
        )
        return 1

    try:
        recipient = validate_email_address(raw_recipient, "Recipient")
    except ValueError as val_err:
        sys.stderr.write(f"Error: {val_err}\n")
        return 1

    # Security Guard: --send cannot bypass EMAIL_NOTIFICATIONS_ENABLED=False
    if args.send and not settings.EMAIL_NOTIFICATIONS_ENABLED:
        sys.stderr.write(
            "Error: Email notifications are disabled in application settings (EMAIL_NOTIFICATIONS_ENABLED=False).\n"
            "Execution with --send is refused. Set EMAIL_NOTIFICATIONS_ENABLED=true in environment/.env to dispatch.\n"
        )
        return 1

    dry_run = not args.send  # Default to dry-run unless --send is explicitly given

    async with session_scope() as session:
        service = EmailNotificationService(session)
        batch_result = await service.process_batch(
            recipient_email=recipient,
            limit=args.limit,
            dry_run=dry_run,
        )

    if getattr(args, "json", False):
        summary_payload: dict[str, Any] = {
            "is_dry_run": batch_result.is_dry_run,
            "recipient": recipient,
            "total_candidates": batch_result.total_candidates,
            "total_sent": batch_result.total_sent,
            "total_delivered": batch_result.total_delivered,
            "total_failed": batch_result.total_failed,
            "total_skipped": batch_result.total_skipped,
            "items": [
                {
                    "job_id": str(item.job_id),
                    "title": item.title,
                    "company_name": item.company_name,
                    "role_priority": item.role_priority,
                    "eligibility_status": item.eligibility_status,
                    "status": item.status,
                    "error_message": item.error_message,
                    "dashboard_url": item.dashboard_url,
                    "application_url": item.application_url,
                }
                for item in batch_result.items
            ],
        }
        print(json.dumps(summary_payload, indent=2))
    else:
        print(format_notify_summary(batch_result, recipient))

    return 0


def execute_notify_command(args: argparse.Namespace) -> int:
    """Synchronous bridge to the async notify runner."""
    import asyncio

    try:
        return asyncio.run(run_notify(args))
    except Exception as exc:
        logger.error("Notify command failed with unexpected error: %s", exc, exc_info=True)
        sys.stderr.write(f"Error: {exc}\n")
        return 1
