"""Deterministic job filtering and eligibility engine orchestrator.

Boundary: 100% deterministic, pure in-memory rules execution.
Zero database, network, LLM, or transaction dependencies.
Enforces precedence order documented across JOB_FILTERS_V1.md and VERIFICATION_V1.md.
"""

from datetime import UTC, datetime
from typing import Any

from app.rules.experience import evaluate_experience
from app.rules.freshness import evaluate_freshness
from app.rules.location import evaluate_location
from app.rules.roles import evaluate_role
from app.rules.salary import evaluate_salary
from app.rules.schemas import (
    DEFAULT_RULE_VERSION,
    JobFilterInput,
    JobFilterResult,
)


class JobFilterRuleEngine:
    """Pure deterministic rules engine for job filtering and eligibility classification."""

    def __init__(self, rule_version: str = DEFAULT_RULE_VERSION) -> None:
        self.rule_version = rule_version

    def evaluate(
        self,
        job_input: JobFilterInput,
        *,
        reference_time: datetime | None = None,
    ) -> JobFilterResult:
        """Execute deterministic filter pipeline against a typed job input.

        Precedence Hierarchy:
            1. Role Hard Exclusions (prohibited disciplines, architect/PM/BA, Java/.NET-only)
            2. Experience Hard Exclusions (Senior/Lead/Architect title, min >= 2.0 years)
            3. Location Hard Exclusions (on-site/hybrid confirmed in unlisted city)
            4. Salary Hard Exclusions (disclosed compensation below regional minimum)
            5. Freshness Expiration (verified publication date > 5 days)
            6. Ambiguity Reviews (missing date, ambiguous stack, remote review, ambiguous location,
               ambiguous experience, unknown salary)
            7. Active Eligibility (all filters satisfied)

        Args:
            job_input: Typed in-memory job attributes.
            reference_time: Evaluation anchor timestamp for deterministic freshness.

        Returns:
            JobFilterResult containing eligibility status, role priority, reason codes,
            and complete audit context.
        """
        now = reference_time or datetime.now(UTC)

        # 1. Evaluate individual dimension rules
        role_eval = evaluate_role(
            title=job_input.title,
            normalized_title=job_input.normalized_title,
            description=job_input.description,
        )

        loc_eval = evaluate_location(
            location=job_input.location,
            normalized_location=job_input.normalized_location,
            remote_type=job_input.remote_type,
        )

        exp_eval = evaluate_experience(
            title=job_input.title,
            experience_min_years=job_input.experience_min_years,
            experience_max_years=job_input.experience_max_years,
            experience_raw=job_input.experience_raw,
        )

        salary_eval = evaluate_salary(
            canonical_city=loc_eval.canonical_city,
            salary_min=job_input.salary_min,
            salary_max=job_input.salary_max,
            salary_currency=job_input.salary_currency,
            salary_period=job_input.salary_period,
        )

        freshness_eval = evaluate_freshness(
            posted_at=job_input.posted_at,
            collected_at=job_input.collected_at,
            posting_date_verified=job_input.posting_date_verified,
            reference_time=now,
        )

        # Build detailed evaluation context for FilterDecision audit trail
        eval_context: dict[str, Any] = {
            "title": job_input.title,
            "normalized_title": job_input.normalized_title,
            "role_priority": role_eval.role_priority,
            "matched_role": role_eval.matched_role,
            "location_raw": job_input.location,
            "location_tier": loc_eval.tier,
            "canonical_city": loc_eval.canonical_city,
            "remote_type": job_input.remote_type,
            "experience_raw": job_input.experience_raw,
            "experience_min": float(exp_eval.min_years) if exp_eval.min_years is not None else None,
            "experience_max": float(exp_eval.max_years) if exp_eval.max_years is not None else None,
            "salary_min": float(job_input.salary_min) if job_input.salary_min is not None else None,
            "salary_max": float(job_input.salary_max) if job_input.salary_max is not None else None,
            "salary_period": job_input.salary_period,
            "salary_effective_annual": (
                float(salary_eval.effective_annual)
                if salary_eval.effective_annual is not None
                else None
            ),
            "salary_threshold_annual": (
                float(salary_eval.threshold_annual)
                if salary_eval.threshold_annual is not None
                else None
            ),
            "posted_at": job_input.posted_at.isoformat() if job_input.posted_at else None,
            "collected_at": job_input.collected_at.isoformat() if job_input.collected_at else None,
            "posting_date_verified": job_input.posting_date_verified,
            "freshness_age_days": (
                freshness_eval.age.total_seconds() / 86400.0
                if freshness_eval.age is not None
                else None
            ),
        }

        # --- PRECEDENCE EVALUATION ---

        # 1. Hard Exclusion: Excluded Role / Irrelevant Role
        if role_eval.is_excluded:
            return JobFilterResult(
                eligibility_status="REJECTED",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=role_eval.rejection_reason,
                review_reason=None,
                reason_codes=[role_eval.rejection_reason] if role_eval.rejection_reason else [],
                passed=False,
                rule_version=self.rule_version,
                explanation=role_eval.explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 2. Hard Exclusion: Experience Too High (Senior title or min >= 2.0 years)
        if exp_eval.is_rejected:
            return JobFilterResult(
                eligibility_status="REJECTED",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=exp_eval.rejection_reason,
                review_reason=None,
                reason_codes=[exp_eval.rejection_reason] if exp_eval.rejection_reason else [],
                passed=False,
                rule_version=self.rule_version,
                explanation=exp_eval.explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 3. Hard Exclusion: Location Not Allowed (confirmed on-site in unlisted city)
        if loc_eval.rejection_reason is not None:
            return JobFilterResult(
                eligibility_status="REJECTED",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=loc_eval.rejection_reason,
                review_reason=None,
                reason_codes=[loc_eval.rejection_reason],
                passed=False,
                rule_version=self.rule_version,
                explanation=loc_eval.explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 4. Hard Exclusion: Salary Below Regional Minimum
        if salary_eval.is_rejected:
            return JobFilterResult(
                eligibility_status="REJECTED",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=salary_eval.rejection_reason,
                review_reason=None,
                reason_codes=[salary_eval.rejection_reason] if salary_eval.rejection_reason else [],
                passed=False,
                rule_version=self.rule_version,
                explanation=salary_eval.explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 5. Expiration: Publication Date > 5 Days
        if freshness_eval.is_expired:
            return JobFilterResult(
                eligibility_status="EXPIRED",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=freshness_eval.rejection_reason,
                review_reason=None,
                reason_codes=[freshness_eval.rejection_reason]
                if freshness_eval.rejection_reason
                else [],
                passed=False,
                rule_version=self.rule_version,
                explanation=freshness_eval.explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 6. Ambiguity Reviews: Collect all applicable review reasons in precedence order
        review_reasons: list[str] = []

        if freshness_eval.is_suspicious and freshness_eval.review_reason:
            review_reasons.append(freshness_eval.review_reason)

        if role_eval.is_ambiguous_tech and role_eval.review_reason:
            review_reasons.append(role_eval.review_reason)

        if loc_eval.is_review and loc_eval.review_reason:
            review_reasons.append(loc_eval.review_reason)

        if exp_eval.is_review and exp_eval.review_reason:
            review_reasons.append(exp_eval.review_reason)

        if salary_eval.is_review and salary_eval.review_reason:
            review_reasons.append(salary_eval.review_reason)

        if review_reasons:
            primary_review = review_reasons[0]
            explanations = [
                r
                for r in [
                    freshness_eval.explanation if freshness_eval.is_suspicious else None,
                    role_eval.explanation if role_eval.is_ambiguous_tech else None,
                    loc_eval.explanation if loc_eval.is_review else None,
                    exp_eval.explanation if exp_eval.is_review else None,
                    salary_eval.explanation if salary_eval.is_review else None,
                ]
                if r is not None
            ]
            combined_explanation = "; ".join(explanations)

            return JobFilterResult(
                eligibility_status="REVIEW",
                role_priority=role_eval.role_priority,
                relevance_status=role_eval.relevance_status,
                rejection_reason=None,
                review_reason=primary_review,
                reason_codes=review_reasons,
                passed=False,
                rule_version=self.rule_version,
                explanation=combined_explanation,
                evaluation_context=eval_context,
                evaluated_at=now,
            )

        # 7. Active Eligibility: All criteria satisfied
        return JobFilterResult(
            eligibility_status="ELIGIBLE",
            role_priority=role_eval.role_priority,
            relevance_status=role_eval.relevance_status,
            rejection_reason=None,
            review_reason=None,
            reason_codes=[],
            passed=True,
            rule_version=self.rule_version,
            explanation=f"All criteria satisfied for role {job_input.title} in {loc_eval.canonical_city}",
            evaluation_context=eval_context,
            evaluated_at=now,
        )


# Module-level convenience function
_DEFAULT_ENGINE = JobFilterRuleEngine()


def evaluate_job_filter(
    job_input: JobFilterInput,
    *,
    reference_time: datetime | None = None,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> JobFilterResult:
    """Evaluate job filter rules using default or custom engine configuration."""
    if rule_version == DEFAULT_RULE_VERSION:
        return _DEFAULT_ENGINE.evaluate(job_input, reference_time=reference_time)
    engine = JobFilterRuleEngine(rule_version=rule_version)
    return engine.evaluate(job_input, reference_time=reference_time)
