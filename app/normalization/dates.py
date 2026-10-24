"""Deterministic publication date parsing.

Boundary: Standardizes source publication timestamps into timezone-aware datetimes.
Preserves NULL when publication date is absent or unknown per DATABASE_SCHEMA_V1.md.
Never fabricates dates using collection timestamps.
"""

from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from typing import Any

_COMMON_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
)


def parse_publication_date(raw_date: Any) -> datetime | None:
    """Parse and standardize publication timestamp into a timezone-aware datetime.

    Args:
        raw_date: Date representation (datetime, date, ISO string, RFC-2822 string,
                  or epoch seconds/milliseconds).

    Returns:
        datetime | None: Timezone-aware UTC datetime, or None if raw_date is None or empty.

    Raises:
        ValueError: If raw_date string or numeric value cannot be parsed into a valid date.
        TypeError: If raw_date is of an unsupported type (e.g., dict, list).
    """
    if raw_date is None:
        return None

    # Handle datetime objects
    if isinstance(raw_date, datetime):
        if raw_date.tzinfo is None:
            return raw_date.replace(tzinfo=UTC)
        return raw_date.astimezone(UTC)

    # Handle date objects (convert to midnight UTC)
    if isinstance(raw_date, date):
        return datetime.combine(raw_date, datetime.min.time(), tzinfo=UTC)

    # Handle numeric UNIX timestamps
    if isinstance(raw_date, (int, float)):
        if raw_date <= 0:
            raise ValueError(f"Invalid non-positive timestamp: {raw_date}")
        # Milliseconds if epoch > 1e11 (approx year 1973 in ms)
        seconds = raw_date / 1000.0 if raw_date > 1e11 else float(raw_date)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OverflowError, OSError) as err:
            raise ValueError(f"Invalid timestamp value: {raw_date}") from err

    # Handle string representations
    if isinstance(raw_date, str):
        cleaned = raw_date.strip()
        if not cleaned:
            return None

        # 1. Try ISO-8601 (handles Z, offsets, YYYY-MM-DD, etc.)
        try:
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            pass

        # 2. Try RFC-2822 (e.g. "Sat, 24 Oct 2026 12:00:00 +0000")
        try:
            dt = parsedate_to_datetime(cleaned)
            if dt is not None:
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
        except (ValueError, TypeError):
            pass

        # 3. Try common strptime formats
        for fmt in _COMMON_DATE_FORMATS:
            try:
                dt = datetime.strptime(cleaned, fmt)
                return dt.replace(tzinfo=UTC)
            except ValueError:
                continue

        raise ValueError(f"Unparseable publication date string: {raw_date!r}")

    raise TypeError(f"Unsupported publication date type '{type(raw_date).__name__}': {raw_date!r}")
