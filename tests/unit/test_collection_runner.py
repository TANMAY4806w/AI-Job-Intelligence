"""Unit tests for CollectorRegistry, CollectionRunRepository, and CollectionRunner.

Boundary: 100% isolated in-memory unit tests with zero database or external network I/O.
Validates:
1. Greenhouse registry lookup and case insensitivity.
2. Unsupported and unregistered source handling with UnsupportedSourceError.
3. Active versus inactive source mappings.
4. Successful collection and metric aggregation.
5. Empty boards versus failed requests.
6. Partial success across jobs.
7. Failure reporting and mapping-level error isolation across multiple mappings.
8. Deterministic aggregate counts.
9. No repository or runner calls to session.commit().
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    BaseCollector,
    CollectorResult,
    JobCollectionFailure,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import GreenhouseCollector
from app.collectors.registry import (
    CollectorRegistry,
    UnsupportedSourceError,
    default_collector_registry,
)
from app.orchestration.collection_runner import (
    STATUS_SKIPPED_INACTIVE,
    STATUS_UNSUPPORTED_SOURCE,
    CollectionRunner,
    CollectionRunnerResult,
)
from app.orchestration.ingestion_service import JobIngestionService
from app.persistence.models.company import Company
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource, Source
from app.persistence.repositories.collection_run_repository import CollectionRunRepository
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.source_repository import SourceRepository

# ==============================================================================
# 1. CollectorRegistry Unit Tests
# ==============================================================================


class TestCollectorRegistry:
    """Isolated unit tests for CollectorRegistry."""

    def test_default_registry_has_greenhouse(self) -> None:
        """Verify default registry pre-populates Greenhouse collector."""
        registry = CollectorRegistry()
        assert registry.is_supported("GREENHOUSE")
        assert registry.is_supported("greenhouse")
        assert registry.is_supported("  Greenhouse  ")
        assert registry.get("GREENHOUSE") is GreenhouseCollector
        assert registry.get("greenhouse") is GreenhouseCollector
        assert "GREENHOUSE" in registry.list_supported_sources()

    def test_global_default_registry_instance(self) -> None:
        """Verify the module-level default_collector_registry is available."""
        assert default_collector_registry.is_supported("GREENHOUSE")
        assert default_collector_registry.get("GREENHOUSE") is GreenhouseCollector

    def test_unsupported_source_lookup(self) -> None:
        """Verify unregistered source platforms return None and is_supported is False."""
        registry = CollectorRegistry()
        assert not registry.is_supported("LEVER")
        assert not registry.is_supported("ASHBY")
        assert not registry.is_supported("WORKABLE")
        assert registry.get("LEVER") is None
        assert registry.get("UNKNOWN_SOURCE") is None

    def test_register_and_unregister_custom_collector(self) -> None:
        """Verify dynamic registration and unregistration of custom BaseCollector subclasses."""
        registry = CollectorRegistry()

        class DummyCollector(BaseCollector):
            async def collect_company(
                self, company_source, company=None, *, collection_run_id=None
            ):
                pass

        registry.register("DUMMY_ATS", DummyCollector)
        assert registry.is_supported("DUMMY_ATS")
        assert registry.is_supported("dummy_ats")
        assert registry.get("DUMMY_ATS") is DummyCollector
        assert "DUMMY_ATS" in registry.list_supported_sources()

        # Unregister
        assert registry.unregister("dummy_ats") is True
        assert not registry.is_supported("DUMMY_ATS")
        assert registry.unregister("dummy_ats") is False

    def test_register_invalid_class_raises_value_error(self) -> None:
        """Verify register() rejects classes that do not inherit from BaseCollector."""
        registry = CollectorRegistry()
        with pytest.raises(ValueError, match="must be a subclass of BaseCollector"):
            registry.register("INVALID", object)  # type: ignore[arg-type]

    def test_register_empty_identifier_raises_value_error(self) -> None:
        """Verify register() rejects empty or whitespace-only source identifiers."""
        registry = CollectorRegistry()

        class DummyCollector(BaseCollector):
            async def collect_company(
                self, company_source, company=None, *, collection_run_id=None
            ):
                pass

        with pytest.raises(ValueError, match="must be a non-empty string"):
            registry.register("", DummyCollector)

        with pytest.raises(ValueError, match="cannot be blank"):
            registry.register("   ", DummyCollector)

    def test_resolve_supported_source_instantiates_collector(self) -> None:
        """Verify resolve() correctly instantiates collector for Source instance and string name."""
        registry = CollectorRegistry()
        mock_ingestion = MagicMock(spec=JobIngestionService)
        mock_http = MagicMock(spec=PoliteHttpClient)

        source = Source(
            id=uuid.uuid4(),
            source_name="greenhouse",
            source_type="ATS_API",
        )

        # Resolve from Source model
        collector = registry.resolve(source, mock_ingestion, http_client=mock_http)
        assert isinstance(collector, GreenhouseCollector)
        assert collector.ingestion_service is mock_ingestion
        assert collector.http_client is mock_http

        # Resolve from string identifier
        collector2 = registry.resolve("GREENHOUSE", mock_ingestion)
        assert isinstance(collector2, GreenhouseCollector)

    def test_resolve_unsupported_source_raises_unsupported_source_error(self) -> None:
        """Verify resolve() raises UnsupportedSourceError when platform is not registered."""
        registry = CollectorRegistry()
        mock_ingestion = MagicMock(spec=JobIngestionService)

        source = Source(
            id=uuid.uuid4(),
            source_name="WORKABLE",
            source_type="ATS_API",
        )

        with pytest.raises(UnsupportedSourceError) as exc_info:
            registry.resolve(source, mock_ingestion)

        assert "Unsupported or unregistered source platform: 'WORKABLE'" in str(exc_info.value)
        assert exc_info.value.source_identifier == "WORKABLE"


# ==============================================================================
# 2. CollectionRunRepository Unit Tests
# ==============================================================================


@pytest.mark.asyncio
class TestCollectionRunRepository:
    """Isolated unit tests for CollectionRunRepository."""

    async def test_repository_never_calls_commit(self) -> None:
        """Verify create, get, update, and list operations never call session.commit()."""
        mock_session = AsyncMock(spec=AsyncSession)
        repo = CollectionRunRepository(mock_session)

        run = CollectionRun(
            id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            company_source_id=uuid.uuid4(),
            run_type="MANUAL_TRIGGER",
            started_at=datetime.now(UTC),
            status="RUNNING",
        )

        # Create
        created = await repo.create(run)
        assert created is run
        mock_session.add.assert_called_once_with(run)
        mock_session.flush.assert_awaited_once()
        mock_session.commit.assert_not_called()

        # Update
        await repo.update(run)
        assert mock_session.flush.await_count == 2
        mock_session.commit.assert_not_called()

        # Get by ID
        mock_session.get.return_value = run
        fetched = await repo.get_by_id(run.id)
        assert fetched is run
        mock_session.commit.assert_not_called()


# ==============================================================================
# 3. CollectionRunner Unit Tests
# ==============================================================================


@pytest.fixture
def runner_context():
    """Build mock repositories, services, and entities for CollectionRunner unit testing."""
    mock_session = AsyncMock(spec=AsyncSession)

    cs_repo = AsyncMock(spec=CompanySourceRepository)
    cs_repo.session = mock_session
    s_repo = AsyncMock(spec=SourceRepository)
    s_repo.session = mock_session
    c_repo = AsyncMock(spec=CompanyRepository)
    c_repo.session = mock_session
    run_repo = AsyncMock(spec=CollectionRunRepository)
    run_repo.session = mock_session
    ingestion = MagicMock(spec=JobIngestionService)
    registry = CollectorRegistry()

    # Configure run_repo create/update to return the run object
    async def fake_create_run(run: CollectionRun) -> CollectionRun:
        if run.id is None:
            run.id = uuid.uuid4()
        return run

    async def fake_update_run(run: CollectionRun) -> CollectionRun:
        return run

    run_repo.create.side_effect = fake_create_run
    run_repo.update.side_effect = fake_update_run

    company = Company(
        id=uuid.uuid4(),
        name="Acme Robotics",
        normalized_name="acme-robotics",
    )
    source = Source(
        id=uuid.uuid4(),
        source_name="GREENHOUSE",
        source_type="ATS_API",
        is_active=True,
    )
    company_source = CompanySource(
        id=uuid.uuid4(),
        company_id=company.id,
        source_id=source.id,
        ats_identifier="acmerobotics",
        is_active=True,
    )
    # Link ORM relationships for quick access
    company_source.company = company
    company_source.source = source

    cs_repo.get_by_id.return_value = company_source
    s_repo.get_by_id.return_value = source
    c_repo.get_by_id.return_value = company

    runner = CollectionRunner(
        company_source_repository=cs_repo,
        source_repository=s_repo,
        company_repository=c_repo,
        collection_run_repository=run_repo,
        ingestion_service=ingestion,
        registry=registry,
    )

    return {
        "session": mock_session,
        "cs_repo": cs_repo,
        "s_repo": s_repo,
        "c_repo": c_repo,
        "run_repo": run_repo,
        "ingestion": ingestion,
        "registry": registry,
        "company": company,
        "source": source,
        "company_source": company_source,
        "runner": runner,
    }


@pytest.mark.asyncio
class TestCollectionRunnerExecution:
    """Isolated unit tests for CollectionRunner workflows."""

    async def test_runner_never_calls_commit(self, runner_context) -> None:
        """Verify CollectionRunner and underlying operations never invoke session.commit()."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        session = runner_context["session"]

        # Mock collector execution
        collector = AsyncMock(spec=BaseCollector)
        collector.collect_company.return_value = CollectorResult(
            company_source_id=cs.id,
            source_id=cs.source_id,
            company_id=cs.company_id,
            ats_identifier="acmerobotics",
            status=STATUS_SUCCESS,
            total_discovered=1,
            total_ingested=1,
            total_failed=0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        runner.registry.resolve = MagicMock(return_value=collector)

        outcome = await runner.run_mapping(cs)
        assert outcome.is_success
        session.commit.assert_not_called()

    async def test_successful_collection_and_metric_aggregation(self, runner_context) -> None:
        """Verify successful harvest records metrics, duplicates, HTTP counts, and updates run."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        run_repo = runner_context["run_repo"]
        cs_repo = runner_context["cs_repo"]

        # Mock ingestion outcomes with 1 new job and 1 duplicate match
        match_outcome = MagicMock()
        match_outcome.is_match = True
        match_outcome.raw_job.http_status = 200

        new_outcome = MagicMock()
        new_outcome.is_match = False
        new_outcome.raw_job.http_status = 200

        collector_result = CollectorResult(
            company_source_id=cs.id,
            source_id=cs.source_id,
            company_id=cs.company_id,
            ats_identifier="acmerobotics",
            status=STATUS_SUCCESS,
            total_discovered=2,
            total_ingested=2,
            total_failed=0,
            ingestion_outcomes=[match_outcome, new_outcome],
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.return_value = collector_result
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_SUCCESS
        assert outcome.is_success
        assert outcome.records_discovered == 2
        assert outcome.records_ingested == 2
        assert outcome.records_failed == 0
        assert outcome.duplicates_detected == 1
        # 1 board query (200) + 2 job queries (200) = 3
        assert outcome.http_2xx_count == 3
        assert outcome.http_4xx_count == 0
        assert outcome.http_5xx_count == 0
        assert cs.last_collected_at == collector_result.finished_at
        cs_repo.update.assert_awaited_once_with(cs)

        # Verify CollectionRun database entity
        assert run_repo.update.await_count == 1
        updated_run: CollectionRun = run_repo.update.call_args[0][0]
        assert updated_run.status == "COMPLETED"
        assert updated_run.records_discovered == 2
        assert updated_run.records_ingested == 2
        assert updated_run.duplicates_detected == 1
        assert updated_run.http_2xx_count == 3

    async def test_empty_board_outcome(self, runner_context) -> None:
        """Verify an empty job board returns STATUS_EMPTY and CollectionRun is COMPLETED."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        run_repo = runner_context["run_repo"]

        collector_result = CollectorResult(
            company_source_id=cs.id,
            source_id=cs.source_id,
            company_id=cs.company_id,
            ats_identifier="acmerobotics",
            status=STATUS_EMPTY,
            total_discovered=0,
            total_ingested=0,
            total_failed=0,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.return_value = collector_result
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_EMPTY
        assert outcome.is_empty
        assert not outcome.is_failed
        assert outcome.records_discovered == 0
        assert outcome.records_ingested == 0
        # 1 board query returned 200 with 0 jobs
        assert outcome.http_2xx_count == 1

        updated_run: CollectionRun = run_repo.update.call_args[0][0]
        assert updated_run.status == "COMPLETED"
        assert updated_run.telemetry["collector_status"] == STATUS_EMPTY

    async def test_failed_request_outcome_is_distinguished_from_empty(self, runner_context) -> None:
        """Verify failed HTTP request is not confused with an empty board and marks run FAILED."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        run_repo = runner_context["run_repo"]

        collector_result = CollectorResult(
            company_source_id=cs.id,
            source_id=cs.source_id,
            company_id=cs.company_id,
            ats_identifier="acmerobotics",
            status=STATUS_FAILED,
            total_discovered=0,
            total_ingested=0,
            total_failed=0,
            error_message="Greenhouse board 'acmerobotics' returned HTTP 404",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.return_value = collector_result
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_FAILED
        assert outcome.is_failed
        assert not outcome.is_empty
        assert outcome.error_message == "Greenhouse board 'acmerobotics' returned HTTP 404"
        assert outcome.http_4xx_count == 1

        updated_run: CollectionRun = run_repo.update.call_args[0][0]
        assert updated_run.status == "FAILED"
        assert updated_run.http_4xx_count == 1
        assert updated_run.telemetry["error_message"] == collector_result.error_message

    async def test_partial_success_across_jobs(self, runner_context) -> None:
        """Verify partial collection success preserves ingested jobs and diagnostic failures."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        run_repo = runner_context["run_repo"]

        ingested_job = MagicMock()
        ingested_job.is_match = False
        ingested_job.raw_job.http_status = 200

        failure = JobCollectionFailure(
            source_job_id="999",
            source_url="https://boards-api.greenhouse.io/v1/boards/acme/jobs/999",
            http_status=404,
            error_type="HTTP_ERROR",
            error_message="Job detail returned HTTP 404",
        )

        collector_result = CollectorResult(
            company_source_id=cs.id,
            source_id=cs.source_id,
            company_id=cs.company_id,
            ats_identifier="acmerobotics",
            status=STATUS_PARTIAL_SUCCESS,
            total_discovered=2,
            total_ingested=1,
            total_failed=1,
            ingestion_outcomes=[ingested_job],
            failures=[failure],
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.return_value = collector_result
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_PARTIAL_SUCCESS
        assert outcome.is_partial
        assert outcome.records_discovered == 2
        assert outcome.records_ingested == 1
        assert outcome.records_failed == 1
        assert outcome.http_2xx_count == 2  # 1 board + 1 job
        assert outcome.http_4xx_count == 1  # 1 failed job 404

        updated_run: CollectionRun = run_repo.update.call_args[0][0]
        assert updated_run.status == "COMPLETED"
        assert updated_run.records_ingested == 1
        assert updated_run.records_failed == 1
        assert updated_run.telemetry["failures_count"] == 1

    async def test_inactive_company_source_is_skipped(self, runner_context) -> None:
        """Verify inactive CompanySource mapping is skipped without running collection."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        cs.is_active = False

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_SKIPPED_INACTIVE
        assert outcome.is_skipped
        assert outcome.records_discovered == 0
        assert outcome.records_ingested == 0
        assert "inactive" in outcome.error_message.lower()
        # No CollectionRun record should be created for skipped inactive mappings
        runner_context["run_repo"].create.assert_not_called()

    async def test_inactive_source_platform_is_skipped(self, runner_context) -> None:
        """Verify mapping is skipped when its parent Source platform is inactive."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        source = runner_context["source"]
        source.is_active = False

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_SKIPPED_INACTIVE
        assert outcome.is_skipped
        assert "inactive" in outcome.error_message.lower()
        runner_context["run_repo"].create.assert_not_called()

    async def test_unsupported_source_records_failure_and_telemetry(self, runner_context) -> None:
        """Verify unsupported source platform never silently appears as 0 jobs."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        source = runner_context["source"]
        source.source_name = "WORKABLE"  # Not registered in V1
        run_repo = runner_context["run_repo"]

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_UNSUPPORTED_SOURCE
        assert outcome.is_failed
        assert outcome.collection_run_id is not None
        assert "Unsupported or unregistered source platform: 'WORKABLE'" in outcome.error_message

        # CollectionRun must be persisted with status FAILED and diagnostic telemetry
        run_repo.create.assert_awaited_once()
        created_run: CollectionRun = run_repo.create.call_args[0][0]
        assert created_run.status == "FAILED"
        assert created_run.telemetry["error_type"] == "UNSUPPORTED_SOURCE"
        assert created_run.telemetry["source_name"] == "WORKABLE"

    async def test_missing_ats_identifier_fails(self, runner_context) -> None:
        """Verify CompanySource without non-blank ats_identifier fails before collection."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        cs.ats_identifier = ""

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_FAILED
        assert outcome.is_failed
        assert "missing required non-blank 'ats_identifier'" in outcome.error_message
        runner_context["run_repo"].create.assert_not_called()

    async def test_multiple_mappings_isolation_and_aggregation(self, runner_context) -> None:
        """Verify run_mappings isolates individual failures and computes deterministic totals."""
        runner = runner_context["runner"]
        cs_repo = runner_context["cs_repo"]
        company = runner_context["company"]

        # Mapping 1: Valid Greenhouse board -> SUCCESS (2 ingested)
        source_gh = Source(
            id=uuid.uuid4(), source_name="GREENHOUSE", source_type="ATS_API", is_active=True
        )
        cs1 = CompanySource(
            id=uuid.uuid4(),
            company_id=company.id,
            source_id=source_gh.id,
            ats_identifier="gh-valid",
            is_active=True,
        )
        cs1.company = company
        cs1.source = source_gh

        # Mapping 2: Valid Greenhouse board -> EMPTY (0 jobs)
        cs2 = CompanySource(
            id=uuid.uuid4(),
            company_id=company.id,
            source_id=source_gh.id,
            ats_identifier="gh-empty",
            is_active=True,
        )
        cs2.company = company
        cs2.source = source_gh

        # Mapping 3: Valid Greenhouse board -> FAILED (HTTP 500)
        cs3 = CompanySource(
            id=uuid.uuid4(),
            company_id=company.id,
            source_id=source_gh.id,
            ats_identifier="gh-broken",
            is_active=True,
        )
        cs3.company = company
        cs3.source = source_gh

        # Mapping 4: Inactive mapping -> SKIPPED_INACTIVE
        cs4 = CompanySource(
            id=uuid.uuid4(),
            company_id=company.id,
            source_id=source_gh.id,
            ats_identifier="gh-inactive",
            is_active=False,
        )
        cs4.company = company
        cs4.source = source_gh

        # Mapping 5: Unsupported source platform -> UNSUPPORTED_SOURCE
        source_unsupported = Source(
            id=uuid.uuid4(), source_name="LEVER", source_type="ATS_API", is_active=True
        )
        cs5 = CompanySource(
            id=uuid.uuid4(),
            company_id=company.id,
            source_id=source_unsupported.id,
            ats_identifier="lever-board",
            is_active=True,
        )
        cs5.company = company
        cs5.source = source_unsupported

        mapping_lookup = {cs.id: cs for cs in (cs1, cs2, cs3, cs4, cs5)}
        cs_repo.get_by_id.side_effect = lambda cid: mapping_lookup.get(cid)

        # Mock collector responses
        collector = AsyncMock(spec=BaseCollector)

        async def fake_collect(company_source, company=None, *, collection_run_id=None):
            token = company_source.ats_identifier
            if token == "gh-valid":
                job_mock = MagicMock()
                job_mock.is_match = False
                job_mock.raw_job.http_status = 200
                return CollectorResult(
                    company_source_id=company_source.id,
                    source_id=company_source.source_id,
                    company_id=company.id,
                    ats_identifier=token,
                    status=STATUS_SUCCESS,
                    total_discovered=2,
                    total_ingested=2,
                    total_failed=0,
                    ingestion_outcomes=[job_mock, job_mock],
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
            elif token == "gh-empty":
                return CollectorResult(
                    company_source_id=company_source.id,
                    source_id=company_source.source_id,
                    company_id=company.id,
                    ats_identifier=token,
                    status=STATUS_EMPTY,
                    total_discovered=0,
                    total_ingested=0,
                    total_failed=0,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
            elif token == "gh-broken":
                return CollectorResult(
                    company_source_id=company_source.id,
                    source_id=company_source.source_id,
                    company_id=company.id,
                    ats_identifier=token,
                    status=STATUS_FAILED,
                    total_discovered=0,
                    total_ingested=0,
                    total_failed=0,
                    error_message="Greenhouse board 'gh-broken' returned HTTP 500",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )

        collector.collect_company.side_effect = fake_collect
        runner.registry.resolve = MagicMock(return_value=collector)

        result: CollectionRunnerResult = await runner.run_mappings(
            [cs1.id, cs2.id, cs3.id, cs4.id, cs5.id]
        )

        # High-level mapping counts
        assert result.total_mappings_selected == 5
        assert result.total_mappings_processed == 4
        assert result.total_mappings_succeeded == 2  # cs1 (SUCCESS) + cs2 (EMPTY)
        assert result.total_mappings_failed == 2  # cs3 (FAILED) + cs5 (UNSUPPORTED_SOURCE)
        assert result.total_mappings_skipped == 1  # cs4 (SKIPPED_INACTIVE)

        # Aggregate record counts
        assert result.records_discovered == 2
        assert result.records_ingested == 2
        assert result.records_failed == 0
        assert result.duplicates_detected == 0

        # Aggregate HTTP status counts
        # cs1: 1 board (200) + 2 jobs (200) = 3
        # cs2: 1 board (200) = 1
        # Total 2xx = 4
        assert result.http_2xx_count == 4
        # cs3: 1 board (500) = 1
        assert result.http_5xx_count == 1
        assert result.http_4xx_count == 0

        # Verify individual outcomes exist and are in order
        assert len(result.mapping_outcomes) == 5
        assert result.mapping_outcomes[0].status == STATUS_SUCCESS
        assert result.mapping_outcomes[1].status == STATUS_EMPTY
        assert result.mapping_outcomes[2].status == STATUS_FAILED
        assert result.mapping_outcomes[3].status == STATUS_SKIPPED_INACTIVE
        assert result.mapping_outcomes[4].status == STATUS_UNSUPPORTED_SOURCE

    async def test_unhandled_collector_exception_transitions_run_to_failed(
        self, runner_context
    ) -> None:
        """Verify unhandled exception during collector harvest updates CollectionRun to FAILED and never leaves RUNNING."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]
        run_repo = runner_context["run_repo"]
        cs_repo = runner_context["cs_repo"]

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.side_effect = RuntimeError(
            "Fatal parsing crash in collector"
        )
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        outcome = await runner.run_mapping(cs.id)

        assert outcome.status == STATUS_FAILED
        assert outcome.is_failed
        assert "Fatal parsing crash in collector" in outcome.error_message

        # Verify CollectionRun was created and then updated to FAILED
        assert run_repo.create.await_count == 1
        assert run_repo.update.await_count == 1
        updated_run: CollectionRun = run_repo.update.call_args[0][0]
        assert updated_run.status == "FAILED"
        assert updated_run.finished_at is not None
        assert updated_run.telemetry["error_type"] == "COLLECTOR_EXECUTION_ERROR"
        assert "Fatal parsing crash in collector" in updated_run.telemetry["error_message"]

        # Verify CompanySource last_collected_at was updated to avoid infinite scheduler polling loops
        cs_repo.update.assert_awaited_once_with(cs)
        assert cs.last_collected_at == updated_run.finished_at

    async def test_database_error_propagates_rather_than_swallowed(self, runner_context) -> None:
        """Verify database errors that invalidate the transaction propagate to the caller immediately."""
        runner = runner_context["runner"]
        cs = runner_context["company_source"]

        mock_collector = AsyncMock(spec=BaseCollector)
        mock_collector.collect_company.side_effect = SQLAlchemyError("Database connection lost")
        runner.registry.resolve = MagicMock(return_value=mock_collector)

        with pytest.raises(SQLAlchemyError, match="Database connection lost"):
            await runner.run_mapping(cs.id)

        with pytest.raises(SQLAlchemyError, match="Database connection lost"):
            await runner.run_mappings([cs.id])
