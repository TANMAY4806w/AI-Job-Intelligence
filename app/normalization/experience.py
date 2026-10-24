"""Experience requirement parsing and standardization.

Boundary: Extracts numeric minimum and maximum experience years from raw text
or structured values to support the 0-2 year target filtering rules in JOB_FILTERS_V1.md.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Match ranges: "0-2 years", "1 to 3 yrs", "0 - 1 yr", "0–2 years"
_RANGE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?|yoe)?",
    re.IGNORECASE,
)

# Match plus / minimum: "2+ years", "at least 3 yrs", "minimum 1 year"
_PLUS_PATTERN = re.compile(
    r"(?:(?:at\s+least|min(?:imum)?)\s+)?(\d+(?:\.\d+)?)\s*(?:\+|\s*plus)\s*(?:yrs?|years?|yoe)?",
    re.IGNORECASE,
)
_MIN_ONLY_PATTERN = re.compile(
    r"(?:at\s+least|min(?:imum)?)\s+(\d+(?:\.\d+)?)\s*(?:yrs?|years?|yoe)?",
    re.IGNORECASE,
)


def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        return Decimal(str(val).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_experience(
    raw_exp: str | None = None,
    min_years: Any = None,
    max_years: Any = None,
) -> tuple[str | None, Decimal | None, Decimal | None]:
    """Parse raw experience string or validate structured experience bounds.

    Args:
        raw_exp: Stated experience text (e.g., '0-2 years', 'Fresher', '1 to 3 yrs').
        min_years: Pre-parsed minimum experience in years if available.
        max_years: Pre-parsed maximum experience in years if available.

    Returns:
        tuple[str | None, Decimal | None, Decimal | None]:
            - experience_raw: Cleaned raw experience text.
            - experience_min_years: Numeric minimum experience as Decimal (or None).
            - experience_max_years: Numeric maximum experience as Decimal (or None).
    """
    cleaned_raw = raw_exp.strip() if raw_exp and isinstance(raw_exp, str) else None
    if cleaned_raw == "":
        cleaned_raw = None

    # If numeric values already supplied, prioritize them
    parsed_min = _to_decimal(min_years)
    parsed_max = _to_decimal(max_years)
    if parsed_min is not None or parsed_max is not None:
        return cleaned_raw, parsed_min, parsed_max

    if not cleaned_raw:
        return None, None, None

    lower = cleaned_raw.lower()

    # 1. Fresher / Entry-level / Intern
    if any(
        term in lower for term in ("fresher", "entry-level", "entry level", "intern", "trainee")
    ):
        return cleaned_raw, Decimal("0.0"), Decimal("0.0")

    # 2. Explicit range (e.g., "0-2 years", "1 to 3 yrs")
    range_match = _RANGE_PATTERN.search(cleaned_raw)
    if range_match:
        try:
            val1 = Decimal(range_match.group(1))
            val2 = Decimal(range_match.group(2))
            return cleaned_raw, min(val1, val2), max(val1, val2)
        except InvalidOperation:
            pass

    # 3. Plus pattern (e.g., "2+ years", "3+ yrs")
    plus_match = _PLUS_PATTERN.search(cleaned_raw)
    if plus_match:
        try:
            val = Decimal(plus_match.group(1))
            return cleaned_raw, val, None
        except InvalidOperation:
            pass

    # 4. Minimum-only pattern (e.g., "minimum 2 years", "at least 1 yr")
    min_match = _MIN_ONLY_PATTERN.search(cleaned_raw)
    if min_match:
        try:
            val = Decimal(min_match.group(1))
            return cleaned_raw, val, None
        except InvalidOperation:
            pass

    # Ambiguous or non-numeric description ("relevant experience required")
    return cleaned_raw, None, None
