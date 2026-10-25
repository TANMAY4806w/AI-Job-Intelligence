"""Job freshness validation, 5-day active window, and publication date plausibility.

Boundary: 100% deterministic rules evaluation.
Enforces frozen V1 freshness rules from rules/JOB_FILTERS_V1.md Section 7
and publication plausibility rules from rules/VERIFICATION_V1.md Dimension 8.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.rules.schemas import (
    REJECTION_JOB_TOO_OLD,
    REVIEW_POSTED_DATE_SUSPICIOUS,
)

FreshnessStatus = Literal["FRESH", "EXPIRED", "SUSPICIOUS"]

# Active feed window per Section 7 (0-5 days)
FRESHNESS_WINDOW = timedelta(days=5)

# Plausibility threshold: posted_at cannot be older than 90 days before collection
MAX_PLAUSIBLE_AGE = timedelta(days=90)


@dataclass(frozen=True)
class FreshnessEvaluation:
    """Evaluation outcome for job freshness criteria."""

    status: FreshnessStatus
    age: timedelta | None
    is_fresh: bool
    is_expired: bool
    is_suspicious: bool
    rejection_reason: str | None
    review_reason: str | None
    explanation: str


def evaluate_freshness(
    posted_at: datetime | None,
    collected_at: datetime | None = None,
    *,
    posting_date_verified: bool | None = None,
    reference_time: datetime | None = None,
) -> FreshnessEvaluation:
    """Evaluate job posting freshness and publication date plausibility.

    Guarantees:
        - Evaluated strictly against original publication timestamp (posted_at).
        - A recent-looking posted_at cannot make a job ELIGIBLE when publication date
          is unverified (False) or its verification state is unresolved (None).
        - Never fills missing posted_at with collected_at.
        - Missing, future, unverified, or implausible dates receive REVIEW with POSTED_DATE_SUSPICIOUS.
        - Verified postings older than 5 days receive EXPIRED with JOB_TOO_OLD.
        - Verified postings exactly on or within 5 days are active/fresh.

    Args:
        posted_at: Original advertised publication timestamp.
        collected_at: Timestamp when record was ingested into the system.
        posting_date_verified: Whether publication date authenticity has been verified (True),
                               failed (False), or is unresolved (None).
        reference_time: Reference timestamp for deterministic evaluation;
                        defaults to datetime.now(UTC).

    Returns:
        FreshnessEvaluation containing status, age, and reason codes.
    """
    now = reference_time or datetime.now(UTC)

    # 1. Missing publication date protocol (DATABASE_SCHEMA_V1.md & VERIFICATION_V1.md)
    if posted_at is None:
        return FreshnessEvaluation(
            status="SUSPICIOUS",
            age=None,
            is_fresh=False,
            is_expired=False,
            is_suspicious=True,
            rejection_reason=None,
            review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
            explanation="Original publication date (posted_at) is missing; routed to review",
        )

    # 2. Publication-date verification gating (VERIFICATION_V1.md Dimension 8)
    # A recent-looking posted_at cannot make a job ELIGIBLE when unverified or unresolved.
    if posting_date_verified is False:
        return FreshnessEvaluation(
            status="SUSPICIOUS",
            age=None,
            is_fresh=False,
            is_expired=False,
            is_suspicious=True,
            rejection_reason=None,
            review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
            explanation="Publication date failed verification; cannot establish active freshness",
        )

    if posting_date_verified is None:
        return FreshnessEvaluation(
            status="SUSPICIOUS",
            age=None,
            is_fresh=False,
            is_expired=False,
            is_suspicious=True,
            rejection_reason=None,
            review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
            explanation="Publication date verification is unresolved; cannot assume active freshness without verification",
        )

    # Ensure timezone awareness for comparison
    posted = posted_at if posted_at.tzinfo is not None else posted_at.replace(tzinfo=UTC)
    now_tz = now if now.tzinfo is not None else now.replace(tzinfo=UTC)

    # 3. Future publication date check
    if posted > now_tz:
        return FreshnessEvaluation(
            status="SUSPICIOUS",
            age=None,
            is_fresh=False,
            is_expired=False,
            is_suspicious=True,
            rejection_reason=None,
            review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
            explanation=f"Publication date ({posted.isoformat()}) is in the future relative to {now_tz.isoformat()}",
        )

    # 3. Plausibility check against collected_at if provided
    if collected_at is not None:
        collected = (
            collected_at if collected_at.tzinfo is not None else collected_at.replace(tzinfo=UTC)
        )
        if posted > collected:
            return FreshnessEvaluation(
                status="SUSPICIOUS",
                age=None,
                is_fresh=False,
                is_expired=False,
                is_suspicious=True,
                rejection_reason=None,
                review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
                explanation=(
                    f"Publication date ({posted.isoformat()}) is later than "
                    f"collection date ({collected.isoformat()})"
                ),
            )

        if posted < (collected - MAX_PLAUSIBLE_AGE):
            return FreshnessEvaluation(
                status="SUSPICIOUS",
                age=now_tz - posted,
                is_fresh=False,
                is_expired=False,
                is_suspicious=True,
                rejection_reason=None,
                review_reason=REVIEW_POSTED_DATE_SUSPICIOUS,
                explanation=(
                    f"Publication date ({posted.isoformat()}) exceeds 90-day plausibility "
                    f"window relative to collection date ({collected.isoformat()})"
                ),
            )

    # 4. 5-day active feed threshold check
    age = now_tz - posted

    # If posting exceeds 5-day freshness window (> 5 days) -> EXPIRED
    if age > FRESHNESS_WINDOW:
        return FreshnessEvaluation(
            status="EXPIRED",
            age=age,
            is_fresh=False,
            is_expired=True,
            is_suspicious=False,
            rejection_reason=REJECTION_JOB_TOO_OLD,
            review_reason=None,
            explanation=(
                f"Job posting age ({age.total_seconds() / 86400:.2f} days) exceeds "
                f"the 5-day active feed threshold"
            ),
        )

    # Within 5-day freshness window (<= 5 days) -> FRESH
    return FreshnessEvaluation(
        status="FRESH",
        age=age,
        is_fresh=True,
        is_expired=False,
        is_suspicious=False,
        rejection_reason=None,
        review_reason=None,
        explanation=f"Job posting is fresh ({age.total_seconds() / 86400:.2f} days old)",
    )
