"""Deterministic business rules and eligibility engine.

Enforces frozen V1 filtering criteria: role priority, experience ladder, location, and salary.
Boundary: 100% deterministic; zero network I/O, web scraping, or database queries.
"""

from app.rules.engine import (
    JobFilterRuleEngine,
    evaluate_job_filter,
)
from app.rules.experience import (
    ExperienceEvaluation,
    evaluate_experience,
)
from app.rules.freshness import (
    FreshnessEvaluation,
    evaluate_freshness,
)
from app.rules.location import (
    LocationEvaluation,
    evaluate_location,
)
from app.rules.roles import (
    RoleEvaluation,
    evaluate_role,
)
from app.rules.salary import (
    SalaryEvaluation,
    evaluate_salary,
)
from app.rules.schemas import (
    DEFAULT_RULE_VERSION,
    REJECTION_EXCLUDED_ROLE,
    REJECTION_EXPERIENCE_TOO_HIGH,
    REJECTION_JOB_TOO_OLD,
    REJECTION_LOCATION_NOT_ALLOWED,
    REJECTION_SALARY_BELOW_MINIMUM,
    REVIEW_COMPANY_UNVERIFIED,
    REVIEW_EXCLUSION_AMBIGUOUS,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_LOCATION_AMBIGUOUS,
    REVIEW_LOCATION_REMOTE_REVIEW,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
    REVIEW_SOURCE_CONFLICT_REVIEW,
    EligibilityStatus,
    JobFilterInput,
    JobFilterResult,
    RelevanceStatus,
    RolePriority,
)

__all__ = [
    "DEFAULT_RULE_VERSION",
    "EligibilityStatus",
    "ExperienceEvaluation",
    "FreshnessEvaluation",
    "JobFilterInput",
    "JobFilterResult",
    "JobFilterRuleEngine",
    "LocationEvaluation",
    "REJECTION_EXCLUDED_ROLE",
    "REJECTION_EXPERIENCE_TOO_HIGH",
    "REJECTION_JOB_TOO_OLD",
    "REJECTION_LOCATION_NOT_ALLOWED",
    "REJECTION_SALARY_BELOW_MINIMUM",
    "REVIEW_COMPANY_UNVERIFIED",
    "REVIEW_EXCLUSION_AMBIGUOUS",
    "REVIEW_EXPERIENCE_AMBIGUOUS",
    "REVIEW_LOCATION_AMBIGUOUS",
    "REVIEW_LOCATION_REMOTE_REVIEW",
    "REVIEW_POSTED_DATE_SUSPICIOUS",
    "REVIEW_SALARY_UNKNOWN",
    "REVIEW_SOURCE_CONFLICT_REVIEW",
    "RelevanceStatus",
    "RoleEvaluation",
    "RolePriority",
    "SalaryEvaluation",
    "evaluate_experience",
    "evaluate_freshness",
    "evaluate_job_filter",
    "evaluate_location",
    "evaluate_role",
    "evaluate_salary",
]
