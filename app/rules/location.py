"""Location tier classification, remote role routing, and geographic exclusions.

Boundary: 100% deterministic rules evaluation.
Enforces frozen V1 location rules from rules/JOB_FILTERS_V1.md Section 5.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app.normalization.location import (
    CANONICAL_TARGET_HUBS,
    KNOWN_NON_TARGET_CITIES,
    normalize_location,
)
from app.rules.schemas import (
    REJECTION_LOCATION_NOT_ALLOWED,
    REVIEW_LOCATION_AMBIGUOUS,
    REVIEW_LOCATION_REMOTE_REVIEW,
)

LocationTier = Literal["P1", "P2", "REMOTE", "UNLISTED", "AMBIGUOUS"]

# P1 Locations (Primary Priority) per Section 5.1
P1_HUBS: frozenset[str] = frozenset(
    {
        "mumbai",
        "navi mumbai",
        "thane",
        "pune",
    }
)

# P2 Locations (Secondary Priority) per Section 5.1
P2_HUBS: frozenset[str] = frozenset(
    {
        "bengaluru",
        "hyderabad",
    }
)

ALL_TARGET_HUBS: frozenset[str] = P1_HUBS | P2_HUBS

_REMOTE_KEYWORDS = re.compile(
    r"\b(?:remote|work\s+from\s+home|wfh|telecommute|virtual|anywhere)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LocationEvaluation:
    """Evaluation outcome for job location criteria."""

    tier: LocationTier
    canonical_city: str | None
    is_allowed: bool
    is_review: bool
    rejection_reason: str | None
    review_reason: str | None
    explanation: str


def evaluate_location(
    location: str | None,
    normalized_location: str | None = None,
    remote_type: str = "UNKNOWN",
) -> LocationEvaluation:
    """Evaluate job location against P1/P2 tiers, remote protocols, and unlisted exclusions.

    Args:
        location: Raw reported location string.
        normalized_location: Pre-normalized canonical city if available.
        remote_type: Work arrangement (ON_SITE, HYBRID, REMOTE, UNKNOWN).

    Returns:
        LocationEvaluation with assigned tier, eligibility outcome, and reason codes.
    """
    clean_raw, norm_from_raw, remote_hint = normalize_location(location)
    resolved_city = normalized_location or norm_from_raw

    # Determine if position is remote
    work_arrangement = (remote_type or "").upper().strip()
    is_remote = (
        work_arrangement in ("REMOTE", "WFH", "WORK_FROM_HOME")
        or remote_hint == "REMOTE"
        or (bool(clean_raw and _REMOTE_KEYWORDS.search(clean_raw)))
    )

    city_lower = resolved_city.lower() if resolved_city else None

    # 1. Check if location resolves to an approved P1 or P2 tech hub
    if city_lower in P1_HUBS:
        return LocationEvaluation(
            tier="P1",
            canonical_city=resolved_city,
            is_allowed=True,
            is_review=False,
            rejection_reason=None,
            review_reason=None,
            explanation=f"Location matches P1 metropolitan tech hub: {resolved_city}",
        )

    if city_lower in P2_HUBS:
        return LocationEvaluation(
            tier="P2",
            canonical_city=resolved_city,
            is_allowed=True,
            is_review=False,
            rejection_reason=None,
            review_reason=None,
            explanation=f"Location matches P2 metropolitan tech hub: {resolved_city}",
        )

    # 2. Check Remote Roles (Section 5.2)
    # If remote, do NOT reject merely because HQ or location is outside P1/P2
    if is_remote:
        return LocationEvaluation(
            tier="REMOTE",
            canonical_city=resolved_city,
            is_allowed=False,
            is_review=True,
            rejection_reason=None,
            review_reason=REVIEW_LOCATION_REMOTE_REVIEW,
            explanation=(
                f"Remote job with location outside P1/P2 hubs or unlisted HQ: "
                f"routed to review pending remote policy ({location or 'Remote'})"
            ),
        )

    # 3. Check for Missing or Ambiguous Location (Section 5.2 & DATABASE_SCHEMA_V1.md)
    if not clean_raw:
        return LocationEvaluation(
            tier="AMBIGUOUS",
            canonical_city=None,
            is_allowed=False,
            is_review=True,
            rejection_reason=None,
            review_reason=REVIEW_LOCATION_AMBIGUOUS,
            explanation="Job posting has missing or unstated location",
        )

    # Check for multi-city listing or unresolvable location text
    text_lower = clean_raw.lower()
    matched_target_hubs: list[str] = []
    for canonical_name, patterns in CANONICAL_TARGET_HUBS:
        for pat in patterns:
            if re.search(pat, text_lower):
                matched_target_hubs.append(canonical_name)
                break

    matched_non_targets: list[str] = []
    for canonical_name, patterns in KNOWN_NON_TARGET_CITIES:
        for pat in patterns:
            if re.search(pat, text_lower):
                matched_non_targets.append(canonical_name)
                break

    total_cities = set(matched_target_hubs) | set(matched_non_targets)
    if len(total_cities) > 1:
        # Multi-city listing without clear single classification
        return LocationEvaluation(
            tier="AMBIGUOUS",
            canonical_city=None,
            is_allowed=False,
            is_review=True,
            rejection_reason=None,
            review_reason=REVIEW_LOCATION_AMBIGUOUS,
            explanation=f"Multi-city location without clear classification: {clean_raw}",
        )

    # If exactly one non-target city matched, or text represents a confirmed unlisted city
    if len(matched_non_targets) == 1:
        city_name = matched_non_targets[0]
        return LocationEvaluation(
            tier="UNLISTED",
            canonical_city=city_name,
            is_allowed=False,
            is_review=False,
            rejection_reason=REJECTION_LOCATION_NOT_ALLOWED,
            review_reason=None,
            explanation=f"On-site or hybrid role situated in unlisted city: {city_name}",
        )

    # Check if raw location is a known unlisted location outside India or outside hubs
    # (e.g., London, New York, Singapore, Austin, Berlin, etc.)
    _FOREIGN_OR_OTHER_CITY = re.compile(
        r"\b(?:london|new\s+york|san\s+francisco|seattle|austin|singapore|berlin|sydney|"
        r"toronto|dubai|chennai|noida|gurgaon|delhi|kolkata|ahmedabad|jaipur|chandigarh|"
        r"tel\s+aviv|tel-aviv|tlv|s[aã]o\s+paulo|sao\s+paulo|paris|tokyo|tōkyō)\b",
        re.IGNORECASE,
    )
    if _FOREIGN_OR_OTHER_CITY.search(clean_raw):
        return LocationEvaluation(
            tier="UNLISTED",
            canonical_city=clean_raw,
            is_allowed=False,
            is_review=False,
            rejection_reason=REJECTION_LOCATION_NOT_ALLOWED,
            review_reason=None,
            explanation=f"On-site or hybrid role situated in unlisted city: {clean_raw}",
        )

    # If location string cannot be deterministically resolved to any known city
    return LocationEvaluation(
        tier="AMBIGUOUS",
        canonical_city=None,
        is_allowed=False,
        is_review=True,
        rejection_reason=None,
        review_reason=REVIEW_LOCATION_AMBIGUOUS,
        explanation=f"Unresolved location string: {clean_raw}",
    )
