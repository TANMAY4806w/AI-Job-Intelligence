"""Job verification orchestration service.

Boundary: Orchestrates the evaluation of canonical Job openings against the pure
deterministic verification engine (JobVerificationEvaluator) and persists immutable
historical VerificationRecord audit rows in PostgreSQL.

Transaction ownership remains strictly with the caller (no commit inside the service).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.decision import VerificationRecord
from app.persistence.models.job import Job
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.verification.evaluator import JobVerificationEvaluator
from app.verification.schemas import JobVerificationInput, JobVerificationResult


class JobVerificationOutcome(NamedTuple):
    """Structured tuple encapsulating the persisted VerificationRecord, pure result, and updated Job."""

    record: VerificationRecord
    result: JobVerificationResult
    job: Job | None = None


class JobVerificationService:
    """Application orchestration service integrating pure verification with database persistence."""

    def __init__(
        self,
        verification_record_repository: VerificationRecordRepository,
        job_repository: JobRepository | None = None,
        evaluator: JobVerificationEvaluator | None = None,
    ) -> None:
        """Initialize service with persistence repositories and verification evaluator.

        Args:
            verification_record_repository: Repository managing VerificationRecord entities.
            job_repository: Optional repository managing canonical Job entities.
            evaluator: Optional JobVerificationEvaluator instance (defaults to standard V1 evaluator).
        """
        self.verification_repo = verification_record_repository
        session = getattr(verification_record_repository, "session", None)
        self.job_repo = job_repository or (JobRepository(session) if session is not None else None)
        self.evaluator = evaluator or JobVerificationEvaluator()

    @classmethod
    def from_session(
        cls,
        session: AsyncSession,
        evaluator: JobVerificationEvaluator | None = None,
    ) -> "JobVerificationService":
        """Instantiate service and repositories bound to an active AsyncSession."""
        return cls(
            verification_record_repository=VerificationRecordRepository(session),
            job_repository=JobRepository(session),
            evaluator=evaluator,
        )

    async def evaluate_and_persist(
        self,
        job_or_id: Job | uuid.UUID,
        evidence: JobVerificationInput | None = None,
        *,
        reference_time: datetime | None = None,
        verification_run_at: datetime | None = None,
        update_job_summary: bool = True,
    ) -> JobVerificationOutcome:
        """Evaluate verification evidence, append a historical VerificationRecord, and update Job summary.

        Steps:
        1. Resolve canonical Job and target job_id.
        2. Resolve or assemble structured JobVerificationInput.
        3. Execute pure JobVerificationEvaluator.
        4. Persist a new historical VerificationRecord row (append-only audit trail).
        5. Optionally update Job verification summary fields (confidence and dimension flags).
        6. Return JobVerificationOutcome to caller.

        Transactional policy:
        - Flushes changes to session to surface constraints and update dirty state.
        - Does NOT commit the session; transaction lifecycle remains with the caller.
        - Preserves prior VerificationRecord rows without overwriting history.

        Args:
            job_or_id: Canonical Job entity instance or its UUID primary key.
            evidence: Explicit verification observations; if omitted, extracted from Job.
            reference_time: Reference timestamp for evaluation (defaults to now UTC).
            verification_run_at: Timestamp for the VerificationRecord execution (defaults to now UTC).
            update_job_summary: Whether to update verification summary columns on Job (default: True).

        Returns:
            JobVerificationOutcome: NamedTuple containing (record, result, job).
        """
        job: Job | None = None
        job_id: uuid.UUID

        if isinstance(job_or_id, Job):
            job = job_or_id
            job_id = job.id
        else:
            job_id = job_or_id
            if self.job_repo is not None:
                job = await self.job_repo.get_by_id(job_id)

        # Assemble or enrich verification input
        if evidence is not None:
            if evidence.job_id is None:
                # Populate job_id from resolved target
                input_data = evidence.model_copy(update={"job_id": job_id})
            else:
                input_data = evidence
        elif job is not None:
            input_data = JobVerificationInput.from_job(job)
        else:
            raise ValueError(
                f"Cannot evaluate verification: job with id {job_id} not found and no explicit evidence provided."
            )

        # Execute pure verification evaluator
        result = self.evaluator.evaluate(input_data, reference_time=reference_time)

        # Persist new historical VerificationRecord
        record_kwargs = result.to_verification_record_kwargs(
            job_id=job_id,
            verification_run_at=verification_run_at,
        )
        record = VerificationRecord(**record_kwargs)
        persisted_record = await self.verification_repo.create(record)

        # Update canonical Job verification summary columns if applicable
        updated_job = job
        if update_job_summary and job is not None and self.job_repo is not None:
            verification_updates = result.to_job_verification_updates()
            updated_job = await self.job_repo.update_verification_status(
                job,
                verification_updates,
            )

        return JobVerificationOutcome(
            record=persisted_record,
            result=result,
            job=updated_job,
        )

    async def verify_job(
        self,
        job_or_id: Job | uuid.UUID,
        evidence: JobVerificationInput | None = None,
        *,
        reference_time: datetime | None = None,
        verification_run_at: datetime | None = None,
        update_job_summary: bool = True,
    ) -> JobVerificationOutcome:
        """Alias for evaluate_and_persist."""
        return await self.evaluate_and_persist(
            job_or_id,
            evidence=evidence,
            reference_time=reference_time,
            verification_run_at=verification_run_at,
            update_job_summary=update_job_summary,
        )

    async def list_history(
        self,
        job_id: uuid.UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[VerificationRecord]:
        """List historical verification records for a job, ordered by run timestamp descending."""
        return await self.verification_repo.list_by_job_id(
            job_id,
            limit=limit,
            offset=offset,
        )

    async def get_latest(
        self,
        job_id: uuid.UUID,
    ) -> VerificationRecord | None:
        """Retrieve the most recent VerificationRecord for a job."""
        return await self.verification_repo.get_latest_by_job_id(job_id)
