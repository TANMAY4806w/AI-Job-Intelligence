"""Deterministic eight-dimension verification evaluator.

Boundary: 100% pure domain evaluation engine.
Follows rules/VERIFICATION_V1.md and rules/JOB_FILTERS_V1.md.
Independent of SQLAlchemy, persistence repositories, network I/O, scraping, and LLMs.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

from app.normalization.location import normalize_location
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

# Specific recognized source platform identities per DATABASE_SCHEMA_V1.md & VERIFICATION_V1.md
APPROVED_SOURCE_PLATFORMS: frozenset[str] = frozenset(
    {
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "smartrecruiters",
        "jobvite",
        "taleo",
        "career_page",
        "company_career_page",
    }
)
# Alias for backwards compatibility referencing the approved platform set
APPROVED_SOURCE_TYPES: frozenset[str] = APPROVED_SOURCE_PLATFORMS

# Approved P1 tech hubs per rules/JOB_FILTERS_V1.md Section 5.1
CANONICAL_P1_HUBS: frozenset[str] = frozenset(
    {
        "Mumbai",
        "Navi Mumbai",
        "Thane",
        "Pune",
    }
)

# Approved P2 tech hubs per rules/JOB_FILTERS_V1.md Section 5.1
CANONICAL_P2_HUBS: frozenset[str] = frozenset(
    {
        "Bengaluru",
        "Hyderabad",
    }
)

ALL_TARGET_HUBS: frozenset[str] = CANONICAL_P1_HUBS | CANONICAL_P2_HUBS

# Maximum plausible age for publication dates (90 days per Dimension 8)
MAX_PLAUSIBLE_AGE = timedelta(days=90)

# Confidence dimension weights per rules/VERIFICATION_V1.md Section 3.1
WEIGHT_JD = Decimal("0.30")
WEIGHT_SOURCE = Decimal("0.20")
WEIGHT_COMPANY = Decimal("0.20")
WEIGHT_EXPERIENCE = Decimal("0.15")
WEIGHT_LOCATION = Decimal("0.10")
WEIGHT_SALARY = Decimal("0.05")

# Accessible HTTP status codes for URL probing
ACCESSIBLE_HTTP_STATUS_CODES: frozenset[int] = frozenset({200, 201, 301, 302, 303, 307, 308})
BROKEN_HTTP_STATUS_CODES: frozenset[int] = frozenset({400, 401, 403, 404, 410, 500, 502, 503, 504})


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime object is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _is_valid_https_url(url: str | None) -> bool:
    """Validate that a URL string is syntactically a valid HTTPS URI."""
    if not url or not isinstance(url, str):
        return False
    url_stripped = url.strip()
    try:
        parsed = urlparse(url_stripped)
        return parsed.scheme.lower() == "https" and bool(parsed.netloc)
    except Exception:
        return False


def _is_url_accessible(
    url: str | None,
    status_code: int | None,
    is_reachable: bool | None,
) -> bool:
    """Check whether a URL is considered accessible based on syntax and observed probe."""
    if not _is_valid_https_url(url):
        return False
    if is_reachable is False:
        return False
    if status_code is not None:
        return status_code in ACCESSIBLE_HTTP_STATUS_CODES
    return is_reachable is True


def _is_url_broken(
    url: str | None,
    status_code: int | None,
    is_reachable: bool | None,
) -> bool:
    """Check whether a URL is confirmed broken or missing."""
    if not url:
        return True
    if not _is_valid_https_url(url):
        return True
    if is_reachable is False:
        return True
    return bool(status_code is not None and status_code in BROKEN_HTTP_STATUS_CODES)


class JobVerificationEvaluator:
    """Pure, deterministic eight-dimension verification evaluator.

    Implements:
    - 8 independent dimension evaluators.
    - Weighted composite confidence metric calculation ($0.00 to 1.00$).
    - Distinction between hard technical failure and information ambiguity review.
    - Publication-date verification gating without using collected_at as publication date.
    - Audit trail generation.
    """

    VERSION: str = "V1.0"

    def evaluate(
        self,
        input_data: JobVerificationInput,
        *,
        reference_time: datetime | None = None,
    ) -> JobVerificationResult:
        """Deterministically evaluate verification evidence against all eight dimensions.

        Args:
            input_data: Structured verification evidence.
            reference_time: Optional reference timestamp for date checks; defaults to now UTC.

        Returns:
            JobVerificationResult: Immutable structured evaluation outcome.
        """
        now = _ensure_utc(reference_time) or datetime.now(UTC)

        # 1. Evaluate Dimension 1: Source Validity
        dim_source = self._evaluate_source(input_data)

        # 2. Evaluate Dimension 2: Actual JD Existence
        dim_jd = self._evaluate_jd(input_data)

        # 3. Evaluate Dimension 3: Application URL Validity
        dim_app_url = self._evaluate_application_url(input_data)

        # 4. Evaluate Dimension 4: Company Legitimacy
        dim_company = self._evaluate_company(input_data)

        # 5. Evaluate Dimension 5: Location Consistency
        dim_location = self._evaluate_location(input_data)

        # 6. Evaluate Dimension 6: Experience Consistency
        dim_experience = self._evaluate_experience(input_data)

        # 7. Evaluate Dimension 7: Salary Statedness
        dim_salary = self._evaluate_salary(input_data)

        # 8. Evaluate Dimension 8: Posting Date Plausibility & Authenticity
        dim_posting_date = self._evaluate_posting_date(input_data, now)

        # 9. Cross-Source Conflict Check
        conflict_review_reasons: list[str] = []
        if input_data.has_source_conflicts or input_data.source_conflicts:
            conflict_review_reasons.append(REVIEW_SOURCE_CONFLICT_REVIEW)

        # 10. Composite Confidence Metric
        confidence, tier = self._calculate_confidence(
            jd_verified=dim_jd.verified,
            source_verified=dim_source.verified,
            company_verified=dim_company.verified,
            experience_verified=dim_experience.verified,
            location_verified=dim_location.verified,
            salary_verified=dim_salary.verified,
        )

        # 11. Critical Dimensions & Overall Passed Status
        # passed is True if critical dimensions (JD authenticity and source validity) passed
        passed = bool(dim_jd.verified and dim_source.verified)

        # Collect failure reasons (hard technical failures)
        failure_reasons_list: list[str] = []
        if not dim_source.verified:
            failure_reasons_list.extend(dim_source.failure_reasons)
        if not dim_jd.verified:
            failure_reasons_list.extend(dim_jd.failure_reasons)

        failure_reasons = failure_reasons_list if failure_reasons_list else None

        # Collect review reasons in deterministic order
        all_review_reasons: list[str] = []
        if REVIEW_SOURCE_CONFLICT_REVIEW in conflict_review_reasons:
            all_review_reasons.append(REVIEW_SOURCE_CONFLICT_REVIEW)
        for r in dim_app_url.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)
        for r in dim_company.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)
        for r in dim_location.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)
        for r in dim_experience.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)
        for r in dim_salary.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)
        for r in dim_posting_date.review_reasons:
            if r not in all_review_reasons:
                all_review_reasons.append(r)

        review_reasons = all_review_reasons if all_review_reasons else None

        # Assemble dimension results map
        dimensions: dict[str, DimensionResult] = {
            "source_validity": dim_source,
            "jd_existence": dim_jd,
            "application_url_validity": dim_app_url,
            "company_legitimacy": dim_company,
            "location_consistency": dim_location,
            "experience_consistency": dim_experience,
            "salary_statedness": dim_salary,
            "posting_date_plausibility": dim_posting_date,
        }

        # Build comprehensive audit trail
        audit_trail: dict[str, Any] = {
            "evaluator_version": self.VERSION,
            "evaluated_at": now.isoformat(),
            "overall": {
                "passed": passed,
                "confidence_score": float(confidence),
                "confidence_tier": tier,
                "failure_reasons": failure_reasons,
                "review_reasons": review_reasons,
            },
            "confidence_metric": {
                "score": float(confidence),
                "tier": tier,
                "weights": {
                    "jd_authenticity": float(WEIGHT_JD),
                    "source_trust": float(WEIGHT_SOURCE),
                    "company_legitimacy": float(WEIGHT_COMPANY),
                    "experience_clarity": float(WEIGHT_EXPERIENCE),
                    "location_consistency": float(WEIGHT_LOCATION),
                    "salary_disclosure": float(WEIGHT_SALARY),
                },
                "components": {
                    "jd_verified": dim_jd.verified,
                    "source_verified": dim_source.verified,
                    "company_verified": dim_company.verified,
                    "experience_verified": dim_experience.verified,
                    "location_verified": dim_location.verified,
                    "salary_verified": dim_salary.verified,
                },
            },
            "dimensions": {
                k: {
                    "verified": v.verified,
                    "score": float(v.score),
                    "weight": float(v.weight),
                    "failure_reasons": v.failure_reasons,
                    "review_reasons": v.review_reasons,
                    "details": v.details,
                }
                for k, v in dimensions.items()
            },
            "cross_source_conflicts": {
                "has_conflicts": input_data.has_source_conflicts,
                "conflicts": input_data.source_conflicts,
            },
        }

        return JobVerificationResult(
            source_verified=dim_source.verified,
            jd_verified=dim_jd.verified,
            application_url_verified=dim_app_url.verified,
            company_verified=dim_company.verified,
            location_verified=dim_location.verified,
            experience_verified=dim_experience.verified,
            salary_verified=dim_salary.verified,
            posting_date_verified=dim_posting_date.verified,
            verification_confidence=confidence,
            confidence_tier=tier,
            passed=passed,
            failure_reasons=failure_reasons,
            review_reasons=review_reasons,
            dimensions=dimensions,
            audit_trail=audit_trail,
        )

    # -----------------------------------------------------------------------
    # Dimension 1: Source Validity
    # -----------------------------------------------------------------------
    def _evaluate_source(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate origin platform legitimacy and protocol adherence."""
        weight = WEIGHT_SOURCE
        failure_reasons: list[str] = []
        details: dict[str, Any] = {
            "source_id": str(input_data.source_id) if input_data.source_id else None,
            "source_name": input_data.source_name,
            "source_type": input_data.source_type,
            "is_source_approved": input_data.is_source_approved,
            "payload_format_valid": input_data.payload_format_valid,
        }

        # 1. Payload format invalidity is an immediate failure
        if input_data.payload_format_valid is False:
            failure_reasons.append(FAILURE_SOURCE_INVALID)
            details["failure_cause"] = "payload_protocol_invalid"
            return DimensionResult(
                dimension_name="source_validity",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=failure_reasons,
                review_reasons=[],
                details=details,
            )

        # 2. Check explicit approval flag if provided
        if input_data.is_source_approved is not None:
            if input_data.is_source_approved:
                details["approval_method"] = "explicit_approval_flag"
                return DimensionResult(
                    dimension_name="source_validity",
                    verified=True,
                    weight=weight,
                    score=Decimal("1.00"),
                    failure_reasons=[],
                    review_reasons=[],
                    details=details,
                )
            failure_reasons.append(FAILURE_SOURCE_INVALID)
            details["failure_cause"] = "source_explicitly_disapproved"
            return DimensionResult(
                dimension_name="source_validity",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=failure_reasons,
                review_reasons=[],
                details=details,
            )

        # 3. Check approved specific source platform name in registry
        # A generic channel category (e.g. ATS_API, DIRECT_PORTAL) does not establish platform trust.
        name = (input_data.source_name or "").strip().lower()

        if name in APPROVED_SOURCE_PLATFORMS:
            details["approval_method"] = "registry_lookup"
            return DimensionResult(
                dimension_name="source_validity",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # Missing or unapproved source
        failure_reasons.append(FAILURE_SOURCE_INVALID)
        details["failure_cause"] = "unrecognized_source_platform"
        return DimensionResult(
            dimension_name="source_validity",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=failure_reasons,
            review_reasons=[],
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 2: Actual JD Existence
    # -----------------------------------------------------------------------
    def _evaluate_jd(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate whether job description actually exists and contains coherent content."""
        weight = WEIGHT_JD
        failure_reasons: list[str] = []
        body = (input_data.description or "").strip()
        char_count = len(body)
        details: dict[str, Any] = {
            "char_count": char_count,
            "http_status_code": input_data.http_status_code,
            "is_page_active": input_data.is_page_active,
            "is_tombstone": input_data.is_tombstone,
            "is_expired": input_data.is_expired,
        }

        # 1. Expired / tombstone / 410 checks
        if input_data.http_status_code == 410 or input_data.is_tombstone or input_data.is_expired:
            failure_reasons.append(FAILURE_JD_EXPIRED)
            details["failure_cause"] = "posting_expired_or_tombstone"
            return DimensionResult(
                dimension_name="jd_existence",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=failure_reasons,
                review_reasons=[],
                details=details,
            )

        # 2. 404 Not Found check
        if input_data.http_status_code == 404 or input_data.is_page_active is False:
            failure_reasons.append(FAILURE_JD_NOT_FOUND)
            details["failure_cause"] = "page_not_found_404"
            return DimensionResult(
                dimension_name="jd_existence",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=failure_reasons,
                review_reasons=[],
                details=details,
            )

        # 3. Minimum body length requirement (> 100 characters per specification)
        if char_count <= 100:
            failure_reasons.append(FAILURE_JD_NOT_FOUND)
            details["failure_cause"] = f"insufficient_jd_length_{char_count}_le_100"
            return DimensionResult(
                dimension_name="jd_existence",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=failure_reasons,
                review_reasons=[],
                details=details,
            )

        details["status"] = "authentic_and_coherent"
        return DimensionResult(
            dimension_name="jd_existence",
            verified=True,
            weight=weight,
            score=Decimal("1.00"),
            failure_reasons=[],
            review_reasons=[],
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 3: Application URL Validity
    # -----------------------------------------------------------------------
    def _evaluate_application_url(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate reachability and syntactic validity of application URL and fallback.

        Semantic rules (Dimension 3):
        - A URL is verified (verified=True, score=1.00) only when confirmed accessible via probe.
        - A valid HTTPS URL that has not been probed (reachability untested) remains unverified
          (verified=False, score=0.00), but is NOT broken; it does not receive APPLICATION_LINK_BROKEN.
        - A URL is broken if missing, syntactically invalid/malformed, or explicitly failed probe.
        - If primary is broken, check fallback source_url. If both are broken/missing, flag APPLICATION_LINK_BROKEN.
        """
        weight = Decimal("0.00")
        review_reasons: list[str] = []
        details: dict[str, Any] = {
            "application_url": input_data.application_url,
            "application_url_status": input_data.application_url_status,
            "application_url_reachable": input_data.application_url_reachable,
            "source_url": input_data.source_url,
            "source_url_status": input_data.source_url_status,
            "source_url_reachable": input_data.source_url_reachable,
        }

        # 1. Primary application URL is confirmed accessible
        if _is_url_accessible(
            input_data.application_url,
            input_data.application_url_status,
            input_data.application_url_reachable,
        ):
            details["primary_url_status"] = "accessible_and_valid"
            return DimensionResult(
                dimension_name="application_url_validity",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 2. Primary application URL is syntactically valid HTTPS, but reachability is unprobed
        is_primary_valid_syntax = _is_valid_https_url(input_data.application_url)
        is_primary_untested = (
            is_primary_valid_syntax
            and input_data.application_url_status is None
            and input_data.application_url_reachable is None
        )

        if is_primary_untested:
            details["primary_url_status"] = "unprobed_syntax_valid"
            return DimensionResult(
                dimension_name="application_url_validity",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 3. Primary application URL is broken, missing, or confirmed unreachable. Evaluate fallback.
        details["primary_url_status"] = "broken_or_invalid"

        if _is_url_accessible(
            input_data.source_url,
            input_data.source_url_status,
            input_data.source_url_reachable,
        ):
            details["fallback_source_url_status"] = "fallback_accessible"
            return DimensionResult(
                dimension_name="application_url_validity",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        is_fallback_valid_syntax = _is_valid_https_url(input_data.source_url)
        is_fallback_untested = (
            is_fallback_valid_syntax
            and input_data.source_url_status is None
            and input_data.source_url_reachable is None
        )

        if is_fallback_untested:
            details["fallback_source_url_status"] = "fallback_unprobed_syntax_valid"
            return DimensionResult(
                dimension_name="application_url_validity",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 4. Both primary and fallback are missing, malformed, or confirmed broken
        review_reasons.append(REVIEW_APPLICATION_LINK_BROKEN)
        details["fallback_source_url_status"] = "fallback_broken_or_absent"
        return DimensionResult(
            dimension_name="application_url_validity",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=[],
            review_reasons=review_reasons,
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 4: Company Legitimacy
    # -----------------------------------------------------------------------
    def _evaluate_company(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate whether hiring company is an identifiable real-world entity."""
        weight = WEIGHT_COMPANY
        review_reasons: list[str] = []
        details: dict[str, Any] = {
            "company_name": input_data.company_name,
            "company_id": str(input_data.company_id) if input_data.company_id else None,
            "is_company_registry_verified": input_data.is_company_registry_verified,
            "company_domain": input_data.company_domain,
            "has_verifiable_domain": input_data.has_verifiable_domain,
        }

        # Check company registry verification
        if input_data.is_company_registry_verified is True:
            details["verification_method"] = "company_registry"
            return DimensionResult(
                dimension_name="company_legitimacy",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # Check verifiable domain
        if input_data.has_verifiable_domain is True:
            details["verification_method"] = "verifiable_domain"
            return DimensionResult(
                dimension_name="company_legitimacy",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # Unverified company entity -> review_reason = COMPANY_UNVERIFIED (Do NOT reject)
        review_reasons.append(REVIEW_COMPANY_UNVERIFIED)
        details["status"] = "unverified_entity"
        return DimensionResult(
            dimension_name="company_legitimacy",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=[],
            review_reasons=review_reasons,
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 5: Location Consistency
    # -----------------------------------------------------------------------
    def _evaluate_location(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate reported location consistency with approved hubs or remote clause."""
        weight = WEIGHT_LOCATION
        review_reasons: list[str] = []
        remote_type = (input_data.remote_type or "UNKNOWN").strip().upper()

        details: dict[str, Any] = {
            "location": input_data.location,
            "normalized_location": input_data.normalized_location,
            "remote_type": remote_type,
            "company_location": input_data.company_location,
        }

        # Resolve canonical target city (Mumbai, Navi Mumbai, Thane, Pune, Bengaluru, Hyderabad)
        resolved_city: str | None = None
        if input_data.normalized_location:
            norm_input = input_data.normalized_location.strip().lower()
            for hub in ALL_TARGET_HUBS:
                if hub.lower() == norm_input:
                    resolved_city = hub
                    break
        if resolved_city is None and input_data.location:
            _, norm_from_raw, _ = normalize_location(input_data.location)
            if norm_from_raw in ALL_TARGET_HUBS:
                resolved_city = norm_from_raw

        # 1. Remote clause protocol (VERIFICATION_V1.md Section 4.4)
        if remote_type == "REMOTE":
            is_company_in_hub = False
            if input_data.company_location:
                _, comp_norm, _ = normalize_location(input_data.company_location)
                if comp_norm in ALL_TARGET_HUBS or any(
                    hub.lower() in input_data.company_location.lower() for hub in ALL_TARGET_HUBS
                ):
                    is_company_in_hub = True

            if is_company_in_hub:
                details["status"] = "remote_with_hub_headquarters"
                return DimensionResult(
                    dimension_name="location_consistency",
                    verified=True,
                    weight=weight,
                    score=Decimal("1.00"),
                    failure_reasons=[],
                    review_reasons=[],
                    details=details,
                )
            # Remote with company outside P1/P2 hubs: location_verified = true, route to REVIEW
            review_reasons.append(REVIEW_LOCATION_REMOTE_REVIEW)
            details["status"] = "remote_with_non_hub_headquarters"
            return DimensionResult(
                dimension_name="location_consistency",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=review_reasons,
                details=details,
            )

        # 2. Approved regional tech hub check (P1: Mumbai, Navi Mumbai, Thane, Pune; P2: Bengaluru, Hyderabad)
        if resolved_city in ALL_TARGET_HUBS:
            tier = "P1" if resolved_city in CANONICAL_P1_HUBS else "P2"
            details["matched_hub"] = resolved_city
            details["location_tier"] = tier
            return DimensionResult(
                dimension_name="location_consistency",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 3. Location is ambiguous, outside approved hubs, or missing
        review_reasons.append(REVIEW_LOCATION_AMBIGUOUS)
        details["status"] = "location_ambiguous_or_unapproved"
        return DimensionResult(
            dimension_name="location_consistency",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=[],
            review_reasons=review_reasons,
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 6: Experience Consistency
    # -----------------------------------------------------------------------
    def _evaluate_experience(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate stated experience requirement against target bands (0–2 years)."""
        weight = WEIGHT_EXPERIENCE
        review_reasons: list[str] = []
        details: dict[str, Any] = {
            "experience_raw": input_data.experience_raw,
            "experience_min_years": float(input_data.experience_min_years)
            if input_data.experience_min_years is not None
            else None,
            "experience_max_years": float(input_data.experience_max_years)
            if input_data.experience_max_years is not None
            else None,
            "is_senior_lead_title": input_data.is_senior_lead_title,
        }

        # 1. Explicit Senior/Lead title is excluded from target policy
        if input_data.is_senior_lead_title:
            details["status"] = "senior_or_lead_role_excluded"
            return DimensionResult(
                dimension_name="experience_consistency",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 2. Minimum experience >= 2.0 years is excluded from 0-2 yr policy
        if input_data.experience_min_years is not None:
            min_exp = input_data.experience_min_years
            max_exp = input_data.experience_max_years

            if min_exp >= Decimal("2.0"):
                details["status"] = "experience_too_high"
                return DimensionResult(
                    dimension_name="experience_consistency",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=[],
                    details=details,
                )

            # Target bands: 0–1, 0–2, 1–2 yrs
            if max_exp is not None:
                if max_exp <= Decimal("2.0"):
                    details["status"] = "target_experience_band_verified"
                    return DimensionResult(
                        dimension_name="experience_consistency",
                        verified=True,
                        weight=weight,
                        score=Decimal("1.00"),
                        failure_reasons=[],
                        review_reasons=[],
                        details=details,
                    )
                if min_exp == Decimal("1.0") and max_exp == Decimal("3.0"):
                    # 1–3 years protocol per Section 4.2: experience_verified = false, review_reason = EXPERIENCE_AMBIGUOUS
                    review_reasons.append(REVIEW_EXPERIENCE_AMBIGUOUS)
                    details["status"] = "experience_1_to_3_years_ambiguous"
                    return DimensionResult(
                        dimension_name="experience_consistency",
                        verified=False,
                        weight=weight,
                        score=Decimal("0.00"),
                        failure_reasons=[],
                        review_reasons=review_reasons,
                        details=details,
                    )
                # max_exp > 2.0 (e.g. 0-3 or 0-4)
                review_reasons.append(REVIEW_EXPERIENCE_AMBIGUOUS)
                details["status"] = "experience_max_crosses_target_policy"
                return DimensionResult(
                    dimension_name="experience_consistency",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=review_reasons,
                    details=details,
                )

            # min_exp < 2.0 and max_exp is None
            details["status"] = "minimum_experience_within_band"
            return DimensionResult(
                dimension_name="experience_consistency",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # 3. Check raw experience string heuristics if numeric fields are absent
        raw = (input_data.experience_raw or "").strip().lower()
        if raw:
            if any(
                term in raw
                for term in ("fresher", "0-1", "0-2", "1-2", "0 to 1", "0 to 2", "1 to 2")
            ):
                details["status"] = "raw_experience_target_band"
                return DimensionResult(
                    dimension_name="experience_consistency",
                    verified=True,
                    weight=weight,
                    score=Decimal("1.00"),
                    failure_reasons=[],
                    review_reasons=[],
                    details=details,
                )
            if "1-3" in raw or "1 to 3" in raw:
                review_reasons.append(REVIEW_EXPERIENCE_AMBIGUOUS)
                details["status"] = "raw_experience_1_to_3_years"
                return DimensionResult(
                    dimension_name="experience_consistency",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=review_reasons,
                    details=details,
                )

        # 4. Unstated or unparseable experience -> EXPERIENCE_AMBIGUOUS
        review_reasons.append(REVIEW_EXPERIENCE_AMBIGUOUS)
        details["status"] = "experience_unstated_or_ambiguous"
        return DimensionResult(
            dimension_name="experience_consistency",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=[],
            review_reasons=review_reasons,
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 7: Salary Statedness
    # -----------------------------------------------------------------------
    def _evaluate_salary(self, input_data: JobVerificationInput) -> DimensionResult:
        """Evaluate whether compensation is explicitly disclosed."""
        weight = WEIGHT_SALARY
        review_reasons: list[str] = []
        details: dict[str, Any] = {
            "salary_min": float(input_data.salary_min)
            if input_data.salary_min is not None
            else None,
            "salary_max": float(input_data.salary_max)
            if input_data.salary_max is not None
            else None,
            "salary_currency": input_data.salary_currency,
            "salary_period": input_data.salary_period,
        }

        # Check whether numeric compensation is present
        if input_data.salary_min is not None or input_data.salary_max is not None:
            details["status"] = "compensation_explicitly_disclosed"
            return DimensionResult(
                dimension_name="salary_statedness",
                verified=True,
                weight=weight,
                score=Decimal("1.00"),
                failure_reasons=[],
                review_reasons=[],
                details=details,
            )

        # Undisclosed compensation protocol (VERIFICATION_V1.md Section 4.1):
        # Set salary_verified = false, review_reason = SALARY_UNKNOWN. Do NOT reject.
        review_reasons.append(REVIEW_SALARY_UNKNOWN)
        details["status"] = "compensation_unstated"
        return DimensionResult(
            dimension_name="salary_statedness",
            verified=False,
            weight=weight,
            score=Decimal("0.00"),
            failure_reasons=[],
            review_reasons=review_reasons,
            details=details,
        )

    # -----------------------------------------------------------------------
    # Dimension 8: Posting Date Plausibility & Authenticity
    # -----------------------------------------------------------------------
    def _evaluate_posting_date(
        self,
        input_data: JobVerificationInput,
        now: datetime,
    ) -> DimensionResult:
        """Evaluate publication date plausibility and verify authenticity gating."""
        weight = Decimal("0.00")
        review_reasons: list[str] = []
        posted = _ensure_utc(input_data.posted_at)
        collected = _ensure_utc(input_data.collected_at)

        details: dict[str, Any] = {
            "posted_at": posted.isoformat() if posted else None,
            "collected_at": collected.isoformat() if collected else None,
            "publication_date_corroborated": input_data.publication_date_corroborated,
            "posting_date_verified": input_data.posting_date_verified,
        }

        # 1. Missing publication date
        if posted is None:
            review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
            details["status"] = "publication_date_missing"
            return DimensionResult(
                dimension_name="posting_date_plausibility",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=review_reasons,
                details=details,
            )

        # 2. Future publication date check relative to now
        if posted > now:
            review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
            details["status"] = "future_publication_date"
            return DimensionResult(
                dimension_name="posting_date_plausibility",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=review_reasons,
                details=details,
            )

        # 3. Plausibility check relative to collected_at if provided
        # Formula: T_collected >= T_posted >= (T_collected - 90 days)
        if collected is not None:
            # Allow 5-minute clock skew tolerance between systems
            if posted > collected + timedelta(minutes=5):
                review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
                details["status"] = "posted_after_collection"
                return DimensionResult(
                    dimension_name="posting_date_plausibility",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=review_reasons,
                    details=details,
                )
            if posted < collected - MAX_PLAUSIBLE_AGE:
                review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
                details["status"] = "posted_older_than_90_days_before_collection"
                return DimensionResult(
                    dimension_name="posting_date_plausibility",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=review_reasons,
                    details=details,
                )
        else:
            if posted < now - MAX_PLAUSIBLE_AGE:
                review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
                details["status"] = "posted_older_than_90_days"
                return DimensionResult(
                    dimension_name="posting_date_plausibility",
                    verified=False,
                    weight=weight,
                    score=Decimal("0.00"),
                    failure_reasons=[],
                    review_reasons=review_reasons,
                    details=details,
                )

        # 4. Authenticity corroboration gating
        # A recent-looking posted_at alone does not prove authenticity; explicit corroboration is required
        is_corroborated = (
            input_data.publication_date_corroborated is True
            or input_data.posting_date_verified is True
        )
        is_explicitly_refuted = (
            input_data.publication_date_corroborated is False
            or input_data.posting_date_verified is False
        )

        if is_explicitly_refuted:
            review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
            details["status"] = "publication_date_corroboration_failed"
            return DimensionResult(
                dimension_name="posting_date_plausibility",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=review_reasons,
                details=details,
            )

        if not is_corroborated:
            # Uncorroborated / unresolved publication date -> cannot be verified
            review_reasons.append(REVIEW_POSTED_DATE_SUSPICIOUS)
            details["status"] = "publication_date_unresolved_without_corroboration"
            return DimensionResult(
                dimension_name="posting_date_plausibility",
                verified=False,
                weight=weight,
                score=Decimal("0.00"),
                failure_reasons=[],
                review_reasons=review_reasons,
                details=details,
            )

        # Plausible and corroborated
        details["status"] = "publication_date_plausible_and_verified"
        return DimensionResult(
            dimension_name="posting_date_plausibility",
            verified=True,
            weight=weight,
            score=Decimal("1.00"),
            failure_reasons=[],
            review_reasons=[],
            details=details,
        )

    # -----------------------------------------------------------------------
    # Composite Confidence Metric
    # -----------------------------------------------------------------------
    def _calculate_confidence(
        self,
        *,
        jd_verified: bool,
        source_verified: bool,
        company_verified: bool,
        experience_verified: bool,
        location_verified: bool,
        salary_verified: bool,
    ) -> tuple[Decimal, ConfidenceTier]:
        """Calculate weighted composite confidence score and tier."""
        score = (
            WEIGHT_JD * (1 if jd_verified else 0)
            + WEIGHT_SOURCE * (1 if source_verified else 0)
            + WEIGHT_COMPANY * (1 if company_verified else 0)
            + WEIGHT_EXPERIENCE * (1 if experience_verified else 0)
            + WEIGHT_LOCATION * (1 if location_verified else 0)
            + WEIGHT_SALARY * (1 if salary_verified else 0)
        ).quantize(Decimal("0.01"))

        if score >= Decimal("0.85"):
            tier: ConfidenceTier = "HIGH"
        elif score >= Decimal("0.60"):
            tier = "MODERATE"
        else:
            tier = "LOW"

        return score, tier
