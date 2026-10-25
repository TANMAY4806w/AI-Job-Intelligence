"""Live PostgreSQL integration tests for JobIngestionService and pipeline coordination.

Boundary: End-to-end integration tests using PostgreSQL 18.
Validates:
1. A complete successful ingestion transaction creating RawJob, Job, JobSourceReference,
   VerificationRecord, and FilterDecision.
2. Duplicate source observation reuses the correct canonical job and preserves provenance.
3. Ambiguous deduplication never silently creates an additional canonical job.
4. Verification and filter decision history is persisted correctly across runs.
5. Transaction rollback completely reverts all writes performed by the ingestion workflow.
6. Raw HTTP response bytes round-trip with byte-for-byte exactness.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.orchestration.ingestion_service import (
    STATUS_AMBIGUOUS_DUPLICATE,
    STATUS_PROCESSED_MATCH,
    STATUS_PROCESSED_NEW,
    JobIngestionEnvelope,
    JobIngestionService,
)
from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import (
    CompanySourceRepository,
)
from app.persistence.repositories.filter_decision_repository import (
    FilterDecisionRepository,
)
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.persistence.repositories.source_repository import SourceRepository
from app.persistence.repositories.verification_record_repository import (
    VerificationRecordRepository,
)
from app.persistence.session import dispose_engine, get_sessionmaker
from app.verification.schemas import JobVerificationInput

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

SAMPLE_JD_TEXT = (
    "We are seeking an enthusiastic AI Software Engineer to join our growing team in Bengaluru. "
    "You will design, develop, and deploy scalable LLM applications, RAG pipelines, and agentic workflows. "
    "Key requirements include strong Python skills, familiarity with FastAPI, PostgreSQL, and modern AI frameworks. "
    "This is a full-time role offering great learning opportunities, mentorship, and impactful work."
)


@pytest.fixture(autouse=True)
def cleanup_engine_after_test() -> None:
    """Cleanly dispose of pooled database connections after each test."""
    yield
    asyncio.run(dispose_engine())


@pytest.mark.asyncio
async def test_successful_new_job_ingestion_transaction_integration() -> None:
    """Verify complete end-to-end ingestion creates and links all 5 persistent entities."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-ingest-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"
    base_posted_at = datetime.now(UTC) - timedelta(days=1)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)
            verif_repo = VerificationRecordRepository(session)
            decision_repo = FilterDecisionRepository(session)

            # 1. Seed Company, Source, and CompanySource
            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Ingest Test Corp {unique_suffix}",
                    normalized_name=f"ingest-test-corp-{unique_suffix}",
                    sector="IT",
                    business_model="PRODUCT",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"INGEST_GH_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            # 2. Build Ingestion Envelope with explicit verification evidence
            raw_payload = {
                "title": "AI Engineer",
                "description": SAMPLE_JD_TEXT,
                "location": "Bengaluru, Karnataka",
                "experience_raw": "0-2 years",
                "salary_min": 1200000,
                "salary_max": 1800000,
                "salary_currency": "INR",
                "salary_period": "ANNUAL",
                "posted_at": base_posted_at.isoformat(),
                "application_url": app_url,
            }
            raw_body = json.dumps(raw_payload).encode("utf-8")

            verif_evidence = JobVerificationInput(
                source_id=source.id,
                source_name="greenhouse",
                source_type="ATS",
                is_source_approved=True,
                payload_format_valid=True,
                description=SAMPLE_JD_TEXT,
                http_status_code=200,
                is_page_active=True,
                application_url=app_url,
                application_url_status=200,
                application_url_reachable=True,
                company_id=company.id,
                company_name=company.name,
                is_company_registry_verified=True,
                location="Bengaluru, Karnataka",
                normalized_location="Bengaluru",
                experience_raw="0-2 years",
                experience_min_years=Decimal("0.0"),
                experience_max_years=Decimal("2.0"),
                salary_min=Decimal("1200000.00"),
                salary_max=Decimal("1800000.00"),
                salary_currency="INR",
                salary_period="ANNUAL",
                posted_at=base_posted_at,
                publication_date_corroborated=True,
                posting_date_verified=True,
            )

            envelope = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=raw_payload,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_job_id_type="NATIVE",
                source_url=app_url,
                application_url=app_url,
                payload_format="JSON",
                collected_at=datetime.now(UTC),
                verification_evidence=verif_evidence,
            )

            # 3. Execute JobIngestionService
            ingestion_service = JobIngestionService.from_session(session)
            outcome = await ingestion_service.ingest(envelope)

            # 4. Verify Ingestion Outcome
            assert outcome.status == STATUS_PROCESSED_NEW
            assert outcome.is_new is True
            assert outcome.is_match is False
            assert outcome.is_ambiguous is False
            assert outcome.raw_job is not None
            assert outcome.canonical_job is not None
            assert outcome.source_reference is not None
            assert outcome.verification_outcome is not None
            assert outcome.filter_decision_outcome is not None

            # Canonical Job has been verified and filter-evaluated
            job = outcome.canonical_job
            assert job.title == "AI Engineer"
            assert job.normalized_title == "ai engineer"
            assert job.normalized_location == "Bengaluru"
            assert job.eligibility_status == "ELIGIBLE"
            assert job.role_priority == "P1"
            assert job.relevance_status == "TARGET"
            assert job.source_verified is True
            assert job.jd_verified is True
            assert job.verification_confidence > Decimal("0.80")

            # 5. Verify PostgreSQL persistence via individual repositories
            persisted_raw = await raw_repo.get_by_id(outcome.raw_job.id)
            assert persisted_raw is not None
            assert persisted_raw.raw_body == raw_body
            assert persisted_raw.content_hash == hashlib.sha256(raw_body).hexdigest()

            persisted_job = await job_repo.get_by_id(job.id)
            assert persisted_job is not None
            assert persisted_job.id == job.id

            persisted_ref = await jsr_repo.get_by_source_external_id(source.id, ext_job_id)
            assert persisted_ref is not None
            assert persisted_ref.job_id == job.id
            assert persisted_ref.raw_job_id == persisted_raw.id
            assert persisted_ref.is_primary is True

            persisted_verif = await verif_repo.get_latest_by_job_id(job.id)
            assert persisted_verif is not None
            assert persisted_verif.passed is True
            assert persisted_verif.posting_date_verified is True

            persisted_decision = await decision_repo.get_latest_by_job_id(job.id)
            assert persisted_decision is not None
            assert persisted_decision.final_status == "ELIGIBLE"
            assert persisted_decision.role_priority == "P1"

    finally:
        pass


@pytest.mark.asyncio
async def test_duplicate_source_observation_reuses_canonical_job_integration() -> None:
    """Verify duplicate observation reuses existing canonical Job and appends provenance & audit history."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-dup-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"
    base_posted_at = datetime.now(UTC) - timedelta(days=1)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            verif_repo = VerificationRecordRepository(session)
            decision_repo = FilterDecisionRepository(session)

            # Setup Company & Source
            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Dup Test Corp {unique_suffix}",
                    normalized_name=f"dup-test-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"DUP_GH_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            raw_payload = {
                "title": "Machine Learning Engineer",
                "description": SAMPLE_JD_TEXT,
                "location": "Bengaluru, Karnataka",
                "experience": "0-2 years",
                "salary_min": 1400000,
                "salary_max": 2000000,
                "salary_currency": "INR",
                "salary_period": "ANNUAL",
                "posted_at": base_posted_at.isoformat(),
                "application_url": app_url,
            }
            raw_body = json.dumps(raw_payload).encode("utf-8")

            envelope_1 = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=raw_payload,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_job_id_type="NATIVE",
                source_url=app_url,
                application_url=app_url,
                payload_format="JSON",
            )

            ingestion_service = JobIngestionService.from_session(session)

            # 1. Ingest First Time (PROCESSED_NEW)
            outcome_1 = await ingestion_service.ingest(envelope_1)
            assert outcome_1.status == STATUS_PROCESSED_NEW
            canonical_job_id = outcome_1.canonical_job.id

            # 2. Ingest Second Time (Duplicate Observation)
            outcome_2 = await ingestion_service.ingest(envelope_1)

            # Verify MATCH status and canonical reuse
            assert outcome_2.status == STATUS_PROCESSED_MATCH
            assert outcome_2.is_match is True
            assert outcome_2.canonical_job.id == canonical_job_id

            # Raw discovery records are distinct (both preserved)
            assert outcome_2.raw_job.id != outcome_1.raw_job.id
            assert await raw_repo.get_by_id(outcome_2.raw_job.id) is not None

            # Canonical jobs table has only 1 row for this company
            jobs_list = await job_repo.list_by_company_id(company.id)
            assert len(jobs_list) == 1

            # Verification history has appended a 2nd record
            verif_history = await verif_repo.list_by_job_id(canonical_job_id)
            assert len(verif_history) == 2

            # Filter decision history has appended a 2nd decision
            decision_history = await decision_repo.list_by_job_id(canonical_job_id)
            assert len(decision_history) == 2

    finally:
        pass


@pytest.mark.asyncio
async def test_ambiguous_deduplication_never_creates_canonical_job_integration() -> None:
    """Verify ambiguous candidate match halts at boundary without creating a duplicate Job in PostgreSQL."""
    unique_suffix = uuid.uuid4().hex[:8]
    base_posted_at = datetime.now(UTC) - timedelta(days=2)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            job_repo = JobRepository(session)
            raw_repo = RawJobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Ambiguous Corp {unique_suffix}",
                    normalized_name=f"ambiguous-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"AMBIG_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            # Seed Two Conflicting Existing Jobs for this company with identical title & location
            job_1 = await job_repo.create(
                Job(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    company_name=company.name,
                    title="Backend Python Engineer",
                    normalized_title="backend python engineer",
                    location="Pune, Maharashtra",
                    normalized_location="Pune",
                    description="Existing vacancy team A.",
                    role_priority="P1",
                    relevance_status="TARGET",
                    eligibility_status="ELIGIBLE",
                    source_verified=True,
                    jd_verified=True,
                    company_verified=True,
                    location_verified=True,
                    experience_verified=True,
                    salary_verified=False,
                    verification_confidence=Decimal("0.85"),
                    posted_at=base_posted_at,
                )
            )
            job_2 = await job_repo.create(
                Job(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    company_name=company.name,
                    title="Backend Python Engineer",
                    normalized_title="backend python engineer",
                    location="Pune, Maharashtra",
                    normalized_location="Pune",
                    description="Existing vacancy team B.",
                    role_priority="P1",
                    relevance_status="TARGET",
                    eligibility_status="ELIGIBLE",
                    source_verified=True,
                    jd_verified=True,
                    company_verified=True,
                    location_verified=True,
                    experience_verified=True,
                    salary_verified=False,
                    verification_confidence=Decimal("0.85"),
                    posted_at=base_posted_at + timedelta(days=2),
                )
            )

            # Ingest an incoming posting matching both candidates (Stage 3 conflict -> AMBIGUOUS)
            incoming_payload = {
                "title": "Backend Python Engineer",
                "description": "Incoming listing matching multiple existing postings.",
                "location": "Pune, Maharashtra",
                "posted_at": base_posted_at.isoformat(),
            }
            raw_body = json.dumps(incoming_payload).encode("utf-8")

            envelope = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=incoming_payload,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=f"ambig-ext-{unique_suffix}",
                source_url="https://ambig.careers/jobs/new",
                payload_format="JSON",
            )

            ingestion_service = JobIngestionService.from_session(session)
            outcome = await ingestion_service.ingest(envelope)

            # 1. Assert Ambiguous Duplicate Outcome
            assert outcome.status == STATUS_AMBIGUOUS_DUPLICATE
            assert outcome.is_ambiguous is True
            assert outcome.requires_review is True
            assert outcome.review_reasons == ["DUPLICATE_AMBIGUOUS"]
            assert outcome.canonical_job is None
            assert outcome.source_reference is None
            assert (
                await jsr_repo.get_by_source_external_id(source.id, f"ambig-ext-{unique_suffix}")
                is None
            )

            # 2. Assert RawJob was preserved for auditability
            assert outcome.raw_job is not None
            persisted_raw = await raw_repo.get_by_id(outcome.raw_job.id)
            assert persisted_raw is not None

            # 3. Assert canonical Jobs table still contains exactly the 2 original seeded jobs
            jobs_after = await job_repo.list_by_company_id(company.id)
            assert len(jobs_after) == 2
            assert {j.id for j in jobs_after} == {job_1.id, job_2.id}

    finally:
        pass


@pytest.mark.asyncio
async def test_transaction_rollback_reverts_all_writes_integration() -> None:
    """Verify session.rollback() completely removes all writes created by an ingestion workflow."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-rollback-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"

    session_factory = get_sessionmaker()

    # Step 1: Ingest job, then rollback session
    async with session_factory() as session:
        company_repo = CompanyRepository(session)
        source_repo = SourceRepository(session)
        cs_repo = CompanySourceRepository(session)

        company = await company_repo.create(
            Company(
                id=uuid.uuid4(),
                name=f"Rollback Corp {unique_suffix}",
                normalized_name=f"rollback-corp-{unique_suffix}",
            )
        )
        source = await source_repo.create(
            Source(
                id=uuid.uuid4(),
                source_name=f"RB_GH_{unique_suffix.upper()}",
                source_type="ATS_API",
            )
        )
        cs = await cs_repo.create(
            CompanySource(
                id=uuid.uuid4(),
                company_id=company.id,
                source_id=source.id,
                ats_identifier=f"token-{unique_suffix}",
                discovery_method="AUTOMATED_PROBE",
            )
        )

        payload = {
            "title": "Data Scientist",
            "description": SAMPLE_JD_TEXT,
            "location": "Bengaluru",
        }
        raw_body = json.dumps(payload).encode("utf-8")

        envelope = JobIngestionEnvelope(
            raw_body=raw_body,
            raw_payload=payload,
            source_id=source.id,
            company_source_id=cs.id,
            company_id=company.id,
            company_name=company.name,
            source_job_id=ext_job_id,
            source_url=app_url,
            payload_format="JSON",
        )

        service = JobIngestionService.from_session(session)
        outcome = await service.ingest(envelope)

        raw_job_id = outcome.raw_job.id
        canonical_job_id = outcome.canonical_job.id

        # Explicitly ROLL BACK the transaction
        await session.rollback()

    # Step 2: Query database in a fresh session to confirm zero records survived
    async with session_factory() as fresh_session:
        raw_repo = RawJobRepository(fresh_session)
        job_repo = JobRepository(fresh_session)
        verif_repo = VerificationRecordRepository(fresh_session)
        decision_repo = FilterDecisionRepository(fresh_session)

        assert await raw_repo.get_by_id(raw_job_id) is None
        assert await job_repo.get_by_id(canonical_job_id) is None
        assert await verif_repo.get_latest_by_job_id(canonical_job_id) is None
        assert await decision_repo.get_latest_by_job_id(canonical_job_id) is None


@pytest.mark.asyncio
async def test_raw_bytes_exact_round_trip_integration() -> None:
    """Verify arbitrary raw response bytes round-trip from PostgreSQL byte-for-byte."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-bytes-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Bytes Corp {unique_suffix}",
                    normalized_name=f"bytes-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"BYTES_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            # Test bytes with multi-byte UTF-8, emojis, HTML entities, and formatting
            original_bytes = (
                b'{"title": "AI & ML Specialist \xf0\x9f\x9a\x80\xe2\x9a\xa1", '
                b'"description": "High-fidelity preservation: &amp; &lt;tag&gt; \\u2014 \\u2022 \\n\\t", '
                b'"location": "Mumbai, Maharashtra"}'
            )
            parsed_payload = json.loads(original_bytes.decode("utf-8"))

            envelope = JobIngestionEnvelope(
                raw_body=original_bytes,
                raw_payload=parsed_payload,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_url=app_url,
                payload_format="JSON",
            )

            service = JobIngestionService.from_session(session)
            outcome = await service.ingest(envelope)

            # Retrieve from PostgreSQL and verify byte-for-byte equality
            retrieved_raw = await raw_repo.get_by_id(outcome.raw_job.id)
            assert retrieved_raw is not None
            assert bytes(retrieved_raw.raw_body) == original_bytes
            assert retrieved_raw.content_hash == hashlib.sha256(original_bytes).hexdigest()

    finally:
        pass


@pytest.mark.asyncio
async def test_envelope_identifier_mismatch_rejected_without_downstream_writes_integration() -> (
    None
):
    """Verify mismatched envelope identifiers are rejected before any downstream writes are made in PostgreSQL."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-mismatch-{unique_suffix}"
    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Mismatch Corp {unique_suffix}",
                    normalized_name=f"mismatch-corp-{unique_suffix}",
                )
            )
            other_company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Other Corp {unique_suffix}",
                    normalized_name=f"other-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"MISMATCH_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            other_source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"OTHER_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            service = JobIngestionService.from_session(session)

            # 1. Test Source ID Mismatch
            envelope_bad_source = JobIngestionEnvelope(
                raw_body=b'{"title": "Mismatch Source"}',
                raw_payload={"title": "Mismatch Source"},
                source_id=other_source.id,  # DOES NOT MATCH cs.source_id
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_url="https://boards.greenhouse.io/acme/jobs/mismatch-source",
            )

            with pytest.raises(ValueError, match="does not match CompanySource.source_id"):
                await service.ingest(envelope_bad_source)

            # Confirm zero raw_jobs written for this ext_job_id
            raw_job_after_source_err = await raw_repo.get_latest_by_company_source_and_external_id(
                cs.id, ext_job_id
            )
            assert raw_job_after_source_err is None

            # 2. Test Company ID Mismatch
            envelope_bad_company = JobIngestionEnvelope(
                raw_body=b'{"title": "Mismatch Company"}',
                raw_payload={"title": "Mismatch Company"},
                source_id=source.id,
                company_source_id=cs.id,
                company_id=other_company.id,  # DOES NOT MATCH cs.company_id
                company_name=other_company.name,
                source_job_id=ext_job_id,
                source_url="https://boards.greenhouse.io/acme/jobs/mismatch-company",
            )

            with pytest.raises(ValueError, match="does not match CompanySource.company_id"):
                await service.ingest(envelope_bad_company)

            # Confirm still zero raw_jobs or jobs written
            raw_job_after_company_err = await raw_repo.get_latest_by_company_source_and_external_id(
                cs.id, ext_job_id
            )
            assert raw_job_after_company_err is None
            jobs_after_err = await job_repo.list_by_company_id(company.id)
            assert len(jobs_after_err) == 0

    finally:
        pass


@pytest.mark.asyncio
async def test_ingestion_without_verification_evidence_routes_to_review_regression_integration() -> (
    None
):
    """Verify ingestion without external verification evidence leaves publication date uncorroborated, routing to REVIEW."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-no-evidence-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"
    base_posted_at = datetime.now(UTC) - timedelta(days=1)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            job_repo = JobRepository(session)
            verif_repo = VerificationRecordRepository(session)
            decision_repo = FilterDecisionRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"No Evidence Corp {unique_suffix}",
                    normalized_name=f"no-evidence-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"NO_EVID_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            # Role with fresh posted_at, target title, junior experience, and target location,
            # but NO external verification evidence provided (verification_evidence=None).
            raw_payload = {
                "title": "Junior Python Developer",
                "description": SAMPLE_JD_TEXT,
                "location": "Bengaluru, Karnataka",
                "experience_raw": "0-2 years",
                "salary_min": 1000000,
                "salary_max": 1500000,
                "salary_currency": "INR",
                "salary_period": "ANNUAL",
                "posted_at": base_posted_at.isoformat(),
                "application_url": app_url,
            }
            raw_body = json.dumps(raw_payload).encode("utf-8")

            envelope = JobIngestionEnvelope(
                raw_body=raw_body,
                raw_payload=raw_payload,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_url=app_url,
                payload_format="JSON",
                verification_evidence=None,  # EXPLICITLY OMITTED EVIDENCE
            )

            service = JobIngestionService.from_session(session)
            outcome = await service.ingest(envelope)

            # Ingestion succeeds in creating records
            assert outcome.status == STATUS_PROCESSED_NEW
            assert outcome.canonical_job is not None

            # Verification evaluated posting_date_verified = False due to lack of corroboration
            verif_outcome = outcome.verification_outcome
            assert verif_outcome is not None
            assert verif_outcome.result.posting_date_verified is False
            assert "POSTED_DATE_SUSPICIOUS" in verif_outcome.result.review_reasons

            # Filter decision evaluated the job into REVIEW status because date is unverified
            decision_outcome = outcome.filter_decision_outcome
            assert decision_outcome is not None
            assert decision_outcome.decision.final_status == "REVIEW"
            assert "POSTED_DATE_SUSPICIOUS" in decision_outcome.decision.reason_codes

            # Persisted canonical Job in PostgreSQL reflects REVIEW status
            persisted_job = await job_repo.get_by_id(outcome.canonical_job.id)
            assert persisted_job is not None
            assert persisted_job.eligibility_status == "REVIEW"
            assert persisted_job.review_reason == "POSTED_DATE_SUSPICIOUS"

            # Persisted VerificationRecord reflects posting_date_verified = False
            persisted_verif = await verif_repo.get_latest_by_job_id(persisted_job.id)
            assert persisted_verif is not None
            assert persisted_verif.posting_date_verified is False
            assert "POSTED_DATE_SUSPICIOUS" in (persisted_verif.review_reasons or [])

            # Persisted FilterDecision reflects REVIEW
            persisted_decision = await decision_repo.get_latest_by_job_id(persisted_job.id)
            assert persisted_decision is not None
            assert persisted_decision.final_status == "REVIEW"
            assert "POSTED_DATE_SUSPICIOUS" in persisted_decision.reason_codes

    finally:
        pass


@pytest.mark.asyncio
async def test_repeated_observation_provenance_preservation_integration() -> None:
    """Verify repeated observation preserves both RawJobs and original provenance pointer in JobSourceReference."""
    unique_suffix = uuid.uuid4().hex[:8]
    ext_job_id = f"gh-provenance-{unique_suffix}"
    app_url = f"https://boards.greenhouse.io/acme/jobs/{ext_job_id}"
    base_posted_at = datetime.now(UTC) - timedelta(days=1)
    obs_time_1 = datetime.now(UTC) - timedelta(hours=2)
    obs_time_2 = datetime.now(UTC)

    session_factory = get_sessionmaker()

    try:
        async with session_factory() as session:
            company_repo = CompanyRepository(session)
            source_repo = SourceRepository(session)
            cs_repo = CompanySourceRepository(session)
            raw_repo = RawJobRepository(session)
            job_repo = JobRepository(session)
            jsr_repo = JobSourceReferenceRepository(session)
            verif_repo = VerificationRecordRepository(session)
            decision_repo = FilterDecisionRepository(session)

            company = await company_repo.create(
                Company(
                    id=uuid.uuid4(),
                    name=f"Provenance Corp {unique_suffix}",
                    normalized_name=f"provenance-corp-{unique_suffix}",
                )
            )
            source = await source_repo.create(
                Source(
                    id=uuid.uuid4(),
                    source_name=f"PROV_SRC_{unique_suffix.upper()}",
                    source_type="ATS_API",
                )
            )
            cs = await cs_repo.create(
                CompanySource(
                    id=uuid.uuid4(),
                    company_id=company.id,
                    source_id=source.id,
                    ats_identifier=f"token-{unique_suffix}",
                    discovery_method="AUTOMATED_PROBE",
                )
            )

            payload_1 = {
                "title": "Fullstack AI Engineer",
                "description": SAMPLE_JD_TEXT,
                "location": "Mumbai, Maharashtra",
                "posted_at": base_posted_at.isoformat(),
            }
            body_1 = json.dumps(payload_1).encode("utf-8")

            envelope_1 = JobIngestionEnvelope(
                raw_body=body_1,
                raw_payload=payload_1,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_url=app_url,
                payload_format="JSON",
                collected_at=obs_time_1,
            )

            service = JobIngestionService.from_session(session)

            # Observation 1: Initial Discovery
            outcome_1 = await service.ingest(envelope_1)
            assert outcome_1.status == STATUS_PROCESSED_NEW
            raw_1_id = outcome_1.raw_job.id
            canonical_job_id = outcome_1.canonical_job.id

            ref_1 = await jsr_repo.get_by_source_external_id(source.id, ext_job_id)
            assert ref_1 is not None
            assert ref_1.raw_job_id == raw_1_id
            assert ref_1.job_id == canonical_job_id

            # Observation 2: Repeated observation later with slightly updated payload text
            payload_2 = {
                "title": "Fullstack AI Engineer",
                "description": SAMPLE_JD_TEXT + " Added notes in observation 2.",
                "location": "Mumbai, Maharashtra",
                "posted_at": base_posted_at.isoformat(),
            }
            body_2 = json.dumps(payload_2).encode("utf-8")

            envelope_2 = JobIngestionEnvelope(
                raw_body=body_2,
                raw_payload=payload_2,
                source_id=source.id,
                company_source_id=cs.id,
                company_id=company.id,
                company_name=company.name,
                source_job_id=ext_job_id,
                source_url=app_url,
                payload_format="JSON",
                collected_at=obs_time_2,
            )

            outcome_2 = await service.ingest(envelope_2)
            assert outcome_2.status == STATUS_PROCESSED_MATCH
            raw_2_id = outcome_2.raw_job.id
            assert raw_2_id != raw_1_id

            # 1. Verify both RawJobs are persisted and retrievable
            persisted_raw_1 = await raw_repo.get_by_id(raw_1_id)
            persisted_raw_2 = await raw_repo.get_by_id(raw_2_id)
            assert persisted_raw_1 is not None
            assert persisted_raw_2 is not None
            assert bytes(persisted_raw_1.raw_body) == body_1
            assert bytes(persisted_raw_2.raw_body) == body_2

            # 2. Verify JobSourceReference preserved pointer to raw_1_id (primary discovery)
            ref_2 = await jsr_repo.get_by_source_external_id(source.id, ext_job_id)
            assert ref_2 is not None
            assert ref_2.id == ref_1.id
            assert ref_2.raw_job_id == raw_1_id  # Initial discovery raw pointer preserved
            assert ref_2.job_id == canonical_job_id
            assert ref_2.collected_at >= obs_time_2  # Timestamp refreshed to latest observation

            # 3. Verify exactly 1 canonical Job exists for this company
            company_jobs = await job_repo.list_by_company_id(company.id)
            assert len(company_jobs) == 1
            assert company_jobs[0].id == canonical_job_id

            # 4. Verify append-only verification and decision histories
            verif_history = await verif_repo.list_by_job_id(canonical_job_id)
            assert len(verif_history) == 2
            decision_history = await decision_repo.list_by_job_id(canonical_job_id)
            assert len(decision_history) == 2

            # 5. Verify the full provenance join path:
            # raw_jobs.company_source_id -> company_sources.id -> company_sources.source_id
            # matched with job_source_references (source_id, source_job_id) -> jobs.id
            # Confirms that repeated observations trace to the canonical job without overwriting raw evidence.
            for raw_obs_id in (raw_1_id, raw_2_id):
                provenance_stmt = (
                    select(Job.id)
                    .select_from(RawJob)
                    .join(CompanySource, RawJob.company_source_id == CompanySource.id)
                    .join(
                        JobSourceReference,
                        (JobSourceReference.source_id == CompanySource.source_id)
                        & (JobSourceReference.source_job_id == RawJob.source_job_id),
                    )
                    .join(Job, JobSourceReference.job_id == Job.id)
                    .where(RawJob.id == raw_obs_id)
                )
                traced_job_id = (await session.execute(provenance_stmt)).scalar_one_or_none()
                assert traced_job_id == canonical_job_id

    finally:
        pass
