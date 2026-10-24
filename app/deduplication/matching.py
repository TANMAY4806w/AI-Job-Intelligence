"""Pure deterministic deduplication matching algorithms and proximity evaluations.

Boundary: In-memory evaluation of date proximity, location equivalency, and Stage 3
composite criteria. Completely decoupled from transport, databases, and I/O.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.deduplication.schemas import DeduplicationCandidate

if TYPE_CHECKING:
    from app.persistence.models.job import Job

# Authoritative Stage 3 publication date proximity window in days
STAGE_3_MAX_DAYS_WINDOW = 14


def normalize_datetime_tz(dt: datetime | None) -> datetime | None:
    """Ensure datetime is timezone-aware in UTC. Naive datetimes are assumed UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def check_date_proximity(
    date1: datetime | None,
    date2: datetime | None,
    max_days: int = STAGE_3_MAX_DAYS_WINDOW,
) -> bool | None:
    """Evaluate whether two publication timestamps are within max_days of each other.

    Args:
        date1: First publication timestamp.
        date2: Second publication timestamp.
        max_days: Maximum inclusive difference in days (default: 14 days).

    Returns:
        bool | None:
            True if both dates are present and difference <= max_days.
            False if both dates are present and difference > max_days.
            None if either date is None (indeterminate / missing evidence).
    """
    if date1 is None or date2 is None:
        return None

    d1 = normalize_datetime_tz(date1)
    d2 = normalize_datetime_tz(date2)
    assert d1 is not None and d2 is not None

    diff_seconds = abs((d1 - d2).total_seconds())
    max_seconds = max_days * 86400.0
    return diff_seconds <= max_seconds


def evaluate_stage_3_candidate(
    incoming: DeduplicationCandidate,
    candidate_job: "Job",
    max_days: int = STAGE_3_MAX_DAYS_WINDOW,
) -> tuple[str, dict[str, Any]]:
    """Evaluate an existing canonical Job against an incoming candidate under Stage 3 criteria.

    Stage 3 requires a strict conjunction of 4 normalized attributes:
    1. canonical company_id (exact match)
    2. normalized_title (exact match, preserving seniority distinctions)
    3. normalized_location (identical canonical city/region)
    4. publication date proximity (<= 14 days)

    Args:
        incoming: The candidate posting being evaluated.
        candidate_job: The existing canonical Job entity.
        max_days: The maximum allowed publication date delta in days (14).

    Returns:
        tuple[str, dict[str, Any]]:
            A status string: "MATCH", "MISMATCH", or "AMBIGUOUS".
            An evidence dictionary recording all evaluated attributes.
    """
    evidence: dict[str, Any] = {
        "candidate_job_id": str(candidate_job.id),
        "company_id_match": incoming.company_id == candidate_job.company_id,
        "title_match": incoming.normalized_title == candidate_job.normalized_title,
        "incoming_title": incoming.normalized_title,
        "candidate_title": candidate_job.normalized_title,
        "incoming_location": incoming.normalized_location,
        "candidate_location": candidate_job.normalized_location,
    }

    # 1. Company ID must match
    if not evidence["company_id_match"]:
        evidence["status"] = "MISMATCH"
        evidence["reason"] = "company_id_mismatch"
        return "MISMATCH", evidence

    # 2. Normalized title must match (preserves seniority distinctions)
    if not evidence["title_match"]:
        evidence["status"] = "MISMATCH"
        evidence["reason"] = "normalized_title_mismatch"
        return "MISMATCH", evidence

    # 3. Evaluate Date Proximity
    date_prox = check_date_proximity(incoming.posted_at, candidate_job.posted_at, max_days)
    evidence["date_proximity"] = date_prox

    if incoming.posted_at is not None and candidate_job.posted_at is not None:
        d1 = normalize_datetime_tz(incoming.posted_at)
        d2 = normalize_datetime_tz(candidate_job.posted_at)
        assert d1 is not None and d2 is not None
        diff_days = round(abs((d1 - d2).total_seconds()) / 86400.0, 2)
        evidence["date_difference_days"] = diff_days

    # If publication date strictly exceeds the 14-day window, reject as candidate
    if date_prox is False:
        evidence["status"] = "MISMATCH"
        evidence["reason"] = "date_exceeds_window"
        return "MISMATCH", evidence

    # 4. Evaluate Location Match
    loc_incoming = incoming.normalized_location
    loc_candidate = candidate_job.normalized_location

    if loc_incoming is not None and loc_candidate is not None:
        if loc_incoming != loc_candidate:
            evidence["location_match"] = False
            evidence["status"] = "MISMATCH"
            evidence["reason"] = "location_mismatch"
            return "MISMATCH", evidence
        else:
            evidence["location_match"] = True
    else:
        # One or both locations are unstated or unresolvable
        evidence["location_match"] = None

    # 5. Conjunction Analysis
    # If date is missing (indeterminate), but company and title matched:
    if date_prox is None:
        evidence["status"] = "AMBIGUOUS"
        evidence["reason"] = "missing_publication_date"
        return "AMBIGUOUS", evidence

    # Date is within window (date_prox is True)
    if evidence["location_match"] is True:
        evidence["status"] = "MATCH"
        evidence["reason"] = "all_criteria_satisfied"
        return "MATCH", evidence
    else:
        # Location was None on incoming or candidate
        evidence["status"] = "AMBIGUOUS"
        evidence["reason"] = "ambiguous_location"
        return "AMBIGUOUS", evidence
