"""Identity and duplicate resolution layer.

Executes the deterministic 3-stage deduplication hierarchy to merge cross-source
postings into canonical Jobs without loss of provenance.
Boundary: Resolves identity and provenance; does not evaluate job relevance or eligibility.
"""

from app.deduplication.matching import (
    STAGE_3_MAX_DAYS_WINDOW,
    check_date_proximity,
    evaluate_stage_3_candidate,
    normalize_datetime_tz,
)
from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationCandidate,
    DeduplicationResult,
    DeduplicationStage,
)
from app.deduplication.service import JobDeduplicationService

__all__ = [
    "STAGE_3_MAX_DAYS_WINDOW",
    "DeduplicationAction",
    "DeduplicationCandidate",
    "DeduplicationResult",
    "DeduplicationStage",
    "JobDeduplicationService",
    "check_date_proximity",
    "evaluate_stage_3_candidate",
    "normalize_datetime_tz",
]
