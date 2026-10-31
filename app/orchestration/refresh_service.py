"""Controlled persistence refresh orchestration service.

Boundary: Orchestrates applying approved re-evaluation results to canonical Job entities,
appending immutable historical VerificationRecord and FilterDecision audit rows in PostgreSQL.
Strict safety:
- Idempotency: Detects already up-to-date states and skips redundant updates.
- Atomic transaction: Applies all company updates in a single caller-controlled transaction.
- Database identity guard: Validates connected database against expected target before execution.
- Development database block: Refuses mutating refresh against 'job_intelligence_dev'.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cli.greenhouse import normalize_company_name
from app.normalization.location import normalize_location
from app.orchestration.reevaluation_service import (
    JobReevaluationComparison,
    JobReevaluationService,
)
from app.persistence.models.company import Company
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource
from app.persistence.repositories.company_repository import CompanyRepository
from app.rules.engine import JobFilterRuleEngine
from app.rules.schemas import JobFilterInput
from app.verification.evaluator import (
    APPROVED_SOURCE_TYPES,
    JobVerificationEvaluator,
)
from app.verification.schemas import JobVerificationInput

# Restrict mutating refresh execution to isolated test database only by default
ALLOWED_MUTATING_DATABASES: frozenset[str] = frozenset({"job_intelligence_test"})

# Scoped authorization parameters for controlled development database refresh
DEV_DATABASE_NAME: str = "job_intelligence_dev"
FIGMA_COMPANY_ID: uuid.UUID = uuid.UUID("4916b6da-2803-4a9e-b3ce-dd41ef2a5324")
FIGMA_TEL_AVIV_JOB_ID: uuid.UUID = uuid.UUID("bdf7f339-da99-44e2-9b2d-fe4a7738f65d")
# Fixed manual confirmation phrase (not a secret credential) required for dev override confirmation
DEV_AUTHORIZATION_TOKEN_ENV_VAR: str = "FIGMA_REFRESH_AUTHORIZATION_TOKEN"
EXPECTED_DEV_AUTHORIZATION_TOKEN: str = "AUTHORIZE_FIGMA_REFRESH_DEV_05377f2ddb14"

logger = logging.getLogger(__name__)


class RefreshItemSummary(BaseModel):
    """Detailed summary of an individual job's pending or applied refresh."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    job_id: uuid.UUID
    source_job_id: str | None = None
    title: str
    location: str | None = None
    action: str  # "UPDATE" or "SKIP"
    persisted_status: str
    proposed_status: str
    persisted_rejection_reason: str | None = None
    proposed_rejection_reason: str | None = None
    persisted_review_reason: str | None = None
    proposed_review_reason: str | None = None
    persisted_confidence: Decimal
    proposed_confidence: Decimal
    status_changed: bool
    reason_changed: bool
    confidence_changed: bool
    verification_passed_changed: bool
    source_verified_changed: bool


class RefreshPlan(BaseModel):
    """Preview plan specifying changes to be persisted during refresh."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    company_id: uuid.UUID
    company_name: str
    current_database: str
    total_jobs: int
    jobs_to_update: int
    jobs_to_skip: int
    verification_records_to_insert: int
    filter_decisions_to_insert: int
    is_already_up_to_date: bool
    status_transitions: dict[str, int]
    items: list[RefreshItemSummary]

    def to_dict(self) -> dict[str, Any]:
        """Convert plan to serializable dictionary."""
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "current_database": self.current_database,
            "total_jobs": self.total_jobs,
            "jobs_to_update": self.jobs_to_update,
            "jobs_to_skip": self.jobs_to_skip,
            "verification_records_to_insert": self.verification_records_to_insert,
            "filter_decisions_to_insert": self.filter_decisions_to_insert,
            "is_already_up_to_date": self.is_already_up_to_date,
            "status_transitions": self.status_transitions,
            "items": [
                {
                    "job_id": str(item.job_id),
                    "source_job_id": item.source_job_id,
                    "title": item.title,
                    "location": item.location,
                    "action": item.action,
                    "persisted_status": item.persisted_status,
                    "proposed_status": item.proposed_status,
                    "persisted_rejection_reason": item.persisted_rejection_reason,
                    "proposed_rejection_reason": item.proposed_rejection_reason,
                    "persisted_review_reason": item.persisted_review_reason,
                    "proposed_review_reason": item.proposed_review_reason,
                    "persisted_confidence": float(item.persisted_confidence),
                    "proposed_confidence": float(item.proposed_confidence),
                    "status_changed": item.status_changed,
                    "reason_changed": item.reason_changed,
                    "confidence_changed": item.confidence_changed,
                    "verification_passed_changed": item.verification_passed_changed,
                    "source_verified_changed": item.source_verified_changed,
                }
                for item in self.items
            ],
        }


class RefreshResult(BaseModel):
    """Execution outcome of a controlled persistence refresh."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    company_id: uuid.UUID
    company_name: str
    target_database: str
    status: str  # "SUCCESS", "SKIPPED_ALREADY_APPLIED", "FAILED"
    applied: bool
    total_jobs: int
    jobs_updated: int
    jobs_skipped: int
    verification_records_inserted: int
    filter_decisions_inserted: int
    refreshed_at: datetime
    plan: RefreshPlan

    def to_dict(self) -> dict[str, Any]:
        """Convert result to serializable dictionary."""
        return {
            "company_id": str(self.company_id),
            "company_name": self.company_name,
            "target_database": self.target_database,
            "status": self.status,
            "applied": self.applied,
            "total_jobs": self.total_jobs,
            "jobs_updated": self.jobs_updated,
            "jobs_skipped": self.jobs_skipped,
            "verification_records_inserted": self.verification_records_inserted,
            "filter_decisions_inserted": self.filter_decisions_inserted,
            "refreshed_at": self.refreshed_at.isoformat(),
            "plan_summary": {
                "total_jobs": self.plan.total_jobs,
                "jobs_to_update": self.plan.jobs_to_update,
                "jobs_to_skip": self.plan.jobs_to_skip,
                "status_transitions": self.plan.status_transitions,
            },
        }


# Module-private sentinel protecting ScopedDevAuthorization against accidental construction
_SCOPED_AUTH_SENTINEL = object()


class ScopedDevAuthorization:
    """Explicit, bound authorization proof issued strictly by JobRefreshService after all 6 gate checks pass.

    Construction is restricted to JobRefreshService._authorize_development_refresh() via a module-private
    sentinel token, protecting against accidental construction across module boundaries. This provides
    architectural protection against bypass within the application, not a cryptographically unforgeable
    capability.
    """

    def __init__(
        self,
        *,
        company_id: uuid.UUID,
        company_name: str,
        database_name: str,
        _private_token: object = None,
    ) -> None:
        """Private constructor: raises PermissionError if private sentinel is not supplied."""
        if _private_token is not _SCOPED_AUTH_SENTINEL:
            raise PermissionError(
                "ScopedDevAuthorization cannot be constructed directly. "
                "It must be issued by JobRefreshService._authorize_development_refresh()."
            )
        if company_id != FIGMA_COMPANY_ID:
            raise PermissionError(
                f"Scoped authorization company ID mismatch: expected {FIGMA_COMPANY_ID}, got {company_id}."
            )
        if database_name.strip().lower() != DEV_DATABASE_NAME:
            raise PermissionError(
                f"Scoped authorization database mismatch: expected '{DEV_DATABASE_NAME}', got '{database_name}'."
            )
        self._company_id = company_id
        self._company_name = company_name
        self._database_name = database_name.strip().lower()

    @property
    def company_id(self) -> uuid.UUID:
        """Return the authorized company UUID."""
        return self._company_id

    @property
    def company_name(self) -> str:
        """Return the authorized company name."""
        return self._company_name

    @property
    def database_name(self) -> str:
        """Return the authorized database name."""
        return self._database_name

    def verify_authority(self, target_company_id: uuid.UUID, target_database: str) -> None:
        """Verify that authorization token is strictly bound to the target company and database context."""
        if self._company_id != FIGMA_COMPANY_ID or self._company_id != target_company_id:
            raise PermissionError(
                f"Scoped authorization context mismatch: token is bound to {self._company_id}, "
                f"but target is {target_company_id}."
            )
        target_db_norm = target_database.strip().lower()
        if self._database_name != DEV_DATABASE_NAME or self._database_name != target_db_norm:
            raise PermissionError(
                f"Scoped authorization context mismatch: token is bound to '{self._database_name}', "
                f"but target is '{target_database}'."
            )

    def __repr__(self) -> str:
        return (
            f"<ScopedDevAuthorization company_id={self._company_id} database={self._database_name}>"
        )


class JobRefreshService:
    """Orchestrates controlled persistence refresh of verification and filter decisions."""

    def __init__(
        self,
        reevaluation_service: JobReevaluationService | None = None,
        verification_evaluator: JobVerificationEvaluator | None = None,
        rule_engine: JobFilterRuleEngine | None = None,
    ) -> None:
        """Initialize service with re-evaluation components."""
        self.reevaluation_service = reevaluation_service or JobReevaluationService(
            verification_evaluator=verification_evaluator,
            rule_engine=rule_engine,
        )
        self.verification_evaluator = (
            verification_evaluator or self.reevaluation_service.verification_evaluator
        )
        self.rule_engine = rule_engine or self.reevaluation_service.rule_engine

    @classmethod
    def _authorize_development_refresh(
        cls,
        *,
        company: Company,
        current_db: str,
        expected_db: str,
        allow_database_override: str | None,
        authorization_token: str | None,
    ) -> ScopedDevAuthorization:
        """Evaluate all 6 scoped authorization checks and issue an authorized ScopedDevAuthorization token.

        Raises PermissionError or ValueError if any check fails.
        """
        effective_token = authorization_token or os.environ.get(DEV_AUTHORIZATION_TOKEN_ENV_VAR)
        override_norm = (allow_database_override or "").strip().lower()
        current_db_norm = current_db.strip().lower()
        expected_db_norm = expected_db.strip().lower()

        # 1. Override flag
        if override_norm != DEV_DATABASE_NAME:
            raise PermissionError(
                f"Mutating refresh execution is strictly restricted to allowlisted test database "
                f"{sorted(ALLOWED_MUTATING_DATABASES)}. Execution against '{DEV_DATABASE_NAME}' "
                f"is blocked without explicit override (allow_database_override='{DEV_DATABASE_NAME}')."
            )

        # 2. Confirmation phrase (manual confirmation phrase, not a secret credential)
        if effective_token != EXPECTED_DEV_AUTHORIZATION_TOKEN:
            raise PermissionError(
                f"Mutating execution against '{DEV_DATABASE_NAME}' requires valid authorization token. "
                f"Invalid or missing {DEV_AUTHORIZATION_TOKEN_ENV_VAR}."
            )

        # 3. Expected database
        if expected_db_norm != DEV_DATABASE_NAME:
            raise ValueError(
                f"Database identity guard failed: target database specified as '{expected_db}', "
                f"expected '{DEV_DATABASE_NAME}'."
            )

        # 4. Connected database
        if current_db_norm != DEV_DATABASE_NAME:
            raise ValueError(
                f"Database identity guard failed: currently connected to '{current_db}', "
                f"expected '{DEV_DATABASE_NAME}'."
            )

        # 5. Figma UUID
        if company.id != FIGMA_COMPANY_ID:
            raise PermissionError(
                f"Scoped database override for '{DEV_DATABASE_NAME}' is strictly restricted to "
                f"Figma (ID {FIGMA_COMPANY_ID}, slug 'figma'). "
                f"Target was '{company.name}' ({company.id}). Refusing execution."
            )

        # 6. Figma slug
        if company.normalized_name != "figma":
            raise PermissionError(
                f"Scoped database override for '{DEV_DATABASE_NAME}' is strictly restricted to "
                f"Figma (ID {FIGMA_COMPANY_ID}, slug 'figma'). "
                f"Target slug was '{company.normalized_name}'. Refusing execution."
            )

        return ScopedDevAuthorization(
            company_id=company.id,
            company_name=company.name,
            database_name=current_db_norm,
            _private_token=_SCOPED_AUTH_SENTINEL,
        )

    async def get_current_database(self, session: AsyncSession) -> str:
        """Query and return the current PostgreSQL database name."""
        stmt = text("SELECT current_database();")
        res = await session.execute(stmt)
        return str(res.scalar() or "UNKNOWN")

    async def _resolve_company(
        self,
        session: AsyncSession,
        company_id_or_name: uuid.UUID | str,
    ) -> Company:
        """Resolve Company entity by UUID or name/slug."""
        company_repo = CompanyRepository(session)
        target_company: Company | None = None

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

        return target_company

    async def preview_refresh(
        self,
        session: AsyncSession,
        company_id_or_name: uuid.UUID | str,
        *,
        target_database: str | None = None,
    ) -> RefreshPlan:
        """Compute read-only preview plan of updates to be persisted.

        Zero database mutations.
        """
        current_db = await self.get_current_database(session)
        if target_database and target_database.strip().lower() != current_db.strip().lower():
            raise ValueError(
                f"Database identity mismatch: connected to '{current_db}', "
                f"but target-database was specified as '{target_database}'."
            )

        company = await self._resolve_company(session, company_id_or_name)

        # Load canonical jobs with full provenance tree
        stmt = (
            select(Job)
            .options(
                selectinload(Job.source_references)
                .selectinload(JobSourceReference.raw_job)
                .selectinload(RawJob.company_source)
                .selectinload(CompanySource.source),
                selectinload(Job.source_references).selectinload(JobSourceReference.source),
            )
            .where(Job.company_id == company.id)
            .order_by(Job.title)
        )
        res = await session.execute(stmt)
        jobs: Sequence[Job] = res.scalars().all()

        items: list[RefreshItemSummary] = []
        jobs_to_update = 0
        jobs_to_skip = 0
        status_transitions: dict[str, int] = {}

        for job in jobs:
            comp: JobReevaluationComparison = self.reevaluation_service.reevaluate_job(job)

            # Check if this job requires an update.
            # Comprehensive check across EVERY canonical Job operational attribute
            # and verification flag that apply_refresh modifies:
            # 1. Eligibility & Operational status differences
            status_diff = comp.status_changed or (job.eligibility_status != comp.proposed_status)
            reason_diff = (
                comp.reason_changed
                or ((job.rejection_reason or None) != (comp.proposed_rejection_reason or None))
                or ((job.review_reason or None) != (comp.proposed_review_reason or None))
            )
            role_priority_diff = (job.role_priority or None) != (
                comp.proposed_role_priority or None
            )
            relevance_status_diff = (job.relevance_status or None) != (
                comp.proposed_relevance_status or None
            )

            # 2. Location normalization difference
            norm_loc_diff = (job.normalized_location or None) != (comp.normalized_location or None)

            # 3. Verification confidence and passed status
            confidence_diff = comp.confidence_changed or (
                Decimal(str(job.verification_confidence)) != comp.proposed_verification_confidence
            )
            verification_passed_diff = comp.verification_passed_changed

            # 4. Dimension verification flags differences (all 6 dimension columns on canonical Job)
            dim_flags_diff = any(
                bool(getattr(job, dim, False)) != comp.proposed_dimension_flags.get(dim, False)
                for dim in (
                    "source_verified",
                    "jd_verified",
                    "company_verified",
                    "location_verified",
                    "experience_verified",
                    "salary_verified",
                )
            )

            needs_update = (
                status_diff
                or reason_diff
                or role_priority_diff
                or relevance_status_diff
                or norm_loc_diff
                or confidence_diff
                or verification_passed_diff
                or dim_flags_diff
            )

            action = "UPDATE" if needs_update else "SKIP"
            if needs_update:
                jobs_to_update += 1
                trans_key = f"{comp.persisted_status} -> {comp.proposed_status}"
                status_transitions[trans_key] = status_transitions.get(trans_key, 0) + 1
            else:
                jobs_to_skip += 1

            source_verified_diff = (
                bool(job.source_verified) != comp.proposed_dimension_flags["source_verified"]
            )

            items.append(
                RefreshItemSummary(
                    job_id=job.id,
                    source_job_id=comp.source_job_id,
                    title=job.title,
                    location=job.location,
                    action=action,
                    persisted_status=comp.persisted_status,
                    proposed_status=comp.proposed_status,
                    persisted_rejection_reason=comp.persisted_rejection_reason,
                    proposed_rejection_reason=comp.proposed_rejection_reason,
                    persisted_review_reason=comp.persisted_review_reason,
                    proposed_review_reason=comp.proposed_review_reason,
                    persisted_confidence=comp.persisted_verification_confidence,
                    proposed_confidence=comp.proposed_verification_confidence,
                    status_changed=comp.status_changed,
                    reason_changed=comp.reason_changed,
                    confidence_changed=comp.confidence_changed,
                    verification_passed_changed=comp.verification_passed_changed,
                    source_verified_changed=source_verified_diff,
                )
            )

        total_jobs = len(jobs)
        is_already_up_to_date = bool(total_jobs > 0 and jobs_to_update == 0)

        return RefreshPlan(
            company_id=company.id,
            company_name=company.name,
            current_database=current_db,
            total_jobs=total_jobs,
            jobs_to_update=jobs_to_update,
            jobs_to_skip=jobs_to_skip,
            verification_records_to_insert=jobs_to_update,
            filter_decisions_to_insert=jobs_to_update,
            is_already_up_to_date=is_already_up_to_date,
            status_transitions=status_transitions,
            items=items,
        )

    async def apply_refresh(
        self,
        session: AsyncSession,
        company_id_or_name: uuid.UUID | str,
        *,
        expected_database: str,
        dry_run: bool = False,
        allow_database_override: str | None = None,
        authorization_token: str | None = None,
    ) -> RefreshResult:
        """Apply approved re-evaluation results transactionally to canonical jobs and append audit rows.

        Strict Guards:
        1. Refuses execution if target or connected database is not allowlisted ('job_intelligence_test').
           Mutating execution against 'job_intelligence_dev' is strictly blocked unless ALL 6 scoped
           authorization conditions are met:
           - allow_database_override == 'job_intelligence_dev'
           - valid FIGMA_REFRESH_AUTHORIZATION_TOKEN
           - expected_database == 'job_intelligence_dev'
           - connected database == 'job_intelligence_dev'
           - target company ID == Figma UUID (4916b6da-2803-4a9e-b3ce-dd41ef2a5324)
           - target company normalized_name == 'figma'
        2. Refuses execution if current_database != expected_database.
        3. Idempotently skips if plan.is_already_up_to_date is True.
        4. Pre-commit invariant validation: asserts all status distributions and audit counts in-transaction.
        5. Single transaction lifecycle: caller owns session commit/rollback.
        """
        current_db = await self.get_current_database(session)
        current_db_norm = current_db.strip().lower()
        expected_db_norm = expected_database.strip().lower()

        is_dev_override_requested = (
            current_db_norm == DEV_DATABASE_NAME or expected_db_norm == DEV_DATABASE_NAME
        )

        target_company: Company | None = None
        dev_authorization: ScopedDevAuthorization | None = None

        if is_dev_override_requested:
            target_company = await self._resolve_company(session, company_id_or_name)
            dev_authorization = self._authorize_development_refresh(
                company=target_company,
                current_db=current_db,
                expected_db=expected_database,
                allow_database_override=allow_database_override,
                authorization_token=authorization_token,
            )
        else:
            # 1. Strict allowlist protection: only job_intelligence_test is allowed for mutating refresh
            if current_db_norm not in ALLOWED_MUTATING_DATABASES:
                raise PermissionError(
                    f"Mutating refresh execution is strictly restricted to allowlisted test database "
                    f"{sorted(ALLOWED_MUTATING_DATABASES)}. Currently connected to '{current_db}'. "
                    f"Execution against '{current_db}' (including '{DEV_DATABASE_NAME}') is forbidden."
                )

            if expected_db_norm not in ALLOWED_MUTATING_DATABASES:
                raise PermissionError(
                    f"Mutating refresh execution is strictly restricted to allowlisted test database "
                    f"{sorted(ALLOWED_MUTATING_DATABASES)}. Target database was specified as "
                    f"'{expected_database}'. Refusing execution."
                )

            # 2. Database identity guard (must match connected database)
            if current_db_norm != expected_db_norm:
                raise ValueError(
                    f"Database identity guard failed: currently connected to '{current_db}', "
                    f"but expected_database was specified as '{expected_database}'. Refusing execution."
                )

        # 3. Dry-run assertion
        if dry_run:
            raise ValueError(
                "apply_refresh called with dry_run=True. Use preview_refresh for read-only inspection."
            )

        # 4. Generate plan
        plan = await self.preview_refresh(
            session=session,
            company_id_or_name=company_id_or_name,
            target_database=expected_database,
        )

        now_utc = datetime.now(UTC)

        # 5. Idempotency Check: if no updates needed, skip execution
        if plan.is_already_up_to_date or plan.jobs_to_update == 0:
            logger.info(
                "All %d jobs for %s are already up to date. Skipping refresh.",
                plan.total_jobs,
                plan.company_name,
            )
            return RefreshResult(
                company_id=plan.company_id,
                company_name=plan.company_name,
                target_database=current_db,
                status="SKIPPED_ALREADY_APPLIED",
                applied=False,
                total_jobs=plan.total_jobs,
                jobs_updated=0,
                jobs_skipped=plan.total_jobs,
                verification_records_inserted=0,
                filter_decisions_inserted=0,
                refreshed_at=now_utc,
                plan=plan,
            )

        # 6. Load jobs to update
        update_job_ids = {item.job_id for item in plan.items if item.action == "UPDATE"}
        stmt = (
            select(Job)
            .options(
                selectinload(Job.source_references)
                .selectinload(JobSourceReference.raw_job)
                .selectinload(RawJob.company_source)
                .selectinload(CompanySource.source),
                selectinload(Job.source_references).selectinload(JobSourceReference.source),
            )
            .where(Job.id.in_(update_job_ids))
        )
        res = await session.execute(stmt)
        jobs_to_process = res.scalars().all()

        v_records_count = 0
        f_decisions_count = 0

        for job in jobs_to_process:
            # Resolve provenance
            ref = next((r for r in job.source_references if r.is_primary), None)
            if ref is None and job.source_references:
                ref = job.source_references[0]

            raw_job = ref.raw_job if ref else None
            company_source = raw_job.company_source if raw_job else None
            source = company_source.source if company_source else (ref.source if ref else None)

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

            _, norm_loc, _ = normalize_location(job.location)
            job_ref_time = job.collected_at or now_utc

            # A. Re-evaluate Verification
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
            v_result = self.verification_evaluator.evaluate(v_input, reference_time=job_ref_time)

            # Append new historical VerificationRecord
            v_record_kwargs = v_result.to_verification_record_kwargs(
                job_id=job.id,
                verification_run_at=now_utc,
            )
            v_record = VerificationRecord(**v_record_kwargs)
            session.add(v_record)
            v_records_count += 1

            # B. Re-evaluate Filter Decisions
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
            f_result = self.rule_engine.evaluate(f_input, reference_time=job_ref_time)

            # Append new historical FilterDecision
            f_decision_kwargs = f_result.to_filter_decision_kwargs(job_id=job.id)
            f_decision_kwargs["evaluated_at"] = now_utc
            f_decision = FilterDecision(**f_decision_kwargs)
            session.add(f_decision)
            f_decisions_count += 1

            # C. Update canonical Job operational attributes
            status_updates = f_result.to_job_status_updates()
            for k, v in status_updates.items():
                if hasattr(job, k):
                    setattr(job, k, v)

            verif_updates = v_result.to_job_verification_updates()
            for k, v in verif_updates.items():
                if hasattr(job, k):
                    setattr(job, k, v)

            if norm_loc != job.normalized_location:
                job.normalized_location = norm_loc

            job.updated_at = now_utc

        # Flush dirty state to surface constraints without committing transaction
        await session.flush()

        # In-transaction pre-commit invariant validation (atomic rollback if any check fails)
        company = target_company or await self._resolve_company(session, company_id_or_name)
        await self._validate_pre_commit_invariants(
            session=session,
            company=company,
            plan=plan,
            jobs_to_process=jobs_to_process,
            now_utc=now_utc,
            current_database=current_db,
            dev_authorization=dev_authorization,
        )

        return RefreshResult(
            company_id=plan.company_id,
            company_name=plan.company_name,
            target_database=current_db,
            status="SUCCESS",
            applied=True,
            total_jobs=plan.total_jobs,
            jobs_updated=len(jobs_to_process),
            jobs_skipped=plan.jobs_to_skip,
            verification_records_inserted=v_records_count,
            filter_decisions_inserted=f_decisions_count,
            refreshed_at=now_utc,
            plan=plan,
        )

    async def _validate_pre_commit_invariants(
        self,
        session: AsyncSession,
        company: Company,
        plan: RefreshPlan,
        jobs_to_process: Sequence[Job],
        now_utc: datetime,
        current_database: str | None = None,
        dev_authorization: ScopedDevAuthorization | None = None,
    ) -> None:
        """Validate critical persistence invariants before committing transaction.

        Generic invariants execute for all companies on any database.
        Figma-specific assertions (138 jobs, 123/15 distribution, Tel Aviv attributes,
        per-job audit counts) execute strictly for the authorized Figma refresh on 'job_intelligence_dev',
        requiring an explicit ScopedDevAuthorization proof object (protected by private sentinel
        against accidental construction).

        Raises RuntimeError or PermissionError if any check fails, triggering transaction rollback.
        """
        # Generic pre-commit invariants for any company
        if len(jobs_to_process) != plan.jobs_to_update:
            raise RuntimeError(
                f"Pre-commit invariant violation: processed {len(jobs_to_process)} jobs, "
                f"expected {plan.jobs_to_update}."
            )

        for job in jobs_to_process:
            if not job.eligibility_status:
                raise RuntimeError(
                    f"Pre-commit invariant violation: job {job.id} has null eligibility_status."
                )
            if job.verification_confidence is None:
                raise RuntimeError(
                    f"Pre-commit invariant violation: job {job.id} has null verification_confidence."
                )
            if job.updated_at != now_utc:
                raise RuntimeError(
                    f"Pre-commit invariant violation: job {job.id} updated_at was not updated."
                )

        # Scoped invariants for development database execution
        active_db = (current_database or await self.get_current_database(session)).strip().lower()
        is_dev_db = active_db == DEV_DATABASE_NAME

        if is_dev_db:
            # Under NO circumstances can dev invariants be evaluated without verified authorization proof
            if dev_authorization is None:
                raise PermissionError(
                    f"Pre-commit invariants against '{DEV_DATABASE_NAME}' are forbidden without "
                    "explicit ScopedDevAuthorization proof."
                )
            dev_authorization.verify_authority(
                target_company_id=company.id,
                target_database=active_db,
            )

            # 1. Total Figma canonical jobs must equal exactly 138
            stmt_total = select(func.count(Job.id)).where(Job.company_id == FIGMA_COMPANY_ID)
            total_figma_jobs = (await session.execute(stmt_total)).scalar()
            if total_figma_jobs != 138:
                raise RuntimeError(
                    f"Pre-commit invariant violation: expected 138 Figma jobs, found {total_figma_jobs}."
                )

            # 2. Strict status and reason distribution: 123 EXCLUDED_ROLE, 15 LOCATION_NOT_ALLOWED, 0 REVIEW, 0 ELIGIBLE
            stmt_dist = (
                select(Job.eligibility_status, Job.rejection_reason, func.count(Job.id))
                .where(Job.company_id == FIGMA_COMPANY_ID)
                .group_by(Job.eligibility_status, Job.rejection_reason)
            )
            dist_res = (await session.execute(stmt_dist)).all()
            dist_map = {(row[0], row[1]): row[2] for row in dist_res}

            excluded_role_count = dist_map.get(("REJECTED", "EXCLUDED_ROLE"), 0)
            location_not_allowed_count = dist_map.get(("REJECTED", "LOCATION_NOT_ALLOWED"), 0)

            if excluded_role_count != 123:
                raise RuntimeError(
                    f"Pre-commit invariant violation: expected 123 REJECTED/EXCLUDED_ROLE jobs, found {excluded_role_count}."
                )
            if location_not_allowed_count != 15:
                raise RuntimeError(
                    f"Pre-commit invariant violation: expected 15 REJECTED/LOCATION_NOT_ALLOWED jobs, found {location_not_allowed_count}."
                )

            # Assert zero non-REJECTED jobs
            stmt_non_rejected = select(func.count(Job.id)).where(
                Job.company_id == FIGMA_COMPANY_ID,
                Job.eligibility_status != "REJECTED",
            )
            non_rejected_count = (await session.execute(stmt_non_rejected)).scalar()
            if non_rejected_count != 0:
                raise RuntimeError(
                    f"Pre-commit invariant violation: expected 0 non-REJECTED Figma jobs, found {non_rejected_count}."
                )

            # 3. Tel Aviv job specific inspection
            tel_aviv_job = await session.get(Job, FIGMA_TEL_AVIV_JOB_ID)
            if tel_aviv_job is None:
                raise RuntimeError(
                    f"Pre-commit invariant violation: Tel Aviv job {FIGMA_TEL_AVIV_JOB_ID} not found."
                )
            if tel_aviv_job.normalized_location != "Tel Aviv":
                raise RuntimeError(
                    f"Pre-commit invariant violation: Tel Aviv job normalized_location is "
                    f"'{tel_aviv_job.normalized_location}', expected 'Tel Aviv'."
                )
            if tel_aviv_job.eligibility_status != "REJECTED":
                raise RuntimeError(
                    f"Pre-commit invariant violation: Tel Aviv job eligibility_status is "
                    f"'{tel_aviv_job.eligibility_status}', expected 'REJECTED'."
                )
            if tel_aviv_job.rejection_reason != "LOCATION_NOT_ALLOWED":
                raise RuntimeError(
                    f"Pre-commit invariant violation: Tel Aviv job rejection_reason is "
                    f"'{tel_aviv_job.rejection_reason}', expected 'LOCATION_NOT_ALLOWED'."
                )
            if not tel_aviv_job.source_verified:
                raise RuntimeError(
                    "Pre-commit invariant violation: Tel Aviv job source_verified is False, expected True."
                )
            if Decimal(str(tel_aviv_job.verification_confidence)) != Decimal("0.50"):
                raise RuntimeError(
                    f"Pre-commit invariant violation: Tel Aviv job verification_confidence is "
                    f"{tel_aviv_job.verification_confidence}, expected 0.50."
                )

            # 4. Audit row count verification: every Figma job must have exactly 2 VerificationRecords and 2 FilterDecisions
            # (1 original baseline + 1 newly inserted refresh record)
            stmt_v_counts = (
                select(VerificationRecord.job_id, func.count(VerificationRecord.id))
                .join(Job, Job.id == VerificationRecord.job_id)
                .where(Job.company_id == FIGMA_COMPANY_ID)
                .group_by(VerificationRecord.job_id)
            )
            v_counts = {row[0]: row[1] for row in (await session.execute(stmt_v_counts)).all()}
            if len(v_counts) != 138 or any(c != 2 for c in v_counts.values()):
                bad_v = {jid: c for jid, c in v_counts.items() if c != 2}
                raise RuntimeError(
                    f"Pre-commit invariant violation: each of the 138 Figma jobs must have exactly 2 "
                    f"VerificationRecords. Found mismatches in {len(bad_v)} jobs (total jobs with records: {len(v_counts)})."
                )

            stmt_f_counts = (
                select(FilterDecision.job_id, func.count(FilterDecision.id))
                .join(Job, Job.id == FilterDecision.job_id)
                .where(Job.company_id == FIGMA_COMPANY_ID)
                .group_by(FilterDecision.job_id)
            )
            f_counts = {row[0]: row[1] for row in (await session.execute(stmt_f_counts)).all()}
            if len(f_counts) != 138 or any(c != 2 for c in f_counts.values()):
                bad_f = {jid: c for jid, c in f_counts.items() if c != 2}
                raise RuntimeError(
                    f"Pre-commit invariant violation: each of the 138 Figma jobs must have exactly 2 "
                    f"FilterDecisions. Found mismatches in {len(bad_f)} jobs (total jobs with decisions: {len(f_counts)})."
                )
