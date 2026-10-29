"""Job re-evaluation and eligibility refresh orchestration service.

Boundary: Evaluates canonical stored jobs against the latest verification evaluator
and deterministic filter rule engine without mutating database state.
Strict read-only safety: Enforces dry_run=True, raising an exception if mutating
execution is attempted.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cli.greenhouse import normalize_company_name
from app.normalization.location import normalize_location
from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.rules.engine import JobFilterRuleEngine
from app.rules.schemas import (
    REVIEW_COMPANY_UNVERIFIED,
    REVIEW_EXPERIENCE_AMBIGUOUS,
    REVIEW_POSTED_DATE_SUSPICIOUS,
    REVIEW_SALARY_UNKNOWN,
    JobFilterInput,
    JobFilterResult,
)
from app.verification.evaluator import (
    APPROVED_SOURCE_TYPES,
    JobVerificationEvaluator,
)
from app.verification.schemas import (
    JobVerificationInput,
    JobVerificationResult,
)

logger = logging.getLogger(__name__)


class JobReevaluationComparison(BaseModel):
    """Detailed side-by-side comparison between persisted and proposed outcomes for a job."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    job_id: uuid.UUID
    source_job_id: str | None = None
    title: str
    location: str | None = None
    normalized_location: str | None = None
    company_name: str

    # Persisted database state
    persisted_status: str
    persisted_rejection_reason: str | None = None
    persisted_review_reason: str | None = None
    persisted_role_priority: str | None = None
    persisted_relevance_status: str | None = None
    persisted_verification_confidence: Decimal
    persisted_verified: bool
    persisted_dimension_flags: dict[str, bool]

    # Proposed re-evaluated state
    proposed_status: str
    proposed_rejection_reason: str | None = None
    proposed_review_reason: str | None = None
    proposed_role_priority: str | None = None
    proposed_relevance_status: str | None = None
    proposed_verification_confidence: Decimal
    proposed_verified: bool
    proposed_dimension_flags: dict[str, bool]
    proposed_dimensions: dict[str, Any]

    # Comparative difference indicators
    status_changed: bool
    reason_changed: bool
    confidence_changed: bool
    verification_passed_changed: bool

    # Categorized evidence observations
    missing_evidence: list[str]
    confirmed_failures: list[str]


class ReevaluationReport(BaseModel):
    """Machine-readable aggregate summary of the dry-run re-evaluation."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    company_id: uuid.UUID
    company_name: str
    total_jobs: int
    reference_time: datetime
    dry_run: bool = True

    # Comparative status & reason distributions
    persisted_status_counts: dict[str, int]
    proposed_status_counts: dict[str, int]
    persisted_reason_counts: dict[str, int]
    proposed_reason_counts: dict[str, int]
    status_transitions: dict[str, int]

    # Verification dimension pass counts
    persisted_dimension_counts: dict[str, int]
    proposed_dimension_counts: dict[str, int]

    # Confidence metrics
    persisted_confidence_counts: dict[str, int]
    proposed_confidence_counts: dict[str, int]
    average_persisted_confidence: Decimal
    average_proposed_confidence: Decimal

    # Evidence breakdown counts
    missing_evidence_counts: dict[str, int]
    confirmed_failure_counts: dict[str, int]

    # Change count
    total_status_changes: int

    # Detailed per-job list
    job_comparisons: list[JobReevaluationComparison]

    def to_dict(self) -> dict[str, Any]:
        """Convert report to serializable dictionary."""
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "total_jobs": self.total_jobs,
            "reference_time": self.reference_time.isoformat(),
            "dry_run": self.dry_run,
            "summary": {
                "persisted_status_counts": self.persisted_status_counts,
                "proposed_status_counts": self.proposed_status_counts,
                "persisted_reason_counts": self.persisted_reason_counts,
                "proposed_reason_counts": self.proposed_reason_counts,
                "status_transitions": self.status_transitions,
                "total_status_changes": self.total_status_changes,
                "persisted_dimension_counts": self.persisted_dimension_counts,
                "proposed_dimension_counts": self.proposed_dimension_counts,
                "persisted_confidence_counts": self.persisted_confidence_counts,
                "proposed_confidence_counts": self.proposed_confidence_counts,
                "average_persisted_confidence": float(self.average_persisted_confidence),
                "average_proposed_confidence": float(self.average_proposed_confidence),
                "missing_evidence_counts": self.missing_evidence_counts,
                "confirmed_failure_counts": self.confirmed_failure_counts,
            },
            "job_comparisons": [
                {
                    "job_id": str(comp.job_id),
                    "source_job_id": comp.source_job_id,
                    "title": comp.title,
                    "location": comp.location,
                    "normalized_location": comp.normalized_location,
                    "company_name": comp.company_name,
                    "persisted": {
                        "status": comp.persisted_status,
                        "rejection_reason": comp.persisted_rejection_reason,
                        "review_reason": comp.persisted_review_reason,
                        "role_priority": comp.persisted_role_priority,
                        "relevance_status": comp.persisted_relevance_status,
                        "confidence": float(comp.persisted_verification_confidence),
                        "verified": comp.persisted_verified,
                        "dimension_flags": comp.persisted_dimension_flags,
                    },
                    "proposed": {
                        "status": comp.proposed_status,
                        "rejection_reason": comp.proposed_rejection_reason,
                        "review_reason": comp.proposed_review_reason,
                        "role_priority": comp.proposed_role_priority,
                        "relevance_status": comp.proposed_relevance_status,
                        "confidence": float(comp.proposed_verification_confidence),
                        "verified": comp.proposed_verified,
                        "dimension_flags": comp.proposed_dimension_flags,
                        "dimensions": comp.proposed_dimensions,
                    },
                    "changed": {
                        "status": comp.status_changed,
                        "reason": comp.reason_changed,
                        "confidence": comp.confidence_changed,
                        "verification_passed": comp.verification_passed_changed,
                    },
                    "missing_evidence": comp.missing_evidence,
                    "confirmed_failures": comp.confirmed_failures,
                }
                for comp in self.job_comparisons
            ],
        }


class JobReevaluationService:
    """Read-only orchestration service for re-evaluating stored jobs under latest rules."""

    def __init__(
        self,
        verification_evaluator: JobVerificationEvaluator | None = None,
        rule_engine: JobFilterRuleEngine | None = None,
    ) -> None:
        """Initialize service with evaluation engines."""
        self.verification_evaluator = verification_evaluator or JobVerificationEvaluator()
        self.rule_engine = rule_engine or JobFilterRuleEngine()

    def reevaluate_job(
        self,
        job: Job,
        *,
        evidence: JobVerificationInput | None = None,
        reference_time: datetime | None = None,
    ) -> JobReevaluationComparison:
        """Re-evaluate an individual canonical Job under latest verification and filter rules.

        Pure deterministic evaluation without database mutation.
        """
        job_ref_time = reference_time or job.collected_at or datetime.now(UTC)

        # 1. Resolve source provenance from linked relationships
        ref: JobSourceReference | None = None
        if hasattr(job, "source_references") and job.source_references:
            ref = next((r for r in job.source_references if r.is_primary), None)
            if ref is None:
                ref = job.source_references[0]

        raw_job: RawJob | None = ref.raw_job if ref else None
        company_source: CompanySource | None = raw_job.company_source if raw_job else None
        source: Source | None = (
            company_source.source if company_source else (ref.source if ref else None)
        )

        source_name = source.source_name if source else None
        source_type = source.source_type if source else None
        is_supported_platform = bool(
            source_name and source_name.strip().lower() in APPROVED_SOURCE_TYPES
        )
        is_source_approved = bool(
            source
            and source.is_active
            and (company_source.is_active if company_source else True)
            and is_supported_platform
        )

        http_status_code = raw_job.http_status if raw_job else None
        payload_format_valid = bool(raw_job and raw_job.payload_format in ("JSON", "HTML"))

        # 2. Location normalization with latest rules
        _, norm_loc, _ = normalize_location(job.location)

        # 3. Construct or use explicit JobVerificationInput
        if evidence is not None:
            v_input = evidence
        else:
            v_input = JobVerificationInput(
                job_id=job.id,
                source_id=source.id if source else None,
                source_name=source_name,
                source_type=source_type,
                is_source_approved=is_source_approved,
                payload_format_valid=payload_format_valid,
                description=job.description,
                http_status_code=http_status_code,
                is_page_active=(http_status_code == 200) if http_status_code else None,
                application_url=job.application_url,
                source_url=ref.source_url if ref else None,
                company_name=job.company_name,
                company_id=job.company_id,
                location=job.location,
                normalized_location=norm_loc,
                remote_type=job.remote_type or "UNKNOWN",
                experience_raw=job.experience_raw,
                experience_min_years=job.experience_min_years,
                experience_max_years=job.experience_max_years,
                salary_min=job.salary_min,
                salary_max=job.salary_max,
                salary_currency=job.salary_currency or "INR",
                salary_period=job.salary_period,
                posted_at=job.posted_at,
                collected_at=job.collected_at,
            )

        v_result: JobVerificationResult = self.verification_evaluator.evaluate(
            v_input,
            reference_time=job_ref_time,
        )

        # 4. Construct JobFilterInput and evaluate eligibility
        posting_date_verif = (
            v_result.dimensions["posting_date_authenticity"].verified
            if "posting_date_authenticity" in v_result.dimensions
            else None
        )
        f_input = JobFilterInput(
            title=job.title,
            normalized_title=job.normalized_title,
            description=job.description or "",
            company_name=job.company_name,
            company_id=job.company_id,
            location=job.location,
            normalized_location=norm_loc,
            employment_type=job.employment_type,
            remote_type=job.remote_type or "UNKNOWN",
            experience_raw=job.experience_raw,
            experience_min_years=job.experience_min_years,
            experience_max_years=job.experience_max_years,
            salary_min=job.salary_min,
            salary_max=job.salary_max,
            salary_currency=job.salary_currency or "INR",
            salary_period=job.salary_period,
            posted_at=job.posted_at,
            collected_at=job.collected_at,
            posting_date_verified=posting_date_verif,
        )
        f_result: JobFilterResult = self.rule_engine.evaluate(
            f_input,
            reference_time=job_ref_time,
        )

        # 5. Extract categorized evidence and failures
        missing_evidence: list[str] = []
        confirmed_failures: list[str] = []

        # Missing evidence categorization
        if job.salary_min is None and job.salary_max is None:
            missing_evidence.append(REVIEW_SALARY_UNKNOWN)
        if job.experience_min_years is None and job.experience_max_years is None:
            missing_evidence.append(REVIEW_EXPERIENCE_AMBIGUOUS)
        if not v_result.company_verified:
            missing_evidence.append(REVIEW_COMPANY_UNVERIFIED)
        if (
            v_result.dimensions.get("application_url_validity")
            and v_result.dimensions["application_url_validity"].details.get("primary_url_status")
            == "unprobed_syntax_valid"
        ):
            missing_evidence.append("unprobed_syntax_valid")
        if job.posted_at is None:
            missing_evidence.append(REVIEW_POSTED_DATE_SUSPICIOUS)
        if norm_loc is None and job.location:
            missing_evidence.append("location_unlisted_or_ambiguous")

        # Confirmed failures categorization
        if not v_result.source_verified and v_result.failure_reasons:
            for fr in v_result.failure_reasons:
                if fr not in confirmed_failures:
                    confirmed_failures.append(fr)
        if not v_result.jd_verified and v_result.failure_reasons:
            for fr in v_result.failure_reasons:
                if fr not in confirmed_failures:
                    confirmed_failures.append(fr)
        if f_result.rejection_reason:
            confirmed_failures.append(f_result.rejection_reason)

        # 6. Build comparison
        pers_status = job.eligibility_status
        pers_rej_reason = job.rejection_reason
        pers_rev_reason = job.review_reason
        pers_role_priority = (
            job.role_priority if isinstance(getattr(job, "role_priority", None), str) else None
        )
        pers_relevance_status = (
            job.relevance_status
            if isinstance(getattr(job, "relevance_status", None), str)
            else None
        )
        pers_conf = Decimal(str(job.verification_confidence))
        pers_verified = bool(job.jd_verified and job.source_verified)

        prop_status = f_result.eligibility_status
        prop_rej_reason = f_result.rejection_reason
        prop_rev_reason = f_result.review_reason
        prop_role_priority = (
            str(f_result.role_priority) if f_result.role_priority is not None else None
        )
        prop_relevance_status = (
            str(f_result.relevance_status) if f_result.relevance_status is not None else None
        )
        prop_conf = v_result.verification_confidence
        prop_verified = v_result.passed

        dim_details: dict[str, Any] = {}
        for d_name, d_res in v_result.dimensions.items():
            dim_details[d_name] = {
                "verified": d_res.verified,
                "score": float(d_res.score),
                "failure_reasons": d_res.failure_reasons,
                "review_reasons": d_res.review_reasons,
            }

        pers_flags = {
            "source_verified": bool(job.source_verified),
            "jd_verified": bool(job.jd_verified),
            "company_verified": bool(job.company_verified),
            "location_verified": bool(job.location_verified),
            "experience_verified": bool(job.experience_verified),
            "salary_verified": bool(job.salary_verified),
        }
        prop_flags = {
            "source_verified": v_result.source_verified,
            "jd_verified": v_result.jd_verified,
            "application_url_verified": v_result.application_url_verified,
            "company_verified": v_result.company_verified,
            "location_verified": v_result.location_verified,
            "experience_verified": v_result.experience_verified,
            "salary_verified": v_result.salary_verified,
            "posting_date_verified": v_result.posting_date_verified,
        }

        return JobReevaluationComparison(
            job_id=job.id,
            source_job_id=ref.source_job_id if ref else None,
            title=job.title,
            location=job.location,
            normalized_location=norm_loc,
            company_name=job.company_name,
            persisted_status=pers_status,
            persisted_rejection_reason=pers_rej_reason,
            persisted_review_reason=pers_rev_reason,
            persisted_role_priority=pers_role_priority,
            persisted_relevance_status=pers_relevance_status,
            persisted_verification_confidence=pers_conf,
            persisted_verified=pers_verified,
            persisted_dimension_flags=pers_flags,
            proposed_status=prop_status,
            proposed_rejection_reason=prop_rej_reason,
            proposed_review_reason=prop_rev_reason,
            proposed_role_priority=prop_role_priority,
            proposed_relevance_status=prop_relevance_status,
            proposed_verification_confidence=prop_conf,
            proposed_verified=prop_verified,
            proposed_dimension_flags=prop_flags,
            proposed_dimensions=dim_details,
            status_changed=(pers_status != prop_status),
            reason_changed=(
                pers_rej_reason != prop_rej_reason or pers_rev_reason != prop_rev_reason
            ),
            confidence_changed=(pers_conf != prop_conf),
            verification_passed_changed=(pers_verified != prop_verified),
            missing_evidence=missing_evidence,
            confirmed_failures=confirmed_failures,
        )

    async def reevaluate_company(
        self,
        session: AsyncSession,
        company_id_or_name: uuid.UUID | str,
        *,
        dry_run: bool = True,
        reference_time: datetime | None = None,
        limit: int | None = None,
    ) -> ReevaluationReport:
        """Evaluate all canonical jobs for a company.

        Safety guard: Strictly requires dry_run=True. Raises NotImplementedError if dry_run=False.
        """
        if not dry_run:
            raise NotImplementedError(
                "Mutating re-evaluation is disabled in V1 dry-run mode. "
                "Only read-only dry-run evaluation (dry_run=True) is supported."
            )

        company_repo = CompanyRepository(session)
        target_company = None

        if isinstance(company_id_or_name, uuid.UUID):
            target_company = await company_repo.get_by_id(company_id_or_name)
        else:
            raw_str = str(company_id_or_name).strip()
            try:
                parsed_uuid = uuid.UUID(raw_str)
                target_company = await company_repo.get_by_id(parsed_uuid)
            except ValueError:
                try:
                    slug = normalize_company_name(raw_str)
                    target_company = await company_repo.get_by_normalized_name(slug)
                except ValueError:
                    target_company = None

                if target_company is None:
                    stmt = select(Company).where(func.lower(Company.name) == raw_str.lower())
                    res = await session.execute(stmt)
                    target_company = res.scalar_one_or_none()

        if target_company is None:
            raise ValueError(f"Company '{company_id_or_name}' not found in database.")

        # Eager load canonical jobs and full provenance tree
        stmt = (
            select(Job)
            .options(
                selectinload(Job.source_references)
                .selectinload(JobSourceReference.raw_job)
                .selectinload(RawJob.company_source)
                .selectinload(CompanySource.source),
                selectinload(Job.source_references).selectinload(JobSourceReference.source),
            )
            .where(Job.company_id == target_company.id)
            .order_by(Job.title)
        )
        if limit is not None and limit > 0:
            stmt = stmt.limit(limit)

        exec_result = await session.execute(stmt)
        jobs: Sequence[Job] = exec_result.scalars().all()

        comparisons: list[JobReevaluationComparison] = []
        pers_statuses: dict[str, int] = {}
        prop_statuses: dict[str, int] = {}
        pers_reasons: dict[str, int] = {}
        prop_reasons: dict[str, int] = {}
        transitions: dict[str, int] = {}

        pers_dim_counts = {
            "source_validity": 0,
            "jd_existence": 0,
            "company_legitimacy": 0,
            "location_consistency": 0,
            "experience_consistency": 0,
            "salary_statedness": 0,
        }
        prop_dim_counts = {
            "source_validity": 0,
            "jd_existence": 0,
            "application_url_validity": 0,
            "company_legitimacy": 0,
            "location_consistency": 0,
            "experience_consistency": 0,
            "salary_statedness": 0,
            "posting_date_authenticity": 0,
        }

        pers_conf_counts: dict[str, int] = {}
        prop_conf_counts: dict[str, int] = {}
        total_pers_conf = Decimal("0.00")
        total_prop_conf = Decimal("0.00")

        missing_counts: dict[str, int] = {}
        failure_counts: dict[str, int] = {}

        for job in jobs:
            comp = self.reevaluate_job(job, reference_time=reference_time)
            comparisons.append(comp)

            # Statuses
            pers_statuses[comp.persisted_status] = pers_statuses.get(comp.persisted_status, 0) + 1
            prop_statuses[comp.proposed_status] = prop_statuses.get(comp.proposed_status, 0) + 1

            # Transitions
            trans_key = f"{comp.persisted_status} -> {comp.proposed_status}"
            transitions[trans_key] = transitions.get(trans_key, 0) + 1

            # Reasons
            pers_reason = comp.persisted_rejection_reason or comp.persisted_review_reason or "NONE"
            pers_reasons[pers_reason] = pers_reasons.get(pers_reason, 0) + 1

            prop_reason = comp.proposed_rejection_reason or comp.proposed_review_reason or "NONE"
            prop_reasons[prop_reason] = prop_reasons.get(prop_reason, 0) + 1

            # Persisted dimensions
            if comp.persisted_dimension_flags.get("source_verified"):
                pers_dim_counts["source_validity"] += 1
            if comp.persisted_dimension_flags.get("jd_verified"):
                pers_dim_counts["jd_existence"] += 1
            if comp.persisted_dimension_flags.get("company_verified"):
                pers_dim_counts["company_legitimacy"] += 1
            if comp.persisted_dimension_flags.get("location_verified"):
                pers_dim_counts["location_consistency"] += 1
            if comp.persisted_dimension_flags.get("experience_verified"):
                pers_dim_counts["experience_consistency"] += 1
            if comp.persisted_dimension_flags.get("salary_verified"):
                pers_dim_counts["salary_statedness"] += 1

            # Proposed dimensions
            if comp.proposed_dimension_flags.get("source_verified"):
                prop_dim_counts["source_validity"] += 1
            if comp.proposed_dimension_flags.get("jd_verified"):
                prop_dim_counts["jd_existence"] += 1
            if comp.proposed_dimension_flags.get("application_url_verified"):
                prop_dim_counts["application_url_validity"] += 1
            if comp.proposed_dimension_flags.get("company_verified"):
                prop_dim_counts["company_legitimacy"] += 1
            if comp.proposed_dimension_flags.get("location_verified"):
                prop_dim_counts["location_consistency"] += 1
            if comp.proposed_dimension_flags.get("experience_verified"):
                prop_dim_counts["experience_consistency"] += 1
            if comp.proposed_dimension_flags.get("salary_verified"):
                prop_dim_counts["salary_statedness"] += 1
            if comp.proposed_dimension_flags.get("posting_date_verified"):
                prop_dim_counts["posting_date_authenticity"] += 1

            # Confidence
            p_conf_str = f"{comp.persisted_verification_confidence:.2f}"
            pers_conf_counts[p_conf_str] = pers_conf_counts.get(p_conf_str, 0) + 1
            total_pers_conf += comp.persisted_verification_confidence

            pr_conf_str = f"{comp.proposed_verification_confidence:.2f}"
            prop_conf_counts[pr_conf_str] = prop_conf_counts.get(pr_conf_str, 0) + 1
            total_prop_conf += comp.proposed_verification_confidence

            # Evidence counts
            for m in comp.missing_evidence:
                missing_counts[m] = missing_counts.get(m, 0) + 1
            for f in comp.confirmed_failures:
                failure_counts[f] = failure_counts.get(f, 0) + 1

        total_jobs_len = len(jobs)
        avg_pers_conf = (
            (total_pers_conf / Decimal(str(total_jobs_len))).quantize(Decimal("0.001"))
            if total_jobs_len > 0
            else Decimal("0.000")
        )
        avg_prop_conf = (
            (total_prop_conf / Decimal(str(total_jobs_len))).quantize(Decimal("0.001"))
            if total_jobs_len > 0
            else Decimal("0.000")
        )

        total_status_changes = sum(1 for c in comparisons if c.status_changed)

        report_ref_time = reference_time or datetime.now(UTC)

        return ReevaluationReport(
            company_id=target_company.id,
            company_name=target_company.name,
            total_jobs=total_jobs_len,
            reference_time=report_ref_time,
            dry_run=True,
            persisted_status_counts=pers_statuses,
            proposed_status_counts=prop_statuses,
            persisted_reason_counts=pers_reasons,
            proposed_reason_counts=prop_reasons,
            status_transitions=transitions,
            persisted_dimension_counts=pers_dim_counts,
            proposed_dimension_counts=prop_dim_counts,
            persisted_confidence_counts=pers_conf_counts,
            proposed_confidence_counts=prop_conf_counts,
            average_persisted_confidence=avg_pers_conf,
            average_proposed_confidence=avg_prop_conf,
            missing_evidence_counts=missing_counts,
            confirmed_failure_counts=failure_counts,
            total_status_changes=total_status_changes,
            job_comparisons=comparisons,
        )
