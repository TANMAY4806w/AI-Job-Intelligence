"""Deterministic three-stage job deduplication service.

Boundary: Evaluates incoming postings across the 3-stage evidence hierarchy:
1. Stage 1 (Exact External Source Identity Match, Confidence: 1.0)
2. Stage 2 (Exact Normalized Application / ATS URL Match, Confidence: 0.98)
3. Stage 3 (Composite Deterministic Match: Company + Title + Location + Date proximity <= 14 days, Confidence: 0.90)

Flags conflicting or ambiguous records with review_reason = "DUPLICATE_AMBIGUOUS".
Preserves provenance by linking JobSourceReferences without discarding source data.
Transaction ownership remains strictly with the caller (no commit inside the service).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.deduplication.matching import evaluate_stage_3_candidate
from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationCandidate,
    DeduplicationResult,
    DeduplicationStage,
)
from app.normalization.urls import normalize_url
from app.persistence.models.raw_job import JobSourceReference
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository


class JobDeduplicationService:
    """Service executing deterministic three-stage duplicate resolution for job postings."""

    def __init__(
        self,
        job_repository: JobRepository,
        job_source_reference_repository: JobSourceReferenceRepository | None = None,
        raw_job_repository: RawJobRepository | None = None,
    ) -> None:
        """Initialize service with persistence repositories.

        Args:
            job_repository: Repository managing canonical Job entities.
            job_source_reference_repository: Optional JobSourceReference repository.
            raw_job_repository: Optional RawJob repository.
        """
        self.job_repo = job_repository
        session = getattr(job_repository, "session", None)
        self.jsr_repo = (
            job_source_reference_repository
            or (JobSourceReferenceRepository(session) if session is not None else None)  # type: ignore[arg-type]
        )
        self.raw_job_repo = (
            raw_job_repository or (RawJobRepository(session) if session is not None else None)  # type: ignore[arg-type]
        )

    @classmethod
    def from_session(cls, session: AsyncSession) -> "JobDeduplicationService":
        """Instantiate service from an active AsyncSession."""
        return cls(
            job_repository=JobRepository(session),
            job_source_reference_repository=JobSourceReferenceRepository(session),
            raw_job_repository=RawJobRepository(session),
        )

    async def deduplicate(self, candidate: DeduplicationCandidate) -> DeduplicationResult:
        """Evaluate an incoming candidate against canonical jobs using the 3-stage hierarchy.

        Evaluation stops at the first decisive match or ambiguity.

        Args:
            candidate: Strongly typed candidate posting undergoing duplicate resolution.

        Returns:
            DeduplicationResult: Typed decision, confidence score, and audit evidence trail.
        """
        evidence: dict[str, Any] = {}

        # ---------------------------------------------------------------------
        # STAGE 1: Exact External Source Identity Match (Confidence: 1.0)
        # ---------------------------------------------------------------------
        # Evaluates identity against existing JobSourceReference by (source_id, source_job_id).
        # Note on Identifier Types: The "gen_" prefix on synthetic hashes is a pipeline
        # convention to distinguish generated IDs from native platform keys, not a formal
        # schema constraint. Stage 1 matches on (source_id, source_job_id) per specification.
        evidence["stage_1"] = {
            "evaluated": True,
            "source_id": str(candidate.source_id),
            "source_job_id": candidate.source_job_id,
            "source_job_id_type": candidate.source_job_id_type,
        }

        stage_1_match = await self.job_repo.get_by_source_reference(
            source_id=candidate.source_id,
            source_job_id=candidate.source_job_id,
        )

        if stage_1_match is not None:
            evidence["stage_1"]["match"] = True
            evidence["stage_1"]["canonical_job_id"] = str(stage_1_match.id)
            return DeduplicationResult(
                action=DeduplicationAction.MATCH,
                stage=DeduplicationStage.STAGE_1_EXACT_SOURCE_ID,
                confidence=1.0,
                canonical_job_id=stage_1_match.id,
                matched_job=stage_1_match,
                candidate_job_ids=[stage_1_match.id],
                evidence=evidence,
            )

        evidence["stage_1"]["match"] = False

        # ---------------------------------------------------------------------
        # STAGE 2: Exact Normalized Application / ATS URL Match (Confidence: 0.98)
        # ---------------------------------------------------------------------
        raw_app_url = candidate.application_url
        normalized_url: str | None = None

        if raw_app_url and raw_app_url.strip():
            try:
                normalized_url = normalize_url(raw_app_url)
            except ValueError as err:
                evidence["stage_2"] = {
                    "evaluated": True,
                    "raw_url": raw_app_url,
                    "error": f"URL normalization failed: {err}",
                    "match": False,
                }

        if normalized_url is not None:
            evidence["stage_2"] = {
                "evaluated": True,
                "raw_url": raw_app_url,
                "normalized_url": normalized_url,
            }

            url_candidates = await self.job_repo.find_all_by_application_url(normalized_url)
            # Deduplicate by primary key UUID
            unique_url_jobs = list({j.id: j for j in url_candidates}.values())
            evidence["stage_2"]["candidates_found"] = len(unique_url_jobs)

            if len(unique_url_jobs) == 1:
                matched_job = unique_url_jobs[0]
                evidence["stage_2"]["match"] = True
                evidence["stage_2"]["canonical_job_id"] = str(matched_job.id)
                return DeduplicationResult(
                    action=DeduplicationAction.MATCH,
                    stage=DeduplicationStage.STAGE_2_EXACT_URL,
                    confidence=0.98,
                    canonical_job_id=matched_job.id,
                    matched_job=matched_job,
                    candidate_job_ids=[matched_job.id],
                    evidence=evidence,
                )
            elif len(unique_url_jobs) > 1:
                # Multiple conflicting canonical jobs share the same ATS application URL
                evidence["stage_2"]["match"] = False
                evidence["stage_2"]["conflict"] = True
                conflicting_ids = [j.id for j in unique_url_jobs]
                evidence["stage_2"]["candidate_job_ids"] = [str(jid) for jid in conflicting_ids]
                return DeduplicationResult(
                    action=DeduplicationAction.AMBIGUOUS,
                    stage=DeduplicationStage.STAGE_2_EXACT_URL,
                    confidence=0.98,
                    review_reason="DUPLICATE_AMBIGUOUS",
                    candidate_job_ids=conflicting_ids,
                    evidence=evidence,
                )
            else:
                evidence["stage_2"]["match"] = False
        else:
            if "stage_2" not in evidence:
                evidence["stage_2"] = {
                    "evaluated": False,
                    "reason": "no_application_url",
                    "match": False,
                }

        # ---------------------------------------------------------------------
        # STAGE 3: Composite Deterministic Match (Confidence: 0.90)
        # ---------------------------------------------------------------------
        evidence["stage_3"] = {
            "evaluated": True,
            "company_id": str(candidate.company_id),
            "normalized_title": candidate.normalized_title,
            "normalized_location": candidate.normalized_location,
        }

        composite_candidates = await self.job_repo.find_by_composite_attributes(
            company_id=candidate.company_id,
            normalized_title=candidate.normalized_title,
        )

        unique_composite_jobs = list({j.id: j for j in composite_candidates}.values())
        evidence["stage_3"]["candidates_queried"] = len(unique_composite_jobs)

        evaluations: list[dict[str, Any]] = []
        matches: list[Any] = []
        ambiguous: list[Any] = []

        for cand_job in unique_composite_jobs:
            status, eval_evidence = evaluate_stage_3_candidate(candidate, cand_job, max_days=14)
            evaluations.append(eval_evidence)
            if status == "MATCH":
                matches.append(cand_job)
            elif status == "AMBIGUOUS":
                ambiguous.append(cand_job)

        evidence["stage_3"]["evaluations"] = evaluations

        # Evaluate Stage 3 candidate outcomes
        if len(matches) == 1 and len(ambiguous) == 0:
            matched_job = matches[0]
            evidence["stage_3"]["match"] = True
            evidence["stage_3"]["canonical_job_id"] = str(matched_job.id)
            return DeduplicationResult(
                action=DeduplicationAction.MATCH,
                stage=DeduplicationStage.STAGE_3_COMPOSITE,
                confidence=0.90,
                canonical_job_id=matched_job.id,
                matched_job=matched_job,
                candidate_job_ids=[matched_job.id],
                evidence=evidence,
            )
        elif len(matches) > 1 or (len(matches) == 1 and len(ambiguous) > 0):
            # Multiple conflicting candidate matches or collision with ambiguous posting
            evidence["stage_3"]["match"] = False
            evidence["stage_3"]["conflict"] = True
            conflicting_ids = [j.id for j in (matches + ambiguous)]
            return DeduplicationResult(
                action=DeduplicationAction.AMBIGUOUS,
                stage=DeduplicationStage.STAGE_3_COMPOSITE,
                confidence=0.90,
                review_reason="DUPLICATE_AMBIGUOUS",
                candidate_job_ids=conflicting_ids,
                evidence=evidence,
            )
        elif len(matches) == 0 and len(ambiguous) > 0:
            # Evidence insufficient to merge (e.g. date missing or ambiguous location)
            evidence["stage_3"]["match"] = False
            evidence["stage_3"]["ambiguous"] = True
            ambiguous_ids = [j.id for j in ambiguous]
            return DeduplicationResult(
                action=DeduplicationAction.AMBIGUOUS,
                stage=DeduplicationStage.STAGE_3_COMPOSITE,
                confidence=0.90,
                review_reason="DUPLICATE_AMBIGUOUS",
                candidate_job_ids=ambiguous_ids,
                evidence=evidence,
            )
        else:
            evidence["stage_3"]["match"] = False

        # ---------------------------------------------------------------------
        # DECISION: No Match -> New Canonical Job (Confidence: 0.0)
        # ---------------------------------------------------------------------
        return DeduplicationResult(
            action=DeduplicationAction.NEW,
            stage=DeduplicationStage.NONE,
            confidence=0.0,
            canonical_job_id=None,
            matched_job=None,
            candidate_job_ids=[],
            review_reason=None,
            evidence=evidence,
        )

    async def record_provenance_link(
        self,
        *,
        job_id: uuid.UUID,
        raw_job_id: uuid.UUID,
        source_id: uuid.UUID,
        source_job_id: str,
        source_url: str,
        source_job_id_type: str = "NATIVE",
        application_url: str | None = None,
        extracted_data: dict[str, Any] | None = None,
        posted_at: datetime | None = None,
        is_primary: bool = False,
    ) -> JobSourceReference:
        """Create or idempotently update a JobSourceReference provenance link without committing.

        If a provenance reference for (source_id, source_job_id) already exists for this job,
        refreshes its collected_at timestamp and metadata. If it is already linked to a different
        canonical job, raises ValueError to prevent unexplained database integrity violations.

        Transaction ownership remains strictly with the caller.
        """
        if self.jsr_repo is None:
            raise RuntimeError(
                "JobSourceReferenceRepository is not available on this service instance."
            )

        existing = await self.jsr_repo.get_by_source_external_id(source_id, source_job_id)
        if existing is not None:
            if existing.job_id != job_id:
                raise ValueError(
                    f"External source reference ({source_id}, '{source_job_id}') is already linked "
                    f"to canonical job {existing.job_id}; cannot link to {job_id}."
                )
            existing.collected_at = datetime.now(UTC)
            if application_url is not None:
                existing.application_url = application_url
            if extracted_data is not None:
                existing.extracted_data = extracted_data
            if posted_at is not None and existing.posted_at is None:
                existing.posted_at = posted_at
            session = getattr(self.jsr_repo, "session", None)
            if session is not None:
                await session.flush()
            return existing

        ref = JobSourceReference(
            id=uuid.uuid4(),
            job_id=job_id,
            raw_job_id=raw_job_id,
            source_id=source_id,
            source_job_id=source_job_id,
            source_job_id_type=source_job_id_type,
            source_url=source_url,
            application_url=application_url,
            extracted_data=extracted_data,
            posted_at=posted_at,
            is_primary=is_primary,
        )
        return await self.jsr_repo.create(ref)
