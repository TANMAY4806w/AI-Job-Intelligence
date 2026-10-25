"""Experience ladder validation, fresher rules, and seniority exclusion.

Boundary: 100% deterministic rules evaluation.
Enforces frozen V1 experience rules from rules/JOB_FILTERS_V1.md Section 4.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from app.rules.schemas import (
    REJECTION_EXPERIENCE_TOO_HIGH,
    REVIEW_EXPERIENCE_AMBIGUOUS,
)

# Clearly Senior / Lead / Architect / Management titles per Section 4 table
_SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|lead|principal|staff|architect|director|head\s+of|vp|manager)\b",
    re.IGNORECASE,
)

# Fresher and entry-level textual indicators
_FRESHER_PATTERN = re.compile(
    r"\b(?:fresher|freshers|entry[\s-]level|intern|internship|trainee|graduate|0\s*years?)\b",
    re.IGNORECASE,
)

# Explicit target experience patterns in text: 0-1, 0-2, 1-2
_TARGET_EXP_PATTERN = re.compile(
    r"\b(?:0\s*[-–to]\s*1|0\s*[-–to]\s*2|1\s*[-–to]\s*2)\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

# Ambiguous 1-3 years pattern
_ONE_TO_THREE_PATTERN = re.compile(
    r"\b1\s*[-–to]\s*3\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

# Excluded experience patterns: min >= 2 years (2-3, 2-4, 2-5, 3+, 2+, etc.)
_EXCLUDED_EXP_PATTERN = re.compile(
    r"\b(?:[2-9]|\d{2})\s*(?:\+|plus)\s*(?:years?|yrs?)?\b|"
    r"\b(?:2|3|4|5|6|7|8|9|\d{2})\s*[-–to]\s*\d+\s*(?:years?|yrs?)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExperienceEvaluation:
    """Evaluation outcome for job experience criteria."""

    is_eligible: bool
    is_review: bool
    is_rejected: bool
    min_years: Decimal | None
    max_years: Decimal | None
    rejection_reason: str | None
    review_reason: str | None
    explanation: str


def evaluate_experience(
    title: str,
    experience_min_years: Decimal | None = None,
    experience_max_years: Decimal | None = None,
    experience_raw: str | None = None,
) -> ExperienceEvaluation:
    """Evaluate experience requirements against approved fresher and experience bands.

    Guarantees:
        - Rejects roles where minimum experience >= 2.0 years.
        - Rejects clearly Senior/Lead/Architect roles based on title.
        - Routes overlapping 1-3 years band to REVIEW with EXPERIENCE_AMBIGUOUS.
        - Keeps Fresher, Entry-level, 0-1, 0-2, 1-2 years as ELIGIBLE.
        - Routes missing or ambiguous experience to REVIEW; never assumes missing is zero.

    Args:
        title: Advertised job title.
        experience_min_years: Extracted minimum required experience in years.
        experience_max_years: Extracted maximum required experience in years.
        experience_raw: Raw stated experience text.

    Returns:
        ExperienceEvaluation containing eligibility status and reason codes.
    """
    # 1. Check title for Senior / Lead / Architect / Management keywords
    if _SENIOR_TITLE_PATTERN.search(title):
        return ExperienceEvaluation(
            is_eligible=False,
            is_review=False,
            is_rejected=True,
            min_years=experience_min_years,
            max_years=experience_max_years,
            rejection_reason=REJECTION_EXPERIENCE_TOO_HIGH,
            review_reason=None,
            explanation=f"Role title indicates Senior/Lead/Architect seniority: '{title}'",
        )

    # 2. Evaluate numeric experience bounds if minimum years is known
    if experience_min_years is not None:
        min_y = Decimal(str(experience_min_years))
        max_y = Decimal(str(experience_max_years)) if experience_max_years is not None else None

        # Rule 4.1: If stated experience range starts at 2 years or more (min >= 2.0) -> EXCLUDE
        if min_y >= Decimal("2.0"):
            return ExperienceEvaluation(
                is_eligible=False,
                is_review=False,
                is_rejected=True,
                min_years=min_y,
                max_years=max_y,
                rejection_reason=REJECTION_EXPERIENCE_TOO_HIGH,
                review_reason=None,
                explanation=f"Stated minimum experience ({min_y} years) is >= 2.0 years threshold",
            )

        # Overlapping 1-3 years band (or any band with upper bound > 2.0 years) -> REVIEW
        if min_y == Decimal("1.0") and max_y is not None and max_y == Decimal("3.0"):
            return ExperienceEvaluation(
                is_eligible=False,
                is_review=True,
                is_rejected=False,
                min_years=min_y,
                max_years=max_y,
                rejection_reason=None,
                review_reason=REVIEW_EXPERIENCE_AMBIGUOUS,
                explanation="Experience requirement is in ambiguous overlapping 1-3 years band",
            )

        if max_y is not None and max_y > Decimal("2.0"):
            return ExperienceEvaluation(
                is_eligible=False,
                is_review=True,
                is_rejected=False,
                min_years=min_y,
                max_years=max_y,
                rejection_reason=None,
                review_reason=REVIEW_EXPERIENCE_AMBIGUOUS,
                explanation=f"Experience upper bound ({max_y} years) exceeds target 2-year window",
            )

        # Target bands: 0-1, 0-2, 1-2, or min < 2 with max <= 2 (or max unknown)
        return ExperienceEvaluation(
            is_eligible=True,
            is_review=False,
            is_rejected=False,
            min_years=min_y,
            max_years=max_y,
            rejection_reason=None,
            review_reason=None,
            explanation=f"Experience requirement ({min_y}-{max_y or min_y} years) satisfies target criteria",
        )

    # 3. Evaluate raw experience text if numeric bound was not parsed
    if experience_raw and isinstance(experience_raw, str):
        clean_raw = experience_raw.strip().lower()

        # Check Fresher / Entry-level text
        if _FRESHER_PATTERN.search(clean_raw):
            return ExperienceEvaluation(
                is_eligible=True,
                is_review=False,
                is_rejected=False,
                min_years=Decimal("0.0"),
                max_years=Decimal("0.0"),
                rejection_reason=None,
                review_reason=None,
                explanation=f"Stated experience indicates fresher/entry level: '{experience_raw}'",
            )

        # Check explicit target patterns (0-1, 0-2, 1-2)
        if _TARGET_EXP_PATTERN.search(clean_raw):
            return ExperienceEvaluation(
                is_eligible=True,
                is_review=False,
                is_rejected=False,
                min_years=None,
                max_years=None,
                rejection_reason=None,
                review_reason=None,
                explanation=f"Stated experience matches target band: '{experience_raw}'",
            )

        # Check ambiguous 1-3 years
        if _ONE_TO_THREE_PATTERN.search(clean_raw):
            return ExperienceEvaluation(
                is_eligible=False,
                is_review=True,
                is_rejected=False,
                min_years=Decimal("1.0"),
                max_years=Decimal("3.0"),
                rejection_reason=None,
                review_reason=REVIEW_EXPERIENCE_AMBIGUOUS,
                explanation=f"Stated experience is in ambiguous 1-3 years band: '{experience_raw}'",
            )

        # Check excluded patterns (min >= 2 years)
        if _EXCLUDED_EXP_PATTERN.search(clean_raw):
            return ExperienceEvaluation(
                is_eligible=False,
                is_review=False,
                is_rejected=True,
                min_years=None,
                max_years=None,
                rejection_reason=REJECTION_EXPERIENCE_TOO_HIGH,
                review_reason=None,
                explanation=f"Stated experience requires >= 2 years: '{experience_raw}'",
            )

    # 4. Ambiguous or Unstated Experience (Rule 4.2)
    # Never assume missing experience means zero experience.
    return ExperienceEvaluation(
        is_eligible=False,
        is_review=True,
        is_rejected=False,
        min_years=None,
        max_years=None,
        rejection_reason=None,
        review_reason=REVIEW_EXPERIENCE_AMBIGUOUS,
        explanation="Experience requirement is missing, unspecified, or ambiguous",
    )
