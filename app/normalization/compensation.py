"""Compensation parsing and standardization.

Boundary: Standardizes salary disclosures into numeric bounds, currency (default INR),
and cadence (ANNUAL, MONTHLY, HOURLY) per DATABASE_SCHEMA_V1.md and JOB_FILTERS_V1.md.
Preserves NULL when compensation is undisclosed.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# Pattern matching LPA (Lakhs Per Annum): "3.6 LPA", "6 - 10 Lakhs", "5.0 lacs per annum"
_LPA_RANGE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?)(?:\s*(?:per\s+annum|pa|p\.a\.))?",
    re.IGNORECASE,
)
_LPA_SINGLE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?|lacs?)(?:\s*(?:per\s+annum|pa|p\.a\.))?",
    re.IGNORECASE,
)

# Pattern matching Monthly figures: "₹30,000 / month", "30k pm", "30,000 per month"
_MONTHLY_PATTERN = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*|\d+k)\s*(?:/|per)?\s*(?:month|pm|mo)\b",
    re.IGNORECASE,
)

# Cadence mappings
_CADENCE_MAP = {
    "annual": "ANNUAL",
    "yearly": "ANNUAL",
    "year": "ANNUAL",
    "pa": "ANNUAL",
    "p.a.": "ANNUAL",
    "lpa": "ANNUAL",
    "monthly": "MONTHLY",
    "month": "MONTHLY",
    "pm": "MONTHLY",
    "p.m.": "MONTHLY",
    "hourly": "HOURLY",
    "hour": "HOURLY",
    "hr": "HOURLY",
}


def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        # Strip commas and currency symbols if string
        if isinstance(val, str):
            clean = re.sub(r"[^\d.]", "", val)
            return Decimal(clean) if clean else None
        return Decimal(str(val))
    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_compensation(
    salary_min: Any = None,
    salary_max: Any = None,
    currency: str | None = None,
    period: str | None = None,
    raw_text: str | None = None,
) -> tuple[Decimal | None, Decimal | None, str | None, str | None]:
    """Parse or standardize compensation attributes into Decimal bounds.

    Args:
        salary_min: Pre-parsed minimum salary.
        salary_max: Pre-parsed maximum salary.
        currency: ISO-4217 currency code (e.g., 'INR', 'USD').
        period: Pay cadence ('ANNUAL', 'MONTHLY', 'HOURLY', 'UNKNOWN').
        raw_text: Stated salary string (e.g., '₹3.6 LPA', '₹30,000 / month').

    Returns:
        tuple[Decimal | None, Decimal | None, str | None, str | None]:
            - salary_min: Standardized minimum amount.
            - salary_max: Standardized maximum amount.
            - salary_currency: Currency code (defaults to 'INR' when salary present).
            - salary_period: Standardized period ('ANNUAL', 'MONTHLY', 'HOURLY', 'UNKNOWN').
    """
    min_dec = _to_decimal(salary_min)
    max_dec = _to_decimal(salary_max)

    # Standardize cadence if passed
    clean_period: str | None = None
    if period:
        clean_period = _CADENCE_MAP.get(period.lower().strip(), period.upper().strip())

    clean_currency: str | None = currency.upper().strip() if currency else None

    # If numeric values already given, return them
    if min_dec is not None or max_dec is not None:
        if not clean_currency:
            clean_currency = "INR"
        return min_dec, max_dec, clean_currency, clean_period

    if not raw_text or not isinstance(raw_text, str):
        return None, None, None, None

    text = raw_text.strip()
    if not text:
        return None, None, None, None

    # 1. Parse LPA range (e.g., "6 - 10 LPA")
    lpa_range = _LPA_RANGE_PATTERN.search(text)
    if lpa_range:
        try:
            v1 = Decimal(lpa_range.group(1)) * Decimal("100000.00")
            v2 = Decimal(lpa_range.group(2)) * Decimal("100000.00")
            return min(v1, v2), max(v1, v2), clean_currency or "INR", "ANNUAL"
        except InvalidOperation:
            pass

    # 2. Parse LPA single (e.g., "3.6 LPA")
    lpa_single = _LPA_SINGLE_PATTERN.search(text)
    if lpa_single:
        try:
            v = Decimal(lpa_single.group(1)) * Decimal("100000.00")
            return v, v, clean_currency or "INR", "ANNUAL"
        except InvalidOperation:
            pass

    # 3. Parse Monthly string (e.g., "₹30,000 / month")
    monthly = _MONTHLY_PATTERN.search(text)
    if monthly:
        try:
            raw_val = monthly.group(1).lower().replace(",", "")
            if raw_val.endswith("k"):
                val = Decimal(raw_val[:-1]) * Decimal("1000.00")
            else:
                val = Decimal(raw_val)
            return val, val, clean_currency or "INR", "MONTHLY"
        except InvalidOperation:
            pass

    return None, None, None, None
