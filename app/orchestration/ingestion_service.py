"""Deterministic Job Ingestion Orchestration Service (V1).

Boundary: Coordinates the end-to-end ingestion lifecycle for discovered job postings:
1. Validates ingestion envelopes without mutating raw evidence (exact bytes & payload matching).
2. Persists immutable raw discoveries in PostgreSQL (RawJob) with exact SHA-256 content hashes.
3. Normalizes extracted job content deterministically (JobNormalizationService).
4. Runs multi-stage duplicate resolution (JobDeduplicationService).
5. Handles deduplication outcomes:
   - NEW: Creates canonical Job, records primary JobSourceReference, runs verification, runs eligibility filtering.
   - MATCH: Links JobSourceReference provenance, appends historical VerificationRecord and FilterDecision.
   - AMBIGUOUS: Halts at boundary without creating a duplicate canonical job, returning typed review status.
6. Returns strongly typed JobIngestionOutcome.

Transaction ownership remains strictly with the caller (no commit inside the service).
Rollback completely reverts all writes performed during the workflow.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationCandidate,
    DeduplicationResult,
)
from app.deduplication.service import JobDeduplicationService
from app.normalization.service import JobNormalizationService
from app.orchestration.decision_service import (
    JobFilterDecisionOutcome,
    JobFilterDecisionService,
)
from app.orchestration.verification_service import (
    JobVerificationOutcome,
    JobVerificationService,
)
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.repositories.company_source_repository import (
    CompanySourceRepository,
)
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.rules.engine import JobFilterRuleEngine
from app.verification.evaluator import JobVerificationEvaluator
from app.verification.schemas import JobVerificationInput

STATUS_PROCESSED_NEW = "PROCESSED_NEW"
STATUS_PROCESSED_MATCH = "PROCESSED_MATCH"
STATUS_AMBIGUOUS_DUPLICATE = "AMBIGUOUS_DUPLICATE"


class JobIngestionEnvelope(BaseModel):
    """Strongly typed input envelope for job ingestion orchestration.

    Boundary: Encapsulates all raw source response data, origin metadata,
    and optional explicit verification evidence. Validates cryptographic
    hash and wire-to-payload match without mutating raw evidence.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
    )

    raw_body: bytes = Field(..., description="Unaltered raw response bytes from wire/disk")
    raw_payload: dict[str, Any] = Field(
        ...,
        description="Parsed semantic dictionary (JSON API object or parser-extracted HTML structure)",
    )
    source_id: uuid.UUID = Field(..., description="Platform identifier referencing sources.id")
    company_source_id: uuid.UUID = Field(
        ...,
        description="Endpoint mapping UUID referencing company_sources.id",
    )
    company_id: uuid.UUID = Field(
        ...,
        description="Canonical Company UUID referencing companies.id",
    )
    company_name: str = Field(..., description="Hiring company display name")
    source_job_id: str = Field(
        ...,
        description="Platform-native identifier or deterministic 'gen_<sha256>' key",
    )
    source_job_id_type: Literal["NATIVE", "GENERATED"] = Field(
        default="NATIVE",
        description="Identifier classification: NATIVE platform key or GENERATED synthetic ID",
    )
    source_url: str = Field(..., description="Discovery or listing page URL")
    application_url: str | None = Field(
        default=None,
        description="Direct application or ATS URL endpoint",
    )
    payload_format: Literal["JSON", "HTML"] = Field(
        default="JSON",
        description="Payload format: JSON API or HTML page extraction",
    )
    collected_at: datetime | None = Field(
        default=None,
        description="Timestamp of discovery/collection (defaults to now UTC)",
    )
    collection_run_id: uuid.UUID | None = Field(
        default=None,
        description="Optional foreign key referencing collection_runs.id",
    )
    http_status: int | None = Field(
        default=200,
        description="HTTP status code from source page fetch",
    )
    content_hash: str | None = Field(
        default=None,
        description="Optional SHA-256 digest of raw_body; verified if provided, calculated if omitted",
    )
    extracted_data: dict[str, Any] | None = Field(
        default=None,
        description="Optional pre-extracted job attributes dictionary; defaults to raw_payload",
    )
    verification_evidence: JobVerificationInput | None = Field(
        default=None,
        description="Optional explicit verification observations from ingestion/probes",
    )


# Alias for callers that prefer JobIngestionInput naming convention
JobIngestionInput = JobIngestionEnvelope


class JobIngestionOutcome(NamedTuple):
    """Structured tuple encapsulating the end-to-end job ingestion result."""

    status: str
    raw_job: RawJob
    deduplication_result: DeduplicationResult
    canonical_job: Job | None = None
    source_reference: JobSourceReference | None = None
    verification_outcome: JobVerificationOutcome | None = None
    filter_decision_outcome: JobFilterDecisionOutcome | None = None
    review_reasons: list[str] | None = None

    @property
    def is_new(self) -> bool:
        """Whether a new canonical Job was created."""
        return self.status == STATUS_PROCESSED_NEW

    @property
    def is_match(self) -> bool:
        """Whether an existing canonical Job was matched and linked."""
        return self.status == STATUS_PROCESSED_MATCH

    @property
    def is_ambiguous(self) -> bool:
        """Whether deduplication was ambiguous, halting canonical creation."""
        return self.status == STATUS_AMBIGUOUS_DUPLICATE

    @property
    def requires_review(self) -> bool:
        """Whether any verification, deduplication, or filtering review reason was raised."""
        return bool(self.review_reasons)


class JobIngestionService:
    """Application orchestration service coordinating end-to-end job ingestion."""

    def __init__(
        self,
        raw_job_repository: RawJobRepository,
        job_repository: JobRepository,
        company_source_repository: CompanySourceRepository | None = None,
        normalization_service: JobNormalizationService | None = None,
        deduplication_service: JobDeduplicationService | None = None,
        verification_service: JobVerificationService | None = None,
        decision_service: JobFilterDecisionService | None = None,
    ) -> None:
        """Initialize orchestration service with repositories and sub-services.

        Args:
            raw_job_repository: Repository managing immutable RawJob discoveries.
            job_repository: Repository managing canonical Job entities.
            company_source_repository: Optional repository managing CompanySource mappings.
            normalization_service: Pure deterministic normalization service.
            deduplication_service: Multi-stage duplicate resolution service.
            verification_service: Service managing verification evaluation and persistence.
            decision_service: Service managing filter rules and decision persistence.
        """
        self.raw_job_repo = raw_job_repository
        self.job_repo = job_repository
        self.norm_service = normalization_service or JobNormalizationService()

        session = getattr(raw_job_repository, "session", None) or getattr(
            job_repository, "session", None
        )
        self.cs_repo = company_source_repository or (
            CompanySourceRepository(session) if session is not None else None
        )
        self.dedup_service = deduplication_service or (
            JobDeduplicationService.from_session(session) if session is not None else None
        )
        self.verification_service = verification_service or (
            JobVerificationService.from_session(session) if session is not None else None
        )
        self.decision_service = decision_service or (
            JobFilterDecisionService.from_session(session) if session is not None else None
        )

    @classmethod
    def from_session(
        cls,
        session: AsyncSession,
        *,
        company_source_repository: CompanySourceRepository | None = None,
        normalization_service: JobNormalizationService | None = None,
        evaluator: JobVerificationEvaluator | None = None,
        rule_engine: JobFilterRuleEngine | None = None,
    ) -> "JobIngestionService":
        """Instantiate ingestion service and all underlying components from an active AsyncSession."""
        return cls(
            raw_job_repository=RawJobRepository(session),
            job_repository=JobRepository(session),
            company_source_repository=company_source_repository or CompanySourceRepository(session),
            normalization_service=normalization_service or JobNormalizationService(),
            deduplication_service=JobDeduplicationService.from_session(session),
            verification_service=JobVerificationService.from_session(session, evaluator=evaluator),
            decision_service=JobFilterDecisionService.from_session(
                session, rule_engine=rule_engine
            ),
        )

    async def ingest(
        self,
        envelope: JobIngestionEnvelope | dict[str, Any],
    ) -> JobIngestionOutcome:
        """Coordinate end-to-end ingestion of a single job discovery.

        Execution stages:
        1. Validate ingestion envelope (cryptographic content hash, JSON wire-to-payload match, no mutation).
        2. Persist immutable raw discovery (RawJob) with exact original bytes via RawJobRepository.
        3. Normalize extracted job data using JobNormalizationService.
        4. Run duplicate resolution using JobDeduplicationService.
        5. Handle deduplication outcome:
           - AMBIGUOUS: Stop at decision boundary; do not create duplicate canonical Job; return review outcome.
           - NEW: Create canonical Job, record primary JobSourceReference, evaluate verification, evaluate eligibility.
           - MATCH: Link JobSourceReference to matched Job, evaluate verification, evaluate eligibility.
        6. Return typed JobIngestionOutcome.

        Transactional policy:
        - Flushes changes to session to surface constraints and update dirty state.
        - Does NOT commit the session; transaction lifecycle remains with the caller.
        - Rollback completely reverts all writes across all tables.

        Args:
            envelope: Strongly typed JobIngestionEnvelope or compatible dictionary.

        Returns:
            JobIngestionOutcome: Encapsulating status, RawJob, Job, references, decisions, and review reasons.

        Raises:
            TypeError: If input types are unsupported.
            ValueError: If envelope validation fails or canonical job cannot be resolved.
        """
        # ---------------------------------------------------------------------
        # STAGE 1: Validate Ingestion Envelope
        # ---------------------------------------------------------------------
        env = self._validate_envelope(envelope)

        # ---------------------------------------------------------------------
        # STAGE 1B: Validate Identifier Consistency against Registry
        # ---------------------------------------------------------------------
        await self._validate_identifier_consistency(env)

        # ---------------------------------------------------------------------
        # STAGE 2: Persist Raw Discovery (RawJob)
        # ---------------------------------------------------------------------
        collected_at = env.collected_at or datetime.now(UTC)
        raw_job = RawJob(
            id=uuid.uuid4(),
            company_source_id=env.company_source_id,
            collection_run_id=env.collection_run_id,
            source_job_id=env.source_job_id,
            source_job_id_type=env.source_job_id_type,
            source_url=env.source_url,
            payload_format=env.payload_format,
            raw_body=env.raw_body,
            raw_payload=env.raw_payload,
            content_hash=hashlib.sha256(env.raw_body).hexdigest(),
            http_status=env.http_status,
            collected_at=collected_at,
        )
        persisted_raw_job = await self.raw_job_repo.create(raw_job)

        # ---------------------------------------------------------------------
        # STAGE 3: Normalize Extracted Job Data
        # ---------------------------------------------------------------------
        extracted = env.extracted_data if env.extracted_data is not None else env.raw_payload
        normalized = self.norm_service.normalize(
            extracted=extracted,
            company_id=env.company_id,
            company_name=env.company_name,
            payload_format=env.payload_format,
        )

        # ---------------------------------------------------------------------
        # STAGE 4: Run Deduplication Resolution
        # ---------------------------------------------------------------------
        if self.dedup_service is None:
            raise RuntimeError(
                "JobDeduplicationService is not configured on this service instance."
            )

        candidate = DeduplicationCandidate.from_normalized(
            normalized=normalized,
            source_id=env.source_id,
            source_job_id=env.source_job_id,
            source_url=env.source_url,
            company_id=env.company_id,
            raw_job_id=persisted_raw_job.id,
            source_job_id_type=env.source_job_id_type,
            application_url=env.application_url or normalized.application_url,
        )
        dedup_result = await self.dedup_service.deduplicate(candidate)

        # ---------------------------------------------------------------------
        # STAGE 5: Apply Approved Deduplication Outcome
        # ---------------------------------------------------------------------
        if dedup_result.action == DeduplicationAction.AMBIGUOUS:
            # Boundary mandate: Never silently create an additional duplicate canonical job.
            # Stop at decision boundary, preserve raw discovery, and return typed outcome with review requirement.
            review_reasons = [dedup_result.review_reason or "DUPLICATE_AMBIGUOUS"]
            return JobIngestionOutcome(
                status=STATUS_AMBIGUOUS_DUPLICATE,
                raw_job=persisted_raw_job,
                deduplication_result=dedup_result,
                canonical_job=None,
                source_reference=None,
                verification_outcome=None,
                filter_decision_outcome=None,
                review_reasons=review_reasons,
            )

        if self.verification_service is None:
            raise RuntimeError("JobVerificationService is not configured on this service instance.")
        if self.decision_service is None:
            raise RuntimeError(
                "JobFilterDecisionService is not configured on this service instance."
            )

        if dedup_result.action == DeduplicationAction.NEW:
            # 5A: New canonical Job
            job_kwargs = normalized.to_job_kwargs(
                company_id=env.company_id,
                company_name=env.company_name,
            )
            canonical_job = Job(
                id=uuid.uuid4(),
                **job_kwargs,
                role_priority="P3",
                relevance_status="TARGET",
                eligibility_status="NEW",
                rejection_reason=None,
                review_reason=None,
                source_verified=False,
                jd_verified=False,
                company_verified=False,
                location_verified=False,
                experience_verified=False,
                salary_verified=False,
                verification_confidence=Decimal("0.00"),
                collected_at=collected_at,
            )
            persisted_job = await self.job_repo.create(canonical_job)

            # Record primary source reference provenance
            source_ref = await self.dedup_service.record_provenance_link(
                job_id=persisted_job.id,
                raw_job_id=persisted_raw_job.id,
                source_id=env.source_id,
                source_job_id=env.source_job_id,
                source_url=env.source_url,
                source_job_id_type=env.source_job_id_type,
                application_url=env.application_url or normalized.application_url,
                extracted_data=extracted,
                posted_at=normalized.posted_at,
                is_primary=True,
            )

            # Evaluate and persist verification record
            verif_outcome = await self.verification_service.evaluate_and_persist(
                job_or_id=persisted_job,
                evidence=env.verification_evidence,
                reference_time=collected_at,
            )

            # Evaluate and persist filter decision
            decision_outcome = await self.decision_service.evaluate_and_persist(
                job_or_id=verif_outcome.job or persisted_job,
                reference_time=collected_at,
            )

            review_reasons = self._collect_review_reasons(
                decision_job=decision_outcome.job,
                verif_result=verif_outcome.result,
                dedup_result=dedup_result,
            )

            return JobIngestionOutcome(
                status=STATUS_PROCESSED_NEW,
                raw_job=persisted_raw_job,
                canonical_job=decision_outcome.job,
                deduplication_result=dedup_result,
                source_reference=source_ref,
                verification_outcome=verif_outcome,
                filter_decision_outcome=decision_outcome,
                review_reasons=review_reasons,
            )

        elif dedup_result.action == DeduplicationAction.MATCH:
            # 5B: Duplicate match to existing canonical Job
            matched_job = dedup_result.matched_job
            if matched_job is None and dedup_result.canonical_job_id is not None:
                matched_job = await self.job_repo.get_by_id(dedup_result.canonical_job_id)
            if matched_job is None:
                raise ValueError(
                    f"Deduplication matched canonical job {dedup_result.canonical_job_id}, "
                    "but the Job entity could not be loaded from repository."
                )

            # Link secondary provenance reference to existing canonical job
            source_ref = await self.dedup_service.record_provenance_link(
                job_id=matched_job.id,
                raw_job_id=persisted_raw_job.id,
                source_id=env.source_id,
                source_job_id=env.source_job_id,
                source_url=env.source_url,
                source_job_id_type=env.source_job_id_type,
                application_url=env.application_url or normalized.application_url,
                extracted_data=extracted,
                posted_at=normalized.posted_at,
                is_primary=False,
            )

            # Evaluate and persist historical verification audit record
            verif_outcome = await self.verification_service.evaluate_and_persist(
                job_or_id=matched_job,
                evidence=env.verification_evidence,
                reference_time=collected_at,
            )

            # Evaluate and persist historical filter decision
            decision_outcome = await self.decision_service.evaluate_and_persist(
                job_or_id=verif_outcome.job or matched_job,
                reference_time=collected_at,
            )

            review_reasons = self._collect_review_reasons(
                decision_job=decision_outcome.job,
                verif_result=verif_outcome.result,
                dedup_result=dedup_result,
            )

            return JobIngestionOutcome(
                status=STATUS_PROCESSED_MATCH,
                raw_job=persisted_raw_job,
                canonical_job=decision_outcome.job,
                deduplication_result=dedup_result,
                source_reference=source_ref,
                verification_outcome=verif_outcome,
                filter_decision_outcome=decision_outcome,
                review_reasons=review_reasons,
            )

        else:
            raise ValueError(f"Unsupported deduplication action: {dedup_result.action!r}")

    async def _validate_identifier_consistency(self, env: JobIngestionEnvelope) -> None:
        """Validate envelope identifier consistency against database relations.

        Guarantees:
        - source_id matches the source associated with company_source_id.
        - company_id matches the company associated with company_source_id.
        - Mismatches are rejected before any downstream writes are performed.
        - Invalid envelope data leaves zero partial writes in caller's transaction.
        """
        if self.cs_repo is None:
            return

        company_source = await self.cs_repo.get_by_id(env.company_source_id)
        if company_source is None:
            raise ValueError(
                f"CompanySource mapping with id '{env.company_source_id}' does not exist in registry."
            )

        if company_source.source_id != env.source_id:
            raise ValueError(
                f"Identifier mismatch: envelope source_id '{env.source_id}' does not match "
                f"CompanySource source_id '{company_source.source_id}' for company_source_id '{env.company_source_id}'."
            )

        if company_source.company_id != env.company_id:
            raise ValueError(
                f"Identifier mismatch: envelope company_id '{env.company_id}' does not match "
                f"CompanySource company_id '{company_source.company_id}' for company_source_id '{env.company_source_id}'."
            )

    def _validate_envelope(
        self,
        envelope: JobIngestionEnvelope | dict[str, Any],
    ) -> JobIngestionEnvelope:
        """Validate input envelope types, cryptographic hash, and wire consistency."""
        if isinstance(envelope, dict):
            env = JobIngestionEnvelope(**envelope)
        elif isinstance(envelope, JobIngestionEnvelope):
            env = envelope
        else:
            raise TypeError(
                f"Unsupported envelope type: {type(envelope).__name__}. "
                "Expected JobIngestionEnvelope or dict."
            )

        if not isinstance(env.raw_body, (bytes, bytearray, memoryview)):
            raise TypeError("envelope.raw_body must be bytes")
        if not isinstance(env.raw_payload, dict):
            raise TypeError("envelope.raw_payload must be a dict")

        raw_bytes = bytes(env.raw_body)
        calculated_hash = hashlib.sha256(raw_bytes).hexdigest()
        if env.content_hash is not None and env.content_hash != calculated_hash:
            raise ValueError(
                f"content_hash mismatch: provided '{env.content_hash}' does not match "
                f"SHA-256 digest of raw_body '{calculated_hash}'"
            )

        if env.payload_format == "JSON":
            try:
                decoded_text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"raw_body contains invalid UTF-8 bytes: {exc}") from exc

            try:
                parsed_json = json.loads(decoded_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"raw_body does not contain valid JSON: {exc}") from exc

            if not isinstance(parsed_json, dict):
                raise ValueError(
                    f"raw_body must decode to a JSON object (dict), got {type(parsed_json).__name__}"
                )

            if parsed_json != env.raw_payload:
                raise ValueError("Wire JSON in raw_body does not match raw_payload")

        elif env.payload_format == "HTML":
            pass

        if not env.source_job_id or not env.source_job_id.strip():
            raise ValueError("source_job_id cannot be empty")
        if not env.source_url or not env.source_url.strip():
            raise ValueError("source_url cannot be empty")
        if not env.company_name or not env.company_name.strip():
            raise ValueError("company_name cannot be empty")

        return env

    @staticmethod
    def _collect_review_reasons(
        decision_job: Job | None,
        verif_result: Any | None,
        dedup_result: DeduplicationResult | None,
    ) -> list[str]:
        """Aggregate unique review reasons in deterministic order."""
        reasons: list[str] = []

        if (
            decision_job
            and decision_job.review_reason
            and decision_job.review_reason not in reasons
        ):
            reasons.append(decision_job.review_reason)

        if verif_result and getattr(verif_result, "review_reasons", None):
            for r in verif_result.review_reasons:
                if r not in reasons:
                    reasons.append(r)

        if (
            dedup_result
            and dedup_result.review_reason
            and dedup_result.review_reason not in reasons
        ):
            reasons.append(dedup_result.review_reason)

        return reasons
