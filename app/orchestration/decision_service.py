"""Job filtering decision persistence and application orchestration service.

Boundary: Orchestrates the evaluation of canonical Job vacancies against the pure
deterministic rules engine (JobFilterRuleEngine) and persists immutable historical
FilterDecision audit records while updating Job operational status in PostgreSQL.

Transaction ownership remains strictly with the caller (no commit inside the service).
"""

import uuid
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.decision import FilterDecision
from app.persistence.models.job import Job
from app.persistence.repositories.filter_decision_repository import FilterDecisionRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.rules.engine import JobFilterRuleEngine
from app.rules.schemas import JobFilterInput, JobFilterResult

# Sentinel to distinguish caller-supplied None from unsupplied parameter
_UNSET = object()


class JobFilterDecisionOutcome(NamedTuple):
    """Structured tuple encapsulating the updated Job, persisted FilterDecision, and pure rule result."""

    job: Job
    decision: FilterDecision
    result: JobFilterResult


class JobFilterDecisionService:
    """Application orchestration service integrating pure filter rules with database persistence."""

    def __init__(
        self,
        job_repository: JobRepository,
        filter_decision_repository: FilterDecisionRepository,
        verification_record_repository: VerificationRecordRepository | None = None,
        rule_engine: JobFilterRuleEngine | None = None,
    ) -> None:
        """Initialize service with persistence repositories and rule engine.

        Args:
            job_repository: Repository managing canonical Job entities.
            filter_decision_repository: Repository managing FilterDecision audit records.
            verification_record_repository: Optional repository managing VerificationRecords.
            rule_engine: Optional JobFilterRuleEngine instance (defaults to standard V1 engine).
        """
        self.job_repo = job_repository
        self.decision_repo = filter_decision_repository
        session = getattr(job_repository, "session", None)
        self.verification_repo = verification_record_repository or (
            VerificationRecordRepository(session) if session is not None else None
        )
        self.rule_engine = rule_engine or JobFilterRuleEngine()

    @classmethod
    def from_session(
        cls,
        session: AsyncSession,
        rule_engine: JobFilterRuleEngine | None = None,
    ) -> "JobFilterDecisionService":
        """Instantiate service and repositories bound to an active AsyncSession."""
        return cls(
            job_repository=JobRepository(session),
            filter_decision_repository=FilterDecisionRepository(session),
            verification_record_repository=VerificationRecordRepository(session),
            rule_engine=rule_engine,
        )

    async def resolve_posting_date_verified(
        self,
        job_or_id: Job | uuid.UUID,
        explicit_value: bool | None | object = _UNSET,
    ) -> bool | None:
        """Resolve publication-date verification state.

        Precedence rules:
        1. Explicit caller-supplied value (True, False, or explicit None) takes priority.
        2. If unsupplied (_UNSET), queries the latest authoritative VerificationRecord
           linked to this job via VerificationRecordRepository.
        3. If no VerificationRecord exists, returns None (unresolved).

        Specification guarantees:
        - Never derives verification state from collected_at.
        - Never assumes a publication date is verified merely because posted_at is present.
        """
        if explicit_value is not _UNSET:
            return explicit_value  # type: ignore[return-value]

        job_id = job_or_id.id if isinstance(job_or_id, Job) else job_or_id

        if self.verification_repo is not None:
            latest_record = await self.verification_repo.get_latest_by_job_id(job_id)
            if latest_record is not None:
                return latest_record.posting_date_verified

        # Fallback to in-memory relationship on Job if already loaded
        if isinstance(job_or_id, Job) and hasattr(job_or_id, "verification_records"):
            records = getattr(job_or_id, "verification_records", None)
            if records:
                sorted_records = sorted(
                    records,
                    key=lambda r: getattr(
                        r, "verification_run_at", datetime.min.replace(tzinfo=UTC)
                    ),
                    reverse=True,
                )
                return sorted_records[0].posting_date_verified

        return None

    async def evaluate_and_persist(
        self,
        job_or_id: Job | uuid.UUID,
        *,
        posting_date_verified: bool | None | object = _UNSET,
        reference_time: datetime | None = None,
    ) -> JobFilterDecisionOutcome:
        """Evaluate a canonical Job with the deterministic rules engine and persist decision.

        Steps:
        1. Load existing canonical Job and required attributes.
        2. Obtain publication-date verification state from caller input or VerificationRecord.
        3. Construct typed JobFilterInput using the pure schema factory.
        4. Execute pure JobFilterRuleEngine.
        5. Persist a new historical FilterDecision row (immutable audit trail).
        6. Update canonical Job status and review/rejection fields.
        7. Return JobFilterDecisionOutcome to caller.

        Transactional policy:
        - Flushes changes to session to surface constraints and update dirty state.
        - Does NOT commit the session; transaction lifecycle remains with the caller.
        - Job update and FilterDecision participate in the same caller-controlled transaction.

        Args:
            job_or_id: Canonical Job entity instance or its UUID primary key.
            posting_date_verified: Explicit verification boolean or None; if omitted (_UNSET),
                resolves from the latest VerificationRecord.
            reference_time: Reference timestamp for freshness evaluation (defaults to now UTC).

        Returns:
            JobFilterDecisionOutcome: NamedTuple containing (job, decision, result).
        """
        # 1. Load canonical Job
        if isinstance(job_or_id, Job):
            job = job_or_id
        else:
            loaded_job = await self.job_repo.get_by_id(job_or_id)
            if loaded_job is None:
                raise ValueError(f"Job with id {job_or_id} not found.")
            job = loaded_job

        # 2. Resolve publication-date verification state
        resolved_verified = await self.resolve_posting_date_verified(
            job, explicit_value=posting_date_verified
        )

        # 3. Construct typed JobFilterInput
        job_input = JobFilterInput.from_job(job, posting_date_verified=resolved_verified)

        # 4. Invoke pure rule engine
        result = self.rule_engine.evaluate(job_input, reference_time=reference_time)

        # 5. Persist a new historical FilterDecision row (immutable audit trail)
        decision_kwargs = result.to_filter_decision_kwargs(job.id)
        decision = FilterDecision(**decision_kwargs)
        persisted_decision = await self.decision_repo.create(decision)

        # 6. Update canonical Job operational status and reason codes
        status_updates = result.to_job_status_updates()
        updated_job = await self.job_repo.update_status(job, status_updates)

        # 7. Return structured outcome
        return JobFilterDecisionOutcome(
            job=updated_job,
            decision=persisted_decision,
            result=result,
        )

    async def evaluate_job(
        self,
        job_or_id: Job | uuid.UUID,
        *,
        posting_date_verified: bool | None | object = _UNSET,
        reference_time: datetime | None = None,
    ) -> JobFilterDecisionOutcome:
        """Evaluate a canonical Job and persist the filter decision (alias for evaluate_and_persist)."""
        return await self.evaluate_and_persist(
            job_or_id,
            posting_date_verified=posting_date_verified,
            reference_time=reference_time,
        )
