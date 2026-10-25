"""Regional compensation evaluation, salary thresholds, and unknown salary protocol.

Boundary: 100% deterministic rules evaluation.
Enforces frozen V1 salary rules from rules/JOB_FILTERS_V1.md Section 6.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.rules.schemas import (
    REJECTION_SALARY_BELOW_MINIMUM,
    REVIEW_SALARY_UNKNOWN,
)

# Regional minimum thresholds in INR per annum per Section 6.1
PUNE_MINIMUM_ANNUAL = Decimal("360000.00")  # ₹3.6 LPA or ₹30,000 / month
BENGALURU_MINIMUM_ANNUAL = Decimal("500000.00")  # ₹5.0 LPA
HYDERABAD_MINIMUM_ANNUAL = Decimal("500000.00")  # ₹5.0 LPA

# Cities with no strict minimum threshold required per Section 6.1
NO_MINIMUM_HUBS: frozenset[str] = frozenset(
    {
        "mumbai",
        "navi mumbai",
        "thane",
    }
)


@dataclass(frozen=True)
class SalaryEvaluation:
    """Evaluation outcome for job compensation criteria."""

    is_eligible: bool
    is_review: bool
    is_rejected: bool
    threshold_annual: Decimal | None
    effective_annual: Decimal | None
    rejection_reason: str | None
    review_reason: str | None
    explanation: str


def _to_annual_inr(
    amount: Decimal | None,
    period: str | None,
    currency: str | None = "INR",
) -> Decimal | None:
    """Convert stated compensation amount to standardized annual INR equivalent."""
    if amount is None:
        return None

    dec_val = Decimal(str(amount))
    cur = (currency or "INR").upper().strip()

    # If foreign currency (e.g. USD, EUR, GBP), convert using conservative floor
    # Any standard full-time tech salary in USD/EUR easily exceeds Indian LPA minimums
    if cur in ("USD", "EUR", "GBP"):
        # Assume minimum 1 USD = 80 INR
        multiplier = Decimal("80.0") if cur == "USD" else Decimal("90.0")
        dec_val = dec_val * multiplier

    cadence = (period or "").upper().strip()

    if cadence == "MONTHLY":
        return dec_val * Decimal("12.0")
    if cadence == "HOURLY":
        return dec_val * Decimal("2080.0")

    # If period is ANNUAL or unspecified
    # Values < 100 are treated as LPA (e.g. 3.6 -> 360,000, 5.0 -> 500,000)
    if dec_val < Decimal("100.0"):
        return dec_val * Decimal("100000.00")

    return dec_val


def evaluate_salary(
    canonical_city: str | None,
    salary_min: Decimal | None = None,
    salary_max: Decimal | None = None,
    salary_currency: str | None = "INR",
    salary_period: str | None = None,
) -> SalaryEvaluation:
    """Evaluate stated compensation against regional salary minimums.

    Guarantees:
        - Mumbai / Navi Mumbai / Thane: No strict minimum required.
        - Pune: Minimum ₹30,000 / month (or equivalent ₹3.6 LPA).
        - Bengaluru: Minimum ₹5.0 LPA.
        - Hyderabad: Minimum ₹5.0 LPA.
        - Below threshold: REJECTED with SALARY_BELOW_MINIMUM.
        - Undisclosed / unknown salary: Never rejected; routes to REVIEW with SALARY_UNKNOWN.

    Args:
        canonical_city: Normalized canonical hub city (e.g. 'Pune', 'Bengaluru').
        salary_min: Minimum stated compensation.
        salary_max: Maximum stated compensation.
        salary_currency: Currency code (default: INR).
        salary_period: Compensation cadence (ANNUAL, MONTHLY, HOURLY).

    Returns:
        SalaryEvaluation containing status and reason codes.
    """
    city_lower = canonical_city.lower() if canonical_city else None

    # Determine regional threshold
    threshold: Decimal | None = None
    if city_lower in NO_MINIMUM_HUBS:
        threshold = None
    elif city_lower == "pune":
        threshold = PUNE_MINIMUM_ANNUAL
    elif city_lower in ("bengaluru", "hyderabad"):
        threshold = BENGALURU_MINIMUM_ANNUAL
    else:
        # Default for other locations if evaluated against Bangalore/Hyderabad standard
        threshold = None

    # Check for Unknown / Undisclosed Salary (Section 6.2)
    if salary_min is None and salary_max is None:
        return SalaryEvaluation(
            is_eligible=False,
            is_review=True,
            is_rejected=False,
            threshold_annual=threshold,
            effective_annual=None,
            rejection_reason=None,
            review_reason=REVIEW_SALARY_UNKNOWN,
            explanation="Salary is undisclosed or unknown; routed to review per maximum recall",
        )

    # Determine effective annual compensation
    annual_min = _to_annual_inr(salary_min, salary_period, salary_currency)
    annual_max = _to_annual_inr(salary_max, salary_period, salary_currency)

    # Effective compensation uses max if available, otherwise min
    effective = annual_max if annual_max is not None else annual_min

    # If city has no strict minimum threshold required (e.g. Mumbai, Navi Mumbai, Thane)
    if threshold is None:
        return SalaryEvaluation(
            is_eligible=True,
            is_review=False,
            is_rejected=False,
            threshold_annual=None,
            effective_annual=effective,
            rejection_reason=None,
            review_reason=None,
            explanation=f"Compensation disclosed (₹{effective:,.2f}/yr) in region with no strict minimum: {canonical_city}",
        )

    # Check against regional minimum threshold
    # If effective annual compensation is below threshold -> REJECTED
    if effective is not None and effective < threshold:
        return SalaryEvaluation(
            is_eligible=False,
            is_review=False,
            is_rejected=True,
            threshold_annual=threshold,
            effective_annual=effective,
            rejection_reason=REJECTION_SALARY_BELOW_MINIMUM,
            review_reason=None,
            explanation=(
                f"Disclosed compensation (₹{effective:,.2f}/yr) is below "
                f"regional minimum of ₹{threshold:,.2f}/yr for {canonical_city}"
            ),
        )

    # Stated compensation meets or exceeds regional threshold
    return SalaryEvaluation(
        is_eligible=True,
        is_review=False,
        is_rejected=False,
        threshold_annual=threshold,
        effective_annual=effective,
        rejection_reason=None,
        review_reason=None,
        explanation=(
            f"Disclosed compensation (₹{effective:,.2f}/yr) meets "
            f"regional minimum of ₹{threshold:,.2f}/yr for {canonical_city}"
        ),
    )
