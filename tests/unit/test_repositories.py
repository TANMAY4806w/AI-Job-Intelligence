"""Unit tests for asynchronous persistence repositories.

Verifies repository query construction, session interaction, and transaction policies
using an isolated mocked AsyncSession without requiring a live PostgreSQL instance.
"""

import hashlib
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.company import Company
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.persistence.repositories.source_repository import SourceRepository


@pytest.fixture
def mock_session() -> AsyncMock:
    """Fixture providing a mock AsyncSession."""
    session = AsyncMock(spec=AsyncSession)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_company_repository_crud_and_transaction_policy(mock_session: AsyncMock) -> None:
    """Verify CompanyRepository queries, flush behavior, and zero commit calls."""
    repo = CompanyRepository(mock_session)
    company_id = uuid.uuid4()
    mock_company = Company(
        id=company_id,
        name="Acme Corporation",
        normalized_name="acme-corporation",
    )

    # Test get_by_id
    mock_session.get.return_value = mock_company
    retrieved = await repo.get_by_id(company_id)
    assert retrieved is mock_company
    mock_session.get.assert_awaited_once_with(Company, company_id)

    # Test get_by_normalized_name
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_company
    mock_session.execute.return_value = mock_result

    by_name = await repo.get_by_normalized_name("acme-corporation")
    assert by_name is mock_company
    assert mock_session.execute.call_count == 1

    # Test create
    new_company = Company(name="Beta Inc", normalized_name="beta-inc")
    created = await repo.create(new_company)
    assert created is new_company
    mock_session.add.assert_called_once_with(new_company)
    mock_session.flush.assert_awaited_once()

    # Verify commit is never called by the repository
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_source_repository_crud_and_transaction_policy(mock_session: AsyncMock) -> None:
    """Verify SourceRepository queries and transaction policy."""
    repo = SourceRepository(mock_session)
    source_id = uuid.uuid4()
    mock_source = Source(
        id=source_id,
        source_name="GREENHOUSE",
        source_type="ATS_API",
        is_active=True,
    )

    # Test get_by_id
    mock_session.get.return_value = mock_source
    retrieved = await repo.get_by_id(source_id)
    assert retrieved is mock_source
    mock_session.get.assert_awaited_once_with(Source, source_id)

    # Test get_by_name
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_source
    mock_session.execute.return_value = mock_result

    by_name = await repo.get_by_name("GREENHOUSE")
    assert by_name is mock_source

    # Test create
    new_source = Source(source_name="ASHBY", source_type="ATS_API")
    created = await repo.create(new_source)
    assert created is new_source
    mock_session.add.assert_called_once_with(new_source)
    mock_session.flush.assert_awaited_once()

    # Commit must not be called
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_company_source_repository_locators(mock_session: AsyncMock) -> None:
    """Verify CompanySourceRepository locator queries matching partial indexes."""
    repo = CompanySourceRepository(mock_session)
    company_id = uuid.uuid4()
    source_id = uuid.uuid4()

    mock_cs = CompanySource(
        id=uuid.uuid4(),
        company_id=company_id,
        source_id=source_id,
        ats_identifier="acme-board",
        discovery_method="AUTOMATED_PROBE",
    )
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_cs
    mock_session.execute.return_value = mock_result

    # 1. Locator with ats_identifier
    cs_ats = await repo.get_by_locator(company_id, source_id, ats_identifier="acme-board")
    assert cs_ats is mock_cs

    # 2. Locator with career_url
    cs_url = await repo.get_by_locator(company_id, source_id, career_url="https://acme.com/careers")
    assert cs_url is mock_cs

    # 3. Locator with api_endpoint
    cs_api = await repo.get_by_locator(
        company_id, source_id, api_endpoint="https://api.acme.com/jobs"
    )
    assert cs_api is mock_cs

    # 4. Bare locator inputs must raise ValueError (None inputs)
    with pytest.raises(ValueError, match="At least one locator"):
        await repo.get_by_locator(company_id, source_id)

    # 5. Whitespace-only locator inputs must raise ValueError
    with pytest.raises(ValueError, match="At least one locator"):
        await repo.get_by_locator(
            company_id, source_id, ats_identifier="   ", career_url="", api_endpoint="  "
        )

    # 6. Locator with whitespace normalization falling through to valid career_url
    cs_ws = await repo.get_by_locator(
        company_id, source_id, ats_identifier="   ", career_url="https://acme.com/careers"
    )
    assert cs_ws is mock_cs

    # 7. Priority resolution with conflicting/multiple locators (ATS identifier takes precedence over career_url)
    cs_multi1 = await repo.get_by_locator(
        company_id,
        source_id,
        ats_identifier="acme-board",
        career_url="https://acme.com/careers",
    )
    assert cs_multi1 is mock_cs

    # 8. Priority resolution with conflicting/multiple locators (career_url takes precedence over api_endpoint)
    cs_multi2 = await repo.get_by_locator(
        company_id,
        source_id,
        career_url="https://acme.com/careers",
        api_endpoint="https://api.acme.com/jobs",
    )
    assert cs_multi2 is mock_cs

    # Test create
    created = await repo.create(mock_cs)
    assert created is mock_cs
    mock_session.add.assert_called_once_with(mock_cs)
    mock_session.flush.assert_awaited_once()

    # Commit must not be called
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_company_source_repository_ats_identifier_ambiguity(mock_session: AsyncMock) -> None:
    """Verify get_by_ats_identifier detects ambiguity and list_by_ats_identifier returns all."""
    repo = CompanySourceRepository(mock_session)
    source_id = uuid.uuid4()

    cs1 = CompanySource(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        source_id=source_id,
        ats_identifier="shared-token",
        discovery_method="AUTOMATED_PROBE",
    )
    cs2 = CompanySource(
        id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        source_id=source_id,
        ats_identifier="shared-token",
        discovery_method="MANUAL_INPUT",
    )

    # Scenario A: Exactly one mapping found -> returns single CompanySource
    mock_result_single = MagicMock()
    mock_scalars_single = MagicMock()
    mock_scalars_single.all.return_value = [cs1]
    mock_result_single.scalars.return_value = mock_scalars_single
    mock_session.execute.return_value = mock_result_single

    single_result = await repo.get_by_ats_identifier(source_id, "shared-token")
    assert single_result is cs1

    # Scenario B: Multiple mappings found -> explicitly detects ambiguity and raises ValueError
    mock_result_ambiguous = MagicMock()
    mock_scalars_ambiguous = MagicMock()
    mock_scalars_ambiguous.all.return_value = [cs1, cs2]
    mock_result_ambiguous.scalars.return_value = mock_scalars_ambiguous
    mock_session.execute.return_value = mock_result_ambiguous

    with pytest.raises(
        ValueError, match="Ambiguous ATS identifier: multiple company sources found"
    ):
        await repo.get_by_ats_identifier(source_id, "shared-token")

    # Scenario C: Blank token returns None
    assert await repo.get_by_ats_identifier(source_id, "   ") is None

    # Scenario D: list_by_ats_identifier returns all matching mappings
    mock_result_list = MagicMock()
    mock_scalars_list = MagicMock()
    mock_scalars_list.all.return_value = [cs1, cs2]
    mock_result_list.scalars.return_value = mock_scalars_list
    mock_session.execute.return_value = mock_result_list

    all_matches = await repo.list_by_ats_identifier(source_id, "shared-token")
    assert len(all_matches) == 2
    assert all_matches == [cs1, cs2]


@pytest.mark.asyncio
async def test_raw_job_repository_append_only_and_payload_immutability(
    mock_session: AsyncMock,
) -> None:
    """Verify RawJobRepository append-only semantics and immutability invariants."""
    repo = RawJobRepository(mock_session)

    # Verify append-only invariant: no update or delete methods exist
    assert not hasattr(repo, "update")
    assert not hasattr(repo, "delete")
    assert not hasattr(repo, "delete_by_id")
    assert not hasattr(repo, "upsert")

    raw_job_id = uuid.uuid4()
    company_source_id = uuid.uuid4()
    original_bytes = b'{"title": "Software Engineer", "id": "gh_123", "dept": "Core"}'
    original_payload = {"title": "Software Engineer", "id": "gh_123", "dept": "Core"}
    content_hash = hashlib.sha256(original_bytes).hexdigest()

    raw_job = RawJob(
        id=raw_job_id,
        company_source_id=company_source_id,
        source_job_id="gh_123",
        source_job_id_type="NATIVE",
        source_url="https://boards.greenhouse.io/acme/jobs/gh_123",
        raw_body=original_bytes,
        raw_payload=original_payload,
        content_hash=content_hash,
    )

    # Test create
    saved = await repo.create(raw_job)
    assert saved is raw_job
    # Confirm raw_body and raw_payload were not mutated
    assert saved.raw_body == original_bytes
    assert saved.raw_payload == original_payload
    mock_session.add.assert_called_once_with(raw_job)
    mock_session.flush.assert_awaited_once()

    # Test get_latest_by_company_source_and_external_id
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = raw_job
    mock_session.execute.return_value = mock_result

    latest = await repo.get_latest_by_company_source_and_external_id(company_source_id, "gh_123")
    assert latest is raw_job

    # Test get_by_content_hash
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [raw_job]
    mock_result.scalars.return_value = mock_scalars

    by_hash = await repo.get_by_content_hash(content_hash)
    assert by_hash == [raw_job]

    # Commit must not be called
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_raw_job_repository_hash_mismatch_and_type_checks(
    mock_session: AsyncMock,
) -> None:
    """Verify that RawJobRepository rejects content_hash mismatches and invalid types."""
    repo = RawJobRepository(mock_session)

    raw_body = b'{"hello": "world"}'
    valid_hash = hashlib.sha256(raw_body).hexdigest()

    # 1. content_hash mismatch raises ValueError
    bad_hash_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_1",
        source_url="https://example.com/jobs/1",
        raw_body=raw_body,
        raw_payload={"hello": "world"},
        content_hash="bad_hash_value_that_does_not_match",
    )
    with pytest.raises(ValueError, match="content_hash mismatch"):
        await repo.create(bad_hash_job)

    # 2. Invalid raw_body type raises TypeError
    bad_body_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_2",
        source_url="https://example.com/jobs/2",
        raw_body="not_bytes",  # type: ignore[arg-type]
        raw_payload={"hello": "world"},
        content_hash=valid_hash,
    )
    with pytest.raises(TypeError, match="raw_job.raw_body must be bytes"):
        await repo.create(bad_body_job)

    # 3. Invalid raw_payload type raises TypeError
    bad_payload_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_3",
        source_url="https://example.com/jobs/3",
        raw_body=raw_body,
        raw_payload="not_a_dict",  # type: ignore[arg-type]
        content_hash=valid_hash,
    )
    with pytest.raises(TypeError, match="raw_job.raw_payload must be a dict"):
        await repo.create(bad_payload_job)


@pytest.mark.asyncio
async def test_raw_job_repository_payload_consistency_and_validation(
    mock_session: AsyncMock,
) -> None:
    """Verify that RawJobRepository validates wire bytes against parsed JSON payload."""
    repo = RawJobRepository(mock_session)

    # 1. Payload content mismatch between raw_body and raw_payload
    body_mismatch = b'{"title": "Backend Engineer", "dept": "AI"}'
    hash_mismatch = hashlib.sha256(body_mismatch).hexdigest()
    mismatch_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_mismatch",
        source_url="https://example.com/jobs/mismatch",
        raw_body=body_mismatch,
        raw_payload={"title": "Frontend Engineer", "dept": "Design"},  # Mismatched payload
        content_hash=hash_mismatch,
    )
    with pytest.raises(
        ValueError, match="raw_job.raw_payload does not match parsed JSON from raw_body"
    ):
        await repo.create(mismatch_job)

    # 2. Invalid JSON syntax in raw_body
    body_invalid_json = b'{"title": "Unclosed string...}'
    hash_invalid_json = hashlib.sha256(body_invalid_json).hexdigest()
    invalid_json_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_invalid_json",
        source_url="https://example.com/jobs/invalid_json",
        raw_body=body_invalid_json,
        raw_payload={"title": "Unclosed string..."},
        content_hash=hash_invalid_json,
    )
    with pytest.raises(ValueError, match="raw_body does not contain valid JSON"):
        await repo.create(invalid_json_job)

    # 3. Non-object JSON in raw_body (e.g. JSON array)
    body_array_json = b'["job1", "job2"]'
    hash_array_json = hashlib.sha256(body_array_json).hexdigest()
    array_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_array",
        source_url="https://example.com/jobs/array",
        raw_body=body_array_json,
        raw_payload={"0": "job1"},
        content_hash=hash_array_json,
    )
    with pytest.raises(ValueError, match="must decode to a JSON object"):
        await repo.create(array_job)

    # 4. Invalid UTF-8 bytes in raw_body
    body_invalid_utf8 = b"\x80\x81\x82\xff"
    hash_invalid_utf8 = hashlib.sha256(body_invalid_utf8).hexdigest()
    invalid_utf8_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_utf8",
        source_url="https://example.com/jobs/utf8",
        raw_body=body_invalid_utf8,
        raw_payload={},
        content_hash=hash_invalid_utf8,
    )
    with pytest.raises(ValueError, match="raw_body contains invalid UTF-8 bytes"):
        await repo.create(invalid_utf8_job)

    # 5. Formatted JSON with whitespace/newlines roundtrip preservation
    formatted_body = b'{\n  "title": "Senior AI Engineer",\n  "status": "OPEN"\n}\n'
    formatted_hash = hashlib.sha256(formatted_body).hexdigest()
    semantic_payload = {"title": "Senior AI Engineer", "status": "OPEN"}
    valid_formatted_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_formatted",
        source_url="https://example.com/jobs/formatted",
        payload_format="JSON",
        raw_body=formatted_body,
        raw_payload=semantic_payload,
        content_hash=formatted_hash,
    )
    saved = await repo.create(valid_formatted_job)
    assert saved is valid_formatted_job
    assert saved.payload_format == "JSON"
    assert saved.raw_body == formatted_body  # Exact wire bytes preserved
    assert saved.raw_payload == semantic_payload  # Parsed dict preserved

    # 6. HTML response acceptance with structured JSON payload
    html_wire_bytes = (
        b"<!DOCTYPE html><html><body><h1>Lead Python Engineer</h1><p>Bangalore</p></body></html>"
    )
    html_hash = hashlib.sha256(html_wire_bytes).hexdigest()
    html_extracted_payload = {
        "title": "Lead Python Engineer",
        "location": "Bangalore",
        "description": "Lead Python Engineer Bangalore",
        "extracted_from": "html_career_page",
    }
    valid_html_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_html_job",
        source_url="https://example.com/careers/lead-python",
        payload_format="HTML",
        raw_body=html_wire_bytes,
        raw_payload=html_extracted_payload,
        content_hash=html_hash,
    )
    saved_html = await repo.create(valid_html_job)
    assert saved_html is valid_html_job
    assert saved_html.payload_format == "HTML"
    assert saved_html.raw_body == html_wire_bytes
    assert saved_html.raw_payload == html_extracted_payload

    # 7. HTML response rejection on content_hash mismatch
    bad_hash_html_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_html_bad_hash",
        source_url="https://example.com/careers/lead-python-2",
        payload_format="HTML",
        raw_body=html_wire_bytes,
        raw_payload=html_extracted_payload,
        content_hash="mismatched_html_content_hash",
    )
    with pytest.raises(ValueError, match="content_hash mismatch"):
        await repo.create(bad_hash_html_job)

    # 8. Rejection of unsupported payload_format
    unsupported_format_job = RawJob(
        company_source_id=uuid.uuid4(),
        source_job_id="test_xml",
        source_url="https://example.com/jobs/xml",
        payload_format="XML",
        raw_body=b"<job><title>XML Job</title></job>",
        raw_payload={"title": "XML Job"},
        content_hash=hashlib.sha256(b"<job><title>XML Job</title></job>").hexdigest(),
    )
    with pytest.raises(ValueError, match="Unsupported payload_format 'XML'"):
        await repo.create(unsupported_format_job)


@pytest.mark.asyncio
async def test_job_repository_deduplication_queries(mock_session: AsyncMock) -> None:
    """Verify JobRepository lookups for Stage 1, 2, and 3 deduplication."""
    repo = JobRepository(mock_session)
    company_id = uuid.uuid4()
    source_id = uuid.uuid4()

    mock_job = Job(
        id=uuid.uuid4(),
        company_id=company_id,
        company_name="Acme Corp",
        title="Senior Python Engineer",
        normalized_title="senior software engineer",
        location="Bengaluru, India",
        normalized_location="bengaluru",
        description="Build scalable services.",
        role_priority="P1",
        relevance_status="RELEVANT",
        eligibility_status="NEW",
        source_verified=True,
        jd_verified=True,
        company_verified=True,
        location_verified=True,
        experience_verified=True,
        salary_verified=False,
        verification_confidence=Decimal("0.95"),
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_job
    mock_session.execute.return_value = mock_result

    # Stage 1: Get by source reference
    stage1 = await repo.get_by_source_reference(source_id, "ext_job_999")
    assert stage1 is mock_job

    # Stage 2: Get by application URL
    stage2 = await repo.get_by_application_url("https://boards.greenhouse.io/acme/jobs/ext_job_999")
    assert stage2 is mock_job

    # Stage 2: Find all by application URL
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_job]
    mock_result.scalars.return_value = mock_scalars
    stage2_all = await repo.find_all_by_application_url(
        "https://boards.greenhouse.io/acme/jobs/ext_job_999"
    )
    assert stage2_all == [mock_job]

    # Stage 3: Find by composite attributes
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_job]
    mock_result.scalars.return_value = mock_scalars

    stage3 = await repo.find_by_composite_attributes(
        company_id,
        normalized_title="senior software engineer",
        normalized_location="bengaluru",
    )
    assert stage3 == [mock_job]

    # Test create
    created = await repo.create(mock_job)
    assert created is mock_job
    mock_session.add.assert_called_once_with(mock_job)
    mock_session.flush.assert_awaited_once()

    # Commit must not be called
    assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_job_source_reference_repository_operations(mock_session: AsyncMock) -> None:
    """Verify JobSourceReferenceRepository provenance link queries."""
    repo = JobSourceReferenceRepository(mock_session)
    job_id = uuid.uuid4()
    raw_job_id = uuid.uuid4()
    source_id = uuid.uuid4()

    mock_ref = JobSourceReference(
        id=uuid.uuid4(),
        job_id=job_id,
        raw_job_id=raw_job_id,
        source_id=source_id,
        source_job_id="job_ext_55",
        source_job_id_type="NATIVE",
        source_url="https://boards.greenhouse.io/acme/jobs/job_ext_55",
        is_primary=True,
    )

    # Test get_by_source_external_id
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_ref
    mock_session.execute.return_value = mock_result

    by_ext = await repo.get_by_source_external_id(source_id, "job_ext_55")
    assert by_ext is mock_ref

    # Test list_by_job_id
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_ref]
    mock_result.scalars.return_value = mock_scalars

    refs = await repo.list_by_job_id(job_id)
    assert refs == [mock_ref]

    # Test list_by_raw_job_id
    raw_refs = await repo.list_by_raw_job_id(raw_job_id)
    assert raw_refs == [mock_ref]

    # Test create
    created = await repo.create(mock_ref)
    assert created is mock_ref
    mock_session.add.assert_called_once_with(mock_ref)
    mock_session.flush.assert_awaited_once()

    # Commit must not be called
    assert mock_session.commit.call_count == 0
