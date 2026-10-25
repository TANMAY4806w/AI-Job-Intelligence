"""Authenticity and verification layer.

Boundary: 100% pure evaluation engine and typed schemas.
Evaluates the eight independent verification dimensions and computes composite confidence scores.
Enforces rules/VERIFICATION_V1.md and rules/JOB_FILTERS_V1.md.
Independent of SQLAlchemy, persistence repositories, network I/O, scraping, and LLMs.
"""

from app.verification.evaluator import JobVerificationEvaluator
from app.verification.schemas import (
    FAILURE_JD_EXPIRED,
    FAILURE_JD_NOT_FOUND,
    FAILURE_SOURCE_INVALID,
    REVIEW_APPLICATION_LINK_BROKEN,
    REVIEW_COMPANY_UNVERIFIED,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_LOCATION_AMBIGUOUS,
    REVIEW_LOCATION_REMOTE_REVIEW,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
    REVIEW_SOURCE_CONFLICT_REVIEW,
    ConfidenceTier,
    DimensionResult,
    JobVerificationInput,
    JobVerificationResult,
)

__all__ = [
    "ConfidenceTier",
    "DimensionResult",
    "FAILURE_JD_EXPIRED",
    "FAILURE_JD_NOT_FOUND",
    "FAILURE_SOURCE_INVALID",
    "JobVerificationEvaluator",
    "JobVerificationInput",
    "JobVerificationResult",
    "REVIEW_APPLICATION_LINK_BROKEN",
    "REVIEW_COMPANY_UNVERIFIED",
    "REVIEW_EXPERIENCE_AMBIGUOUS",
    "REVIEW_LOCATION_AMBIGUOUS",
    "REVIEW_LOCATION_REMOTE_REVIEW",
    "REVIEW_POSTED_DATE_SUSPICIOUS",
    "REVIEW_SALARY_UNKNOWN",
    "REVIEW_SOURCE_CONFLICT_REVIEW",
]
