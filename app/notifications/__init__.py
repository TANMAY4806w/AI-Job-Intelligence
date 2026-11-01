"""Notification subsystem for AI-Job-Intelligence (Email Notifications V1)."""

from app.notifications.email_service import (
    EmailDispatchBatchResult,
    EmailNotificationService,
    NotificationItemSummary,
    SmtpFailureClassification,
    validate_email_address,
)

__all__ = [
    "EmailDispatchBatchResult",
    "EmailNotificationService",
    "NotificationItemSummary",
    "SmtpFailureClassification",
    "validate_email_address",
]
