"""Unit tests for JobIngestionService and pipeline coordination.

Boundary: Isolated unit tests verifying:
1. Valid JSON and HTML ingestion envelopes.
2. Content hash validation, wire-to-payload match, and malformed input rejection.
3. Pipeline stage ordering and error propagation.
4. Deduplication outcomes: NEW, MATCH, and AMBIGUOUS.
5. Missing vs. explicit verification evidence handling.
6. Review and rejection outcomes.
7. Preservation of original raw bytes and input payloads.
8. Transaction ownership: zero repository or service session.commit() calls.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.deduplication.schemas import (
    DeduplicationAction,
    DeduplicationResult,
    DeduplicationStage,
)
from app.normalization.service import JobNormalizationService
from app.orchestration.decision_service import (
    JobFilterDecisionOutcome,
    JobFilterDecisionService,
)
from app.orchestration.ingestion_service import (
    STATUS_AMBIGUOUS_DUPLICATE,
    STATUS_PROCESSED_MATCH,
    STATUS_PROCESSED_NEW,
    JobIngestionEnvelope,
    JobIngestionInput,
    JobIngestionService,
)
from app.orchestration.verification_service import (
    JobVerificationOutcome,
    JobVerificationService,
)
from app.persistence.models.decision import FilterDecision, VerificationRecord
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.repositories.company_source_repository import (
    CompanySourceRepository,
)
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.rules.schemas import JobFilterResult
from app.verification.schemas import JobVerificationInput, JobVerificationResult

SAMPLE_DESCRIPTION = (
    "We are seeking an enthusiastic AI Software Engineer to join our growing team in Bengaluru. "
    "You will design, develop, and deploy scalable LLM applications, RAG pipelines, and agentic workflows. "
    "Key requirements include strong Python skills, familiarity with FastAPI, PostgreSQL, and modern AI frameworks. "
    "This is a full-time role offering great learning opportunities, mentorship, and impactful work."
)


def _make_sample_envelope(
    *,
    payload: dict[str, Any] | None = None,
    payload_format: str = "JSON",
    raw_body: bytes | None = None,
    content_hash: str | None = None,
    verification_evidence: JobVerificationInput | None = None,
    source_job_id: str = "job-101",
    source_job_id_type: str = "NATIVE",
    company_name: str = "Acme AI Corp",
) -> JobIngestionEnvelope:
    """Helper to build a valid sample JobIngestionEnvelope."""
    cid = uuid.uuid4()
    sid = uuid.uuid4()
    csid = uuid.uuid4()

    if payload is None:
        payload = {
            "title": "AI Engineer",
            "description": SAMPLE_DESCRIPTION,
            "location": "Bengaluru, Karnataka",
            "experience": "0-2 years",
            "salary_min": 1200000,
            "salary_max": 1800000,
            "salary_currency": "INR",
            "salary_period": "ANNUAL",
            "posted_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "application_url": "https://acme.careers/jobs/101",
        }

    if raw_body is None:
        if payload_format == "JSON":
            raw_body = json.dumps(payload).encode("utf-8")
        else:
            raw_body = b"<html><body><h1>AI Engineer</h1><p>Description</p></body></html>"

    return JobIngestionEnvelope(
        raw_body=raw_body,
        raw_payload=payload,
        source_id=sid,
        company_source_id=csid,
        company_id=cid,
        company_name=company_name,
        source_job_id=source_job_id,
        source_job_id_type=source_job_id_type,  # type: ignore[arg-type]
        source_url="https://acme.careers/jobs/101",
        application_url=payload.get("application_url"),
        payload_format=payload_format,  # type: ignore[arg-type]
        content_hash=content_hash,
        verification_evidence=verification_evidence,
    )


@pytest.fixture
def mock_session() -> MagicMock:
    """Mock SQLAlchemy AsyncSession."""
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.fixture
def mock_raw_repo(mock_session: MagicMock) -> MagicMock:
    """Mock RawJobRepository that assigns an ID and returns the passed RawJob."""
    repo = MagicMock(spec=RawJobRepository)
    repo.session = mock_session

    async def _create(raw_job: RawJob) -> RawJob:
        if not raw_job.id:
            raw_job.id = uuid.uuid4()
        return raw_job

    repo.create = AsyncMock(side_effect=_create)
    return repo


@pytest.fixture
def mock_job_repo(mock_session: MagicMock) -> MagicMock:
    """Mock JobRepository that assigns an ID and returns the passed Job."""
    repo = MagicMock(spec=JobRepository)
    repo.session = mock_session

    async def _create(job: Job) -> Job:
        if not job.id:
            job.id = uuid.uuid4()
        return job

    repo.create = AsyncMock(side_effect=_create)
    repo.get_by_id = AsyncMock(return_value=None)
    repo.update_status = AsyncMock(side_effect=lambda job, updates: job)
    repo.update_verification_status = AsyncMock(side_effect=lambda job, updates: job)
    return repo


@pytest.fixture
def mock_dedup_service(mock_session: MagicMock) -> MagicMock:
    """Mock JobDeduplicationService returning NEW action by default."""
    service = MagicMock()
    service.session = mock_session
    service.deduplicate = AsyncMock(
        return_value=DeduplicationResult(
            action=DeduplicationAction.NEW,
            stage=DeduplicationStage.NONE,
            confidence=0.0,
            review_reason=None,
        )
    )

    async def _record_provenance_link(*args: Any, **kwargs: Any) -> JobSourceReference:
        return JobSourceReference(
            id=uuid.uuid4(),
            job_id=kwargs.get("job_id", uuid.uuid4()),
            raw_job_id=kwargs.get("raw_job_id", uuid.uuid4()),
            source_id=kwargs.get("source_id", uuid.uuid4()),
            source_job_id=kwargs.get("source_job_id", "job-101"),
            source_url=kwargs.get("source_url", "https://acme.careers/jobs/101"),
            is_primary=kwargs.get("is_primary", True),
        )

    service.record_provenance_link = AsyncMock(side_effect=_record_provenance_link)
    return service


@pytest.fixture
def mock_verification_service(mock_session: MagicMock) -> MagicMock:
    """Mock JobVerificationService returning a verified JobVerificationOutcome."""
    service = MagicMock(spec=JobVerificationService)
    service.session = mock_session

    async def _evaluate_and_persist(job_or_id: Any, **kwargs: Any) -> JobVerificationOutcome:
        job = job_or_id if isinstance(job_or_id, Job) else MagicMock(spec=Job)
        record = VerificationRecord(
            id=uuid.uuid4(),
            job_id=job.id,
            passed=True,
            verification_confidence=Decimal("0.90"),
            jd_verified=True,
            source_verified=True,
            application_url_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            posting_date_verified=True,
            audit_trail={},
        )
        result = MagicMock(spec=JobVerificationResult)
        result.passed = True
        result.confidence_score = Decimal("0.90")
        result.review_reasons = []
        result.failure_reasons = []
        return JobVerificationOutcome(record=record, result=result, job=job)

    service.evaluate_and_persist = AsyncMock(side_effect=_evaluate_and_persist)
    return service


@pytest.fixture
def mock_decision_service(mock_session: MagicMock) -> MagicMock:
    """Mock JobFilterDecisionService returning an ELIGIBLE JobFilterDecisionOutcome."""
    service = MagicMock(spec=JobFilterDecisionService)
    service.session = mock_session

    async def _evaluate_and_persist(job_or_id: Any, **kwargs: Any) -> JobFilterDecisionOutcome:
        job = job_or_id if isinstance(job_or_id, Job) else MagicMock(spec=Job)
        job.eligibility_status = "ELIGIBLE"
        job.role_priority = "P1"
        job.relevance_status = "TARGET"
        decision = FilterDecision(
            id=uuid.uuid4(),
            job_id=job.id,
            rule_version="1.0.0",
            final_status="ELIGIBLE",
            role_priority="P1",
            reason_codes=[],
            evaluation_context={},
        )
        result = MagicMock(spec=JobFilterResult)
        result.eligibility_status = "ELIGIBLE"
        result.role_priority = "P1"
        result.relevance_status = "TARGET"
        result.primary_review_reason = None
        result.all_review_reasons = []
        return JobFilterDecisionOutcome(job=job, decision=decision, result=result)

    service.evaluate_and_persist = AsyncMock(side_effect=_evaluate_and_persist)
    return service


@pytest.fixture
def mock_cs_repo(mock_session: MagicMock) -> MagicMock:
    """Mock CompanySourceRepository for identifier consistency verification."""
    repo = MagicMock(spec=CompanySourceRepository)
    repo.session = mock_session

    registry: dict[uuid.UUID, Any] = {}

    def register(cs_id: uuid.UUID, company_id: uuid.UUID, source_id: uuid.UUID) -> None:
        registry[cs_id] = MagicMock(id=cs_id, company_id=company_id, source_id=source_id)

    repo.register = register

    class AlwaysMatches:
        def __eq__(self, other: Any) -> bool:
            return True

        def __ne__(self, other: Any) -> bool:
            return False

    async def _get_by_id(cs_id: uuid.UUID) -> Any:
        if cs_id in registry:
            return registry[cs_id]
        return MagicMock(id=cs_id, company_id=AlwaysMatches(), source_id=AlwaysMatches())

    repo.get_by_id = AsyncMock(side_effect=_get_by_id)
    return repo


@pytest.fixture
def ingestion_service(
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
    mock_cs_repo: MagicMock,
    mock_dedup_service: MagicMock,
    mock_verification_service: MagicMock,
    mock_decision_service: MagicMock,
) -> JobIngestionService:
    """Construct a JobIngestionService with all mocked collaborators."""
    return JobIngestionService(
        raw_job_repository=mock_raw_repo,
        job_repository=mock_job_repo,
        company_source_repository=mock_cs_repo,
        normalization_service=JobNormalizationService(),
        deduplication_service=mock_dedup_service,
        verification_service=mock_verification_service,
        decision_service=mock_decision_service,
    )


# ===========================================================================
# 1. Ingestion Envelope Validation & Error Handling
# ===========================================================================


def test_envelope_creation_and_alias() -> None:
    """Verify JobIngestionEnvelope and JobIngestionInput alias behavior."""
    assert JobIngestionInput is JobIngestionEnvelope

    env = _make_sample_envelope()
    assert env.company_name == "Acme AI Corp"
    assert env.source_job_id == "job-101"
    assert env.payload_format == "JSON"


def test_envelope_content_hash_mismatch_raises() -> None:
    """Verify ValueError is raised when caller-provided content_hash diverges from raw_body digest."""
    service = JobIngestionService(
        raw_job_repository=MagicMock(),
        job_repository=MagicMock(),
    )
    env = _make_sample_envelope(
        content_hash="0000000000000000000000000000000000000000000000000000000000000000"
    )

    with pytest.raises(ValueError, match="content_hash mismatch"):
        service._validate_envelope(env)


def test_envelope_json_wire_to_payload_mismatch_raises() -> None:
    """Verify ValueError is raised when raw_body JSON differs from parsed raw_payload."""
    service = JobIngestionService(
        raw_job_repository=MagicMock(),
        job_repository=MagicMock(),
    )
    wire_bytes = json.dumps({"title": "Original Title"}).encode("utf-8")
    divergent_payload = {"title": "Tampered Title"}

    env = _make_sample_envelope(raw_body=wire_bytes, payload=divergent_payload)

    with pytest.raises(ValueError, match="Wire JSON in raw_body does not match raw_payload"):
        service._validate_envelope(env)


def test_envelope_invalid_utf8_raises() -> None:
    """Verify ValueError is raised when raw_body is invalid UTF-8 for JSON payload format."""
    service = JobIngestionService(
        raw_job_repository=MagicMock(),
        job_repository=MagicMock(),
    )
    invalid_utf8 = b"\x80\x81\x82\xff"
    env = _make_sample_envelope(raw_body=invalid_utf8, payload={"some": "dict"})

    with pytest.raises(ValueError, match="invalid UTF-8 bytes"):
        service._validate_envelope(env)


def test_envelope_malformed_json_raises() -> None:
    """Verify ValueError is raised when raw_body is not valid JSON."""
    service = JobIngestionService(
        raw_job_repository=MagicMock(),
        job_repository=MagicMock(),
    )
    malformed_json = b"{'title': invalid json}"
    env = _make_sample_envelope(raw_body=malformed_json, payload={"some": "dict"})

    with pytest.raises(ValueError, match="does not contain valid JSON"):
        service._validate_envelope(env)


def test_envelope_empty_identifiers_raise() -> None:
    """Verify empty string identifiers are rejected."""
    service = JobIngestionService(
        raw_job_repository=MagicMock(),
        job_repository=MagicMock(),
    )

    with pytest.raises(ValueError, match="source_job_id cannot be empty"):
        env = _make_sample_envelope(source_job_id="   ")
        service._validate_envelope(env)

    with pytest.raises(ValueError, match="company_name cannot be empty"):
        env = _make_sample_envelope(company_name="   ")
        service._validate_envelope(env)


@pytest.mark.asyncio
async def test_envelope_source_id_mismatch_rejected_before_writes(
    ingestion_service: JobIngestionService,
    mock_cs_repo: MagicMock,
    mock_raw_repo: MagicMock,
) -> None:
    """Verify envelope source_id mismatch with CompanySource registry is rejected before any DB writes."""
    env = _make_sample_envelope()
    actual_source_id = uuid.uuid4()  # different from env.source_id
    mock_cs_repo.register(
        cs_id=env.company_source_id,
        company_id=env.company_id,
        source_id=actual_source_id,
    )

    with pytest.raises(
        ValueError, match="envelope source_id '.*' does not match CompanySource source_id"
    ):
        await ingestion_service.ingest(env)

    mock_raw_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_envelope_company_id_mismatch_rejected_before_writes(
    ingestion_service: JobIngestionService,
    mock_cs_repo: MagicMock,
    mock_raw_repo: MagicMock,
) -> None:
    """Verify envelope company_id mismatch with CompanySource registry is rejected before any DB writes."""
    env = _make_sample_envelope()
    actual_company_id = uuid.uuid4()  # different from env.company_id
    mock_cs_repo.register(
        cs_id=env.company_source_id,
        company_id=actual_company_id,
        source_id=env.source_id,
    )

    with pytest.raises(
        ValueError, match="envelope company_id '.*' does not match CompanySource company_id"
    ):
        await ingestion_service.ingest(env)

    mock_raw_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_envelope_nonexistent_company_source_rejected_before_writes(
    ingestion_service: JobIngestionService,
    mock_cs_repo: MagicMock,
    mock_raw_repo: MagicMock,
) -> None:
    """Verify non-existent company_source_id is rejected before any DB writes."""
    mock_cs_repo.get_by_id = AsyncMock(return_value=None)
    env = _make_sample_envelope()

    with pytest.raises(ValueError, match="CompanySource mapping with id '.*' does not exist"):
        await ingestion_service.ingest(env)

    mock_raw_repo.create.assert_not_called()


# ===========================================================================
# 2. Pipeline Stage Execution & Deduplication Outcomes
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_new_canonical_job_flow(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
    mock_dedup_service: MagicMock,
    mock_verification_service: MagicMock,
    mock_decision_service: MagicMock,
    mock_session: MagicMock,
) -> None:
    """Verify end-to-end flow for a NEW job candidate."""
    env = _make_sample_envelope()

    outcome = await ingestion_service.ingest(env)

    # 1. Assert status and outcome structure
    assert outcome.status == STATUS_PROCESSED_NEW
    assert outcome.is_new is True
    assert outcome.is_match is False
    assert outcome.is_ambiguous is False
    assert outcome.raw_job is not None
    assert outcome.canonical_job is not None
    assert outcome.source_reference is not None
    assert outcome.verification_outcome is not None
    assert outcome.filter_decision_outcome is not None

    # 2. Assert Stage order & interactions
    mock_raw_repo.create.assert_awaited_once()
    mock_dedup_service.deduplicate.assert_awaited_once()
    mock_job_repo.create.assert_awaited_once()
    mock_dedup_service.record_provenance_link.assert_awaited_once()
    assert mock_dedup_service.record_provenance_link.call_args.kwargs["is_primary"] is True
    mock_verification_service.evaluate_and_persist.assert_awaited_once()
    mock_decision_service.evaluate_and_persist.assert_awaited_once()

    # 3. Assert zero session.commit calls
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_matched_duplicate_job_flow(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
    mock_dedup_service: MagicMock,
    mock_verification_service: MagicMock,
    mock_decision_service: MagicMock,
    mock_session: MagicMock,
) -> None:
    """Verify duplicate observation reuses canonical Job and appends secondary provenance."""
    existing_job = Job(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        company_name="Acme AI Corp",
        title="AI Engineer",
        normalized_title="ai engineer",
        description=SAMPLE_DESCRIPTION,
        role_priority="P1",
        relevance_status="TARGET",
        eligibility_status="ELIGIBLE",
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=True,
        verification_confidence=Decimal("1.00"),
    )

    # Configure deduplication to return MATCH
    mock_dedup_service.deduplicate = AsyncMock(
        return_value=DeduplicationResult(
            action=DeduplicationAction.MATCH,
            stage=DeduplicationStage.STAGE_1_EXACT_SOURCE_ID,
            confidence=1.0,
            canonical_job_id=existing_job.id,
            matched_job=existing_job,
        )
    )

    env = _make_sample_envelope()
    outcome = await ingestion_service.ingest(env)

    # Assert status and outcome
    assert outcome.status == STATUS_PROCESSED_MATCH
    assert outcome.is_match is True
    assert outcome.is_new is False
    assert outcome.is_ambiguous is False
    assert outcome.canonical_job is not None

    # Crucial invariant: job_repo.create was NOT called (no duplicate Job created)
    mock_job_repo.create.assert_not_called()

    # Provenance link recorded as secondary (is_primary=False)
    mock_dedup_service.record_provenance_link.assert_awaited_once()
    assert mock_dedup_service.record_provenance_link.call_args.kwargs["is_primary"] is False
    assert mock_dedup_service.record_provenance_link.call_args.kwargs["job_id"] == existing_job.id

    # Verification and filter decision history appended
    mock_verification_service.evaluate_and_persist.assert_awaited_once()
    mock_decision_service.evaluate_and_persist.assert_awaited_once()

    # Zero session.commit calls
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_ambiguous_duplicate_stops_at_boundary(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
    mock_dedup_service: MagicMock,
    mock_verification_service: MagicMock,
    mock_decision_service: MagicMock,
    mock_session: MagicMock,
) -> None:
    """Verify AMBIGUOUS deduplication halts canonical creation and flags review reason."""
    candidate_id_1 = uuid.uuid4()
    candidate_id_2 = uuid.uuid4()

    mock_dedup_service.deduplicate = AsyncMock(
        return_value=DeduplicationResult(
            action=DeduplicationAction.AMBIGUOUS,
            stage=DeduplicationStage.STAGE_3_COMPOSITE,
            confidence=0.90,
            review_reason="DUPLICATE_AMBIGUOUS",
            candidate_job_ids=[candidate_id_1, candidate_id_2],
        )
    )

    env = _make_sample_envelope()
    outcome = await ingestion_service.ingest(env)

    # 1. Assert outcome status
    assert outcome.status == STATUS_AMBIGUOUS_DUPLICATE
    assert outcome.is_ambiguous is True
    assert outcome.is_new is False
    assert outcome.is_match is False
    assert outcome.requires_review is True
    assert outcome.review_reasons == ["DUPLICATE_AMBIGUOUS"]

    # 2. Immutable raw discovery is preserved
    mock_raw_repo.create.assert_awaited_once()
    assert outcome.raw_job is not None

    # 3. Canonical Job and provenance are NEVER created
    mock_job_repo.create.assert_not_called()
    mock_dedup_service.record_provenance_link.assert_not_called()
    mock_verification_service.evaluate_and_persist.assert_not_called()
    mock_decision_service.evaluate_and_persist.assert_not_called()

    assert outcome.canonical_job is None
    assert outcome.source_reference is None
    assert outcome.verification_outcome is None
    assert outcome.filter_decision_outcome is None

    # Zero session.commit calls
    mock_session.commit.assert_not_called()


# ===========================================================================
# 3. Verification & Filtering Review Aggregation
# ===========================================================================


@pytest.mark.asyncio
async def test_ingest_aggregates_review_reasons_deterministically(
    ingestion_service: JobIngestionService,
    mock_verification_service: MagicMock,
    mock_decision_service: MagicMock,
) -> None:
    """Verify review reasons from decision and verification are cleanly aggregated without duplicates."""

    # Verification reports review reason
    async def _verif_with_review(job_or_id: Any, **kwargs: Any) -> JobVerificationOutcome:
        job = job_or_id if isinstance(job_or_id, Job) else MagicMock(spec=Job)
        record = VerificationRecord(
            id=uuid.uuid4(),
            job_id=job.id,
            passed=True,
            verification_confidence=Decimal("0.70"),
            jd_verified=True,
            source_verified=True,
            application_url_verified=True,
            company_verified=True,
            location_verified=True,
            experience_verified=True,
            salary_verified=True,
            posting_date_verified=True,
            audit_trail={},
        )
        result = MagicMock(spec=JobVerificationResult)
        result.review_reasons = ["SALARY_UNKNOWN", "POSTED_DATE_SUSPICIOUS"]
        return JobVerificationOutcome(record=record, result=result, job=job)

    mock_verification_service.evaluate_and_persist = AsyncMock(side_effect=_verif_with_review)

    # Decision reports primary review reason
    async def _decision_with_review(job_or_id: Any, **kwargs: Any) -> JobFilterDecisionOutcome:
        job = job_or_id if isinstance(job_or_id, Job) else MagicMock(spec=Job)
        job.eligibility_status = "REVIEW"
        job.review_reason = "LOCATION_AMBIGUOUS"
        decision = FilterDecision(
            id=uuid.uuid4(),
            job_id=job.id,
            rule_version="1.0.0",
            final_status="REVIEW",
            role_priority="P1",
            reason_codes=["LOCATION_AMBIGUOUS"],
            evaluation_context={},
        )
        result = MagicMock(spec=JobFilterResult)
        result.eligibility_status = "REVIEW"
        result.primary_review_reason = "LOCATION_AMBIGUOUS"
        return JobFilterDecisionOutcome(job=job, decision=decision, result=result)

    mock_decision_service.evaluate_and_persist = AsyncMock(side_effect=_decision_with_review)

    env = _make_sample_envelope()
    outcome = await ingestion_service.ingest(env)

    assert outcome.requires_review is True
    # Order: decision review reason, then verification review reasons
    assert outcome.review_reasons == [
        "LOCATION_AMBIGUOUS",
        "SALARY_UNKNOWN",
        "POSTED_DATE_SUSPICIOUS",
    ]


@pytest.mark.asyncio
async def test_ingest_handles_html_payload_format(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
) -> None:
    """Verify HTML format is accepted, raw bytes preserved, and text cleaned."""
    raw_html = b"<html><body><h1>Senior AI Lead</h1><p>We need an experienced Python developer.</p></body></html>"
    payload = {
        "title": "Senior AI Lead",
        "description": "<p>We need an experienced Python developer.</p>",
        "location": "Pune",
    }
    env = _make_sample_envelope(
        payload=payload,
        payload_format="HTML",
        raw_body=raw_html,
    )

    outcome = await ingestion_service.ingest(env)

    assert outcome.status == STATUS_PROCESSED_NEW
    # Verify exact raw HTML bytes were persisted in raw_job
    mock_raw_repo.create.assert_awaited_once()
    persisted_raw_job = mock_raw_repo.create.call_args[0][0]
    assert persisted_raw_job.raw_body == raw_html
    assert persisted_raw_job.payload_format == "HTML"


# ===========================================================================
# 4. Error Propagation & Input Immutability
# ===========================================================================


@pytest.mark.asyncio
async def test_normalization_failure_propagates_without_partial_canonical_job(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
) -> None:
    """Verify normalization failure (missing title) aborts workflow and prevents canonical Job creation."""
    # Payload with empty title
    payload = {
        "title": "   ",
        "description": "Some description",
    }
    raw_body = json.dumps(payload).encode("utf-8")
    env = _make_sample_envelope(payload=payload, raw_body=raw_body)

    with pytest.raises(ValueError, match="Job title is required and cannot be empty"):
        await ingestion_service.ingest(env)

    # RawJob was saved (Stage 2)
    mock_raw_repo.create.assert_awaited_once()
    # Canonical Job was NEVER created
    mock_job_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_input_immutability(
    ingestion_service: JobIngestionService,
) -> None:
    """Verify original envelope raw_payload and raw_body are not mutated during ingestion."""
    payload = {
        "title": "AI Engineer",
        "description": SAMPLE_DESCRIPTION,
        "skills": ["Python", "FastAPI"],
    }
    raw_body = json.dumps(payload).encode("utf-8")
    env = _make_sample_envelope(payload=payload, raw_body=raw_body)

    orig_skills = list(payload["skills"])
    await ingestion_service.ingest(env)

    assert env.raw_payload["skills"] == orig_skills
    assert env.raw_body == raw_body


@pytest.mark.asyncio
async def test_ingest_explicit_verification_evidence_forwarded(
    ingestion_service: JobIngestionService,
    mock_verification_service: MagicMock,
) -> None:
    """Verify explicitly supplied verification observations are forwarded without distortion."""
    explicit_evidence = JobVerificationInput(
        company_name="Acme AI Corp",
        source_name="greenhouse",
        is_source_approved=True,
        application_url_reachable=True,
    )
    env = _make_sample_envelope(verification_evidence=explicit_evidence)

    await ingestion_service.ingest(env)

    mock_verification_service.evaluate_and_persist.assert_awaited_once()
    passed_evidence = mock_verification_service.evaluate_and_persist.call_args.kwargs["evidence"]
    assert passed_evidence == explicit_evidence
    assert passed_evidence.is_source_approved is True
    assert passed_evidence.application_url_reachable is True


@pytest.mark.asyncio
async def test_ingest_rejection_outcome_captured(
    ingestion_service: JobIngestionService,
    mock_decision_service: MagicMock,
) -> None:
    """Verify rejection outcome and rejection reason are accurately surfaced."""

    async def _decision_rejection(job_or_id: Any, **kwargs: Any) -> JobFilterDecisionOutcome:
        job = job_or_id if isinstance(job_or_id, Job) else MagicMock(spec=Job)
        job.eligibility_status = "REJECTED"
        job.rejection_reason = "JOB_TOO_OLD"
        decision = FilterDecision(
            id=uuid.uuid4(),
            job_id=job.id,
            rule_version="1.0.0",
            final_status="REJECTED",
            role_priority="P1",
            reason_codes=["JOB_TOO_OLD"],
            evaluation_context={},
        )
        result = MagicMock(spec=JobFilterResult)
        result.eligibility_status = "REJECTED"
        result.rejection_reason = "JOB_TOO_OLD"
        result.primary_review_reason = None
        result.all_review_reasons = []
        return JobFilterDecisionOutcome(job=job, decision=decision, result=result)

    mock_decision_service.evaluate_and_persist = AsyncMock(side_effect=_decision_rejection)

    env = _make_sample_envelope()
    outcome = await ingestion_service.ingest(env)

    assert outcome.canonical_job is not None
    assert outcome.canonical_job.eligibility_status == "REJECTED"
    assert outcome.canonical_job.rejection_reason == "JOB_TOO_OLD"


@pytest.mark.asyncio
async def test_ingest_repository_error_propagates(
    ingestion_service: JobIngestionService,
    mock_raw_repo: MagicMock,
    mock_job_repo: MagicMock,
) -> None:
    """Verify database/repository exceptions propagate immediately without masking."""
    mock_raw_repo.create = AsyncMock(side_effect=RuntimeError("Database connection lost"))

    env = _make_sample_envelope()

    with pytest.raises(RuntimeError, match="Database connection lost"):
        await ingestion_service.ingest(env)

    # Downstream pipeline stages never executed
    mock_job_repo.create.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_without_verification_evidence_forwards_none(
    ingestion_service: JobIngestionService,
    mock_verification_service: MagicMock,
) -> None:
    """Verify ingestion without verification evidence forwards evidence=None so that fallback rules apply."""
    env = _make_sample_envelope(verification_evidence=None)

    await ingestion_service.ingest(env)

    mock_verification_service.evaluate_and_persist.assert_awaited_once()
    evidence_passed = mock_verification_service.evaluate_and_persist.call_args.kwargs["evidence"]
    assert evidence_passed is None
