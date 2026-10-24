"""Deterministic location normalization and remote classification.

Boundary: Standardizes location strings to canonical Western/Southern Indian tech
hubs (P1: Mumbai, Navi Mumbai, Thane, Pune; P2: Bengaluru, Hyderabad) per
JOB_FILTERS_V1.md and DATABASE_SCHEMA_V1.md. Preserves NULL for missing or
ambiguous locations according to the Maximum Recall Principle.
"""

import re

# P1 and P2 target metropolitan tech hubs per JOB_FILTERS_V1.md Section 5.1
CANONICAL_TARGET_HUBS: list[tuple[str, list[str]]] = [
    (
        "Navi Mumbai",
        [
            r"\bnavi\s+mumbai\b",
            r"\bnew\s+bombay\b",
        ],
    ),
    (
        "Mumbai",
        [
            r"\bmumbai\b",
            r"\bbombay\b",
            r"\bmumbai\s+city\b",
            r"\bmumbai\s+suburban\b",
        ],
    ),
    (
        "Thane",
        [
            r"\bthane\b",
            r"\bthane\s+west\b",
            r"\bthane\s+east\b",
        ],
    ),
    (
        "Pune",
        [
            r"\bpune\b",
            r"\bpoona\b",
            r"\bhinjewadi\b",
            r"\bmagarpatta\b",
        ],
    ),
    (
        "Bengaluru",
        [
            r"\bbengaluru\b",
            r"\bbangalore\b",
            r"\bblr\b",
            r"\bwhitefield\b",
            r"\belectronic\s+city\b",
            r"\bbellandur\b",
        ],
    ),
    (
        "Hyderabad",
        [
            r"\bhyderabad\b",
            r"\bsecunderabad\b",
            r"\bcyberabad\b",
            r"\bhyd\b",
            r"\bgachibowli\b",
            r"\bhitec\s+city\b",
        ],
    ),
]

# Known non-target cities to distinguish known out-of-scope locations (LOCATION_NOT_ALLOWED)
# from missing or ambiguous locations (LOCATION_AMBIGUOUS) per JOB_FILTERS_V1.md Section 5.2.
KNOWN_NON_TARGET_CITIES: list[tuple[str, list[str]]] = [
    (
        "Delhi",
        [
            r"\bnew\s+delhi\b",
            r"\bdelhi\s+ncr\b",
            r"\bdelhi\b",
        ],
    ),
    (
        "Noida",
        [
            r"\bgreater\s+noida\b",
            r"\bnoida\b",
        ],
    ),
    (
        "Gurugram",
        [
            r"\bgurugram\b",
            r"\bgurgaon\b",
        ],
    ),
    (
        "Chennai",
        [
            r"\bchennai\b",
            r"\bmadras\b",
        ],
    ),
    (
        "Kolkata",
        [
            r"\bkolkata\b",
            r"\bcalcutta\b",
        ],
    ),
    (
        "Ahmedabad",
        [
            r"\bahmedabad\b",
        ],
    ),
    (
        "Chandigarh",
        [
            r"\bchandigarh\b",
            r"\bmohali\b",
            r"\bpanchkula\b",
        ],
    ),
    (
        "Jaipur",
        [
            r"\bjaipur\b",
        ],
    ),
    (
        "Kochi",
        [
            r"\bkochi\b",
            r"\bcochin\b",
            r"\btrivandrum\b",
            r"\bthiruvananthapuram\b",
        ],
    ),
    (
        "Indore",
        [
            r"\bindore\b",
        ],
    ),
    (
        "Coimbatore",
        [
            r"\bcoimbatore\b",
        ],
    ),
]

# Set of target hub names for fast lookup
TARGET_HUB_NAMES = {name for name, _ in CANONICAL_TARGET_HUBS}
CANONICAL_HUBS = CANONICAL_TARGET_HUBS

_REMOTE_PATTERN = re.compile(
    r"\b(remote|work\s+from\s+home|wfh|telecommute|virtual|anywhere)\b",
    re.IGNORECASE,
)
_HYBRID_PATTERN = re.compile(
    r"\b(hybrid|partially\s+remote|flexible\s+work)\b",
    re.IGNORECASE,
)
_ON_SITE_PATTERN = re.compile(
    r"\b(on[\s-]?site|in[\s-]?office|office[\s-]?based)\b",
    re.IGNORECASE,
)

_SENTINEL_VALUES = {
    "",
    "none",
    "null",
    "n/a",
    "na",
    "unknown",
    "not specified",
    "unspecified",
    "undefined",
}


def normalize_location(
    raw_location: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Clean location string, resolve canonical tech hub, and detect work arrangement.

    Args:
        raw_location: Reported location text (e.g., 'Bengaluru, Karnataka, India').

    Returns:
        tuple[str | None, str | None, str | None]:
            - cleaned_location: Cleaned raw string, or None if absent/sentinel.
            - normalized_location: Canonical city (e.g., 'Bengaluru'), or None if
              missing, ambiguous, or outside supported hubs.
            - remote_hint: Inferred work arrangement ('REMOTE', 'HYBRID', 'ON_SITE', or None).
    """
    if raw_location is None:
        return None, None, None

    cleaned = re.sub(r"\s+", " ", raw_location).strip()
    if cleaned.lower() in _SENTINEL_VALUES:
        return None, None, None

    # Determine remote hint from location text
    remote_hint: str | None = None
    if _REMOTE_PATTERN.search(cleaned):
        remote_hint = "REMOTE"
    elif _HYBRID_PATTERN.search(cleaned):
        remote_hint = "HYBRID"
    elif _ON_SITE_PATTERN.search(cleaned):
        remote_hint = "ON_SITE"

    # Evaluate canonical tech hubs
    matched_hubs: list[str] = []
    text_lower = cleaned.lower()

    for canonical_name, patterns in CANONICAL_HUBS:
        for pat in patterns:
            if re.search(pat, text_lower):
                matched_hubs.append(canonical_name)
                break

    # If Navi Mumbai matched, remove Mumbai match caused by substring
    if "Navi Mumbai" in matched_hubs and "Mumbai" in matched_hubs:
        # Check if Mumbai was mentioned independently or only inside 'Navi Mumbai' / 'New Bombay'
        without_navi = re.sub(r"\b(navi\s+mumbai|new\s+bombay)\b", "", text_lower)
        if not any(re.search(pat, without_navi) for pat in CANONICAL_HUBS[1][1]):
            matched_hubs.remove("Mumbai")

    # Evaluate known out-of-scope cities
    matched_non_target: list[str] = []
    for canonical_name, patterns in KNOWN_NON_TARGET_CITIES:
        for pat in patterns:
            if re.search(pat, text_lower):
                matched_non_target.append(canonical_name)
                break

    # Distinct cities identified across target and non-target catalogs
    total_distinct_cities = set(matched_hubs) | set(matched_non_target)

    if len(total_distinct_cities) == 1:
        # Exactly one unambiguous canonical city resolved
        normalized_location = next(iter(total_distinct_cities))
    else:
        # Ambiguous multi-city listing (len > 1), unresolvable text, or purely remote (len == 0)
        normalized_location = None

    return cleaned, normalized_location, remote_hint
