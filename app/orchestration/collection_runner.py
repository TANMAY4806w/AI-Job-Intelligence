"""Application collection runner orchestration service.

Boundary: Orchestrates job harvesting across explicitly selected, active CompanySource mappings:
1. Loads and validates selected CompanySource mappings and related Company/Source records.
2. Resolves collectors deterministically via CollectorRegistry.
3. Creates and updates CollectionRun persistence records using existing model fields.
4. Invokes collect_company() on resolved collectors.
5. Aggregates metrics (discovered, ingested, failed, duplicate, HTTP status counts).
6. Never commits transactions; caller retains strict ownership of database commit/rollback.
7. Zero filtering, zero guessing, and explicit error isolation across mappings.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    CollectorResult,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.registry import (
    CollectorRegistry,
    UnsupportedSourceError,
    get_default_collector_registry,
)
from app.orchestration.ingestion_service import JobIngestionService
from app.persistence.models.run import CollectionRun
from app.persistence.models.source import CompanySource
from app.persistence.repositories.collection_run_repository import CollectionRunRepository
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.source_repository import SourceRepository

logger = logging.getLogger(__name__)

STATUS_SKIPPED_INACTIVE = "SKIPPED_INACTIVE"
STATUS_UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"


def _get_loaded_relationship(instance: Any, attr_name: str) -> Any:
    """Return relationship attribute only if already loaded in memory without triggering I/O.

    Inspects SQLAlchemy instance state to confirm the attribute is neither unloaded
    nor expired, avoiding MissingGreenlet exceptions in asynchronous runtime contexts.
    """
    insp = inspect(instance, raiseerr=False)
    if insp is None:
        # Non-ORM mock or plain object
        return getattr(instance, attr_name, None)
    if attr_name in insp.unloaded or attr_name in insp.expired_attributes:
        return None
    return instance.__dict__.get(attr_name)


class MappingCollectionOutcome(BaseModel):
    """Typed outcome for an individual CompanySource collection attempt."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
    )

    company_source_id: uuid.UUID = Field(
        ...,
        description="CompanySource mapping identifier",
    )
    company_id: uuid.UUID | None = Field(
        default=None,
        description="Associated hiring company identifier",
    )
    source_id: uuid.UUID | None = Field(
        default=None,
        description="Associated platform source identifier",
    )
    company_name: str | None = Field(
        default=None,
        description="Hiring company display name",
    )
    ats_identifier: str | None = Field(
        default=None,
        description="Target ATS board token or endpoint locator",
    )
    status: str = Field(
        ...,
        description="Outcome status: SUCCESS, PARTIAL_SUCCESS, EMPTY, FAILED, SKIPPED_INACTIVE, UNSUPPORTED_SOURCE",
    )
    collection_run_id: uuid.UUID | None = Field(
        default=None,
        description="CollectionRun persistence UUID if a run was initiated",
    )
    collector_result: CollectorResult | None = Field(
        default=None,
        description="Collector execution outcome if collection was invoked",
    )
    records_discovered: int = Field(
        default=0,
        description="Total job openings discovered on the endpoint",
    )
    records_ingested: int = Field(
        default=0,
        description="Total jobs successfully processed through ingestion",
    )
    records_failed: int = Field(
        default=0,
        description="Total jobs that failed parsing or pipeline ingestion",
    )
    duplicates_detected: int = Field(
        default=0,
        description="Duplicate observations matched to existing canonical jobs",
    )
    http_2xx_count: int = Field(
        default=0,
        description="Successful HTTP request count",
    )
    http_4xx_count: int = Field(
        default=0,
        description="Client error HTTP request count (e.g. 404, 429)",
    )
    http_5xx_count: int = Field(
        default=0,
        description="Server error HTTP request count (e.g. 500, 503)",
    )
    error_message: str | None = Field(
        default=None,
        description="Non-secret descriptive error message if collection failed",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when mapping processing began",
    )
    finished_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when mapping processing completed",
    )

    @property
    def is_success(self) -> bool:
        """True if collection completed with zero errors."""
        return self.status == STATUS_SUCCESS

    @property
    def is_partial(self) -> bool:
        """True if some jobs were ingested but others failed."""
        return self.status == STATUS_PARTIAL_SUCCESS

    @property
    def is_empty(self) -> bool:
        """True if endpoint was queried successfully and contained no jobs."""
        return self.status == STATUS_EMPTY

    @property
    def is_failed(self) -> bool:
        """True if mapping collection failed or encountered an unsupported source."""
        return self.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE)

    @property
    def is_skipped(self) -> bool:
        """True if mapping was skipped due to inactive status."""
        return self.status == STATUS_SKIPPED_INACTIVE


class CollectionRunnerResult(BaseModel):
    """Aggregate result across all selected CompanySource mappings."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
    )

    run_type: str = Field(
        ...,
        description="Cadence or trigger tier: MANUAL_TRIGGER, HIGH_FREQUENCY_ATS, DAILY_CRAWL",
    )
    total_mappings_selected: int = Field(
        default=0,
        description="Total mappings requested for execution",
    )
    total_mappings_processed: int = Field(
        default=0,
        description="Total mappings evaluated and attempted",
    )
    total_mappings_succeeded: int = Field(
        default=0,
        description="Total mappings completing with SUCCESS, EMPTY, or PARTIAL_SUCCESS",
    )
    total_mappings_failed: int = Field(
        default=0,
        description="Total mappings failing collection or unsupported",
    )
    total_mappings_skipped: int = Field(
        default=0,
        description="Total mappings skipped because mapping or source is inactive",
    )
    records_discovered: int = Field(
        default=0,
        description="Aggregate count of job listings discovered across all mappings",
    )
    records_ingested: int = Field(
        default=0,
        description="Aggregate count of jobs successfully ingested across all mappings",
    )
    records_failed: int = Field(
        default=0,
        description="Aggregate count of jobs failing retrieval/ingestion across all mappings",
    )
    duplicates_detected: int = Field(
        default=0,
        description="Aggregate count of duplicate job observations linked to existing jobs",
    )
    http_2xx_count: int = Field(
        default=0,
        description="Aggregate successful HTTP request count",
    )
    http_4xx_count: int = Field(
        default=0,
        description="Aggregate client error HTTP request count",
    )
    http_5xx_count: int = Field(
        default=0,
        description="Aggregate server error HTTP request count",
    )
    mapping_outcomes: list[MappingCollectionOutcome] = Field(
        default_factory=list,
        description="Per-mapping collection outcome details",
    )
    collection_runs: list[Any] = Field(
        default_factory=list,
        description="CollectionRun database entities created and updated during execution",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Execution start timestamp",
    )
    finished_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Execution completion timestamp",
    )


class CollectionRunner:
    """Application orchestration service coordinating job collection across ATS endpoints."""

    def __init__(
        self,
        company_source_repository: CompanySourceRepository,
        source_repository: SourceRepository,
        company_repository: CompanyRepository,
        collection_run_repository: CollectionRunRepository,
        ingestion_service: JobIngestionService,
        *,
        registry: CollectorRegistry | None = None,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Initialize CollectionRunner with repositories and ingestion service.

        Args:
            company_source_repository: Repository managing CompanySource mappings.
            source_repository: Repository managing Source platform catalog.
            company_repository: Repository managing canonical Company entities.
            collection_run_repository: Repository managing CollectionRun telemetry.
            ingestion_service: Ingestion service for pipeline processing.
            registry: Optional collector registry; defaults to default_collector_registry.
            http_client: Optional polite HTTP client passed to collectors.
        """
        self.company_source_repo = company_source_repository
        self.source_repo = source_repository
        self.company_repo = company_repository
        self.collection_run_repo = collection_run_repository
        self.ingestion_service = ingestion_service
        self.registry = registry or get_default_collector_registry()
        self.http_client = http_client or PoliteHttpClient()

    @classmethod
    def from_session(
        cls,
        session: AsyncSession,
        *,
        company_source_repository: CompanySourceRepository | None = None,
        source_repository: SourceRepository | None = None,
        company_repository: CompanyRepository | None = None,
        collection_run_repository: CollectionRunRepository | None = None,
        ingestion_service: JobIngestionService | None = None,
        registry: CollectorRegistry | None = None,
        http_client: PoliteHttpClient | None = None,
    ) -> "CollectionRunner":
        """Instantiate CollectionRunner and underlying components from an active AsyncSession."""
        cs_repo = company_source_repository or CompanySourceRepository(session)
        s_repo = source_repository or SourceRepository(session)
        c_repo = company_repository or CompanyRepository(session)
        run_repo = collection_run_repository or CollectionRunRepository(session)
        ingestion = ingestion_service or JobIngestionService.from_session(
            session, company_source_repository=cs_repo
        )

        return cls(
            company_source_repository=cs_repo,
            source_repository=s_repo,
            company_repository=c_repo,
            collection_run_repository=run_repo,
            ingestion_service=ingestion,
            registry=registry,
            http_client=http_client,
        )

    async def run_mapping(
        self,
        company_source: uuid.UUID | CompanySource,
        *,
        run_type: str = "MANUAL_TRIGGER",
    ) -> MappingCollectionOutcome:
        """Execute job collection for a single CompanySource endpoint.

        Args:
            company_source: CompanySource UUID or loaded CompanySource instance.
            run_type: Cadence tier (MANUAL_TRIGGER, HIGH_FREQUENCY_ATS, DAILY_CRAWL).

        Returns:
            MappingCollectionOutcome: Detailed execution outcome and metrics.
        """
        started_at = datetime.now(UTC)

        # 1. Resolve CompanySource entity
        if isinstance(company_source, uuid.UUID):
            resolved_cs = await self.company_source_repo.get_by_id(company_source)
            if resolved_cs is None:
                return MappingCollectionOutcome(
                    company_source_id=company_source,
                    status=STATUS_FAILED,
                    error_message=f"CompanySource mapping '{company_source}' not found.",
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
        else:
            resolved_cs = company_source

        # Resolve CompanySource primary key safely without triggering lazy load on expired instances
        insp = inspect(resolved_cs, raiseerr=False)
        cs_id: uuid.UUID | None = None
        if insp is not None:
            if insp.identity is not None and len(insp.identity) > 0:
                cs_id = insp.identity[0]
            elif "id" in resolved_cs.__dict__:
                cs_id = resolved_cs.__dict__["id"]
        if cs_id is None:
            cs_id = getattr(resolved_cs, "id", None)

        # Ensure CompanySource mapped columns are refreshed if expired
        if (
            insp is not None
            and (insp.expired or "source_id" in insp.expired_attributes)
            and cs_id is not None
        ):
            refreshed = await self.company_source_repo.get_by_id(cs_id)
            if refreshed is not None:
                resolved_cs = refreshed

        if cs_id is None:
            cs_id = getattr(resolved_cs, "id", None)

        # 2. Check mapping active state
        if not resolved_cs.is_active:
            logger.info("Skipping inactive CompanySource mapping %s", cs_id)
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_cs.company_id,
                source_id=resolved_cs.source_id,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_SKIPPED_INACTIVE,
                error_message="CompanySource mapping is marked inactive.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # 3. Resolve and validate Source platform
        resolved_source = _get_loaded_relationship(resolved_cs, "source")
        if resolved_source is None:
            resolved_source = await self.source_repo.get_by_id(resolved_cs.source_id)

        if resolved_source is None:
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_cs.company_id,
                source_id=resolved_cs.source_id,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_FAILED,
                error_message=f"Source platform '{resolved_cs.source_id}' not found.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        if not resolved_source.is_active:
            logger.info(
                "Skipping CompanySource %s because source '%s' is inactive",
                cs_id,
                resolved_source.source_name,
            )
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_cs.company_id,
                source_id=resolved_source.id,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_SKIPPED_INACTIVE,
                error_message=f"Source platform '{resolved_source.source_name}' is marked inactive.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # 4. Resolve and validate Company entity
        resolved_company = _get_loaded_relationship(resolved_cs, "company")
        if resolved_company is None:
            resolved_company = await self.company_repo.get_by_id(resolved_cs.company_id)

        if resolved_company is None:
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_cs.company_id,
                source_id=resolved_source.id,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_FAILED,
                error_message=f"Company entity '{resolved_cs.company_id}' not found.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # 5. Validate ATS locator presence
        if not resolved_cs.ats_identifier or not resolved_cs.ats_identifier.strip():
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_company.id,
                source_id=resolved_source.id,
                company_name=resolved_company.name,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_FAILED,
                error_message="CompanySource missing required non-blank 'ats_identifier' locator.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # 6. Check collector registry support
        if not self.registry.is_supported(resolved_source.source_name):
            err_msg = (
                f"Unsupported or unregistered source platform: '{resolved_source.source_name}'. "
                "Supported sources in V1: GREENHOUSE."
            )
            logger.warning(err_msg)

            # Persist CollectionRun with FAILED status and diagnostic telemetry
            failed_run = CollectionRun(
                source_id=resolved_source.id,
                company_source_id=cs_id,
                run_type=run_type,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                status="FAILED",
                records_discovered=0,
                records_ingested=0,
                records_failed=0,
                duplicates_detected=0,
                http_2xx_count=0,
                http_4xx_count=0,
                http_5xx_count=0,
                telemetry={
                    "error_type": "UNSUPPORTED_SOURCE",
                    "error_message": err_msg,
                    "source_name": resolved_source.source_name,
                    "company_name": resolved_company.name,
                },
            )
            failed_run = await self.collection_run_repo.create(failed_run)

            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_company.id,
                source_id=resolved_source.id,
                company_name=resolved_company.name,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_UNSUPPORTED_SOURCE,
                collection_run_id=failed_run.id,
                error_message=err_msg,
                started_at=started_at,
                finished_at=failed_run.finished_at or datetime.now(UTC),
            )

        # 7. Resolve collector from registry
        try:
            collector = self.registry.resolve(
                resolved_source,
                self.ingestion_service,
                http_client=self.http_client,
            )
        except UnsupportedSourceError as exc:
            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_company.id,
                source_id=resolved_source.id,
                company_name=resolved_company.name,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_UNSUPPORTED_SOURCE,
                error_message=str(exc),
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        # 8. Create initial CollectionRun record with RUNNING status
        collection_run = CollectionRun(
            source_id=resolved_source.id,
            company_source_id=cs_id,
            run_type=run_type,
            started_at=started_at,
            status="RUNNING",
            records_discovered=0,
            records_ingested=0,
            records_failed=0,
            duplicates_detected=0,
            http_2xx_count=0,
            http_4xx_count=0,
            http_5xx_count=0,
        )
        collection_run = await self.collection_run_repo.create(collection_run)

        # 9. Execute collector harvest
        if not getattr(resolved_cs, "source", None):
            resolved_cs.source = resolved_source
        try:
            collector_result = await collector.collect_company(
                company_source=resolved_cs,
                company=resolved_company,
                collection_run_id=collection_run.id,
            )
        except SQLAlchemyError:
            # Database errors that invalidate the transaction must propagate immediately
            raise
        except Exception as exc:
            logger.error(
                "Unhandled error during collector harvest for CompanySource %s: %s",
                cs_id,
                exc,
            )
            finished_at = datetime.now(UTC)
            collection_run.finished_at = finished_at
            collection_run.status = "FAILED"
            collection_run.telemetry = {
                "error_type": "COLLECTOR_EXECUTION_ERROR",
                "error_message": str(exc),
                "source_name": resolved_source.source_name,
                "company_name": resolved_company.name,
                "ats_identifier": resolved_cs.ats_identifier,
            }
            await self.collection_run_repo.update(collection_run)
            resolved_cs.last_collected_at = finished_at
            await self.company_source_repo.update(resolved_cs)

            return MappingCollectionOutcome(
                company_source_id=cs_id,
                company_id=resolved_company.id,
                source_id=resolved_source.id,
                company_name=resolved_company.name,
                ats_identifier=resolved_cs.ats_identifier,
                status=STATUS_FAILED,
                collection_run_id=collection_run.id,
                error_message=f"Collection failed with error: {exc}",
                started_at=started_at,
                finished_at=finished_at,
            )

        # 10. Aggregate metrics
        duplicates_detected = sum(
            1 for o in collector_result.ingestion_outcomes if getattr(o, "is_match", False)
        )

        # Compute HTTP status distribution
        http_2xx = 0
        http_4xx = 0
        http_5xx = 0

        # Successful board queries count as 1 x 2xx
        if collector_result.status in (STATUS_SUCCESS, STATUS_EMPTY, STATUS_PARTIAL_SUCCESS):
            http_2xx += 1
        elif collector_result.error_message and "returned HTTP 4" in collector_result.error_message:
            http_4xx += 1
        elif collector_result.error_message and "returned HTTP 5" in collector_result.error_message:
            http_5xx += 1

        # Job-level HTTP responses
        for outcome in collector_result.ingestion_outcomes:
            raw_job = getattr(outcome, "raw_job", None)
            code = getattr(raw_job, "http_status", 200)
            if 200 <= code < 300:
                http_2xx += 1
            elif 400 <= code < 500:
                http_4xx += 1
            elif 500 <= code < 600:
                http_5xx += 1

        # Job-level HTTP failures
        for failure in collector_result.failures:
            code = failure.http_status
            if code is not None:
                if 200 <= code < 300:
                    http_2xx += 1
                elif 400 <= code < 500:
                    http_4xx += 1
                elif 500 <= code < 600:
                    http_5xx += 1

        # 11. Determine CollectionRun status
        # Note on schema vocabulary: CollectionRun.status supports RUNNING, COMPLETED, FAILED, CANCELLED.
        # More granular collector statuses (SUCCESS, PARTIAL_SUCCESS, EMPTY) are recorded in telemetry.
        run_status = "FAILED" if collector_result.status == STATUS_FAILED else "COMPLETED"

        # 12. Build diagnostic telemetry dictionary
        telemetry: dict[str, Any] = {
            "collector_status": collector_result.status,
            "source_name": resolved_source.source_name,
            "company_name": resolved_company.name,
            "ats_identifier": resolved_cs.ats_identifier,
            "failures_count": len(collector_result.failures),
        }
        if collector_result.error_message:
            telemetry["error_message"] = collector_result.error_message
        if collector_result.failures:
            # Cap diagnostic failure list to avoid payload bloat
            telemetry["failures"] = [f.model_dump() for f in collector_result.failures[:30]]

        # 13. Update CollectionRun record
        collection_run.finished_at = collector_result.finished_at
        collection_run.status = run_status
        collection_run.records_discovered = collector_result.total_discovered
        collection_run.records_ingested = collector_result.total_ingested
        collection_run.records_failed = collector_result.total_failed
        collection_run.duplicates_detected = duplicates_detected
        collection_run.http_2xx_count = http_2xx
        collection_run.http_4xx_count = http_4xx
        collection_run.http_5xx_count = http_5xx
        collection_run.telemetry = telemetry
        await self.collection_run_repo.update(collection_run)

        # 14. Update CompanySource last_collected_at timestamp
        resolved_cs.last_collected_at = collector_result.finished_at
        await self.company_source_repo.update(resolved_cs)

        # 15. Return structured mapping outcome
        return MappingCollectionOutcome(
            company_source_id=cs_id,
            company_id=resolved_company.id,
            source_id=resolved_source.id,
            company_name=resolved_company.name,
            ats_identifier=resolved_cs.ats_identifier,
            status=collector_result.status,
            collection_run_id=collection_run.id,
            collector_result=collector_result,
            records_discovered=collector_result.total_discovered,
            records_ingested=collector_result.total_ingested,
            records_failed=collector_result.total_failed,
            duplicates_detected=duplicates_detected,
            http_2xx_count=http_2xx,
            http_4xx_count=http_4xx,
            http_5xx_count=http_5xx,
            error_message=collector_result.error_message,
            started_at=started_at,
            finished_at=collector_result.finished_at,
        )

    async def run_mappings(
        self,
        company_sources: Sequence[uuid.UUID | CompanySource],
        *,
        run_type: str = "MANUAL_TRIGGER",
    ) -> CollectionRunnerResult:
        """Execute job collection across multiple explicitly selected CompanySource mappings.

        Error isolation policy:
        - If an individual mapping fails (HTTP error, timeout, malformed data, or unsupported source),
          the error is recorded in that mapping's MappingCollectionOutcome and CollectionRun record.
        - The runner safely continues to execute subsequent selected mappings within the transaction.
        - The transaction remains open; commit/rollback lifecycle is owned strictly by the caller.

        Args:
            company_sources: Sequence of CompanySource UUIDs or loaded CompanySource instances.
            run_type: Cadence tier (MANUAL_TRIGGER, HIGH_FREQUENCY_ATS, DAILY_CRAWL).

        Returns:
            CollectionRunnerResult: Aggregate counts and per-mapping execution outcomes.
        """
        started_at = datetime.now(UTC)
        outcomes: list[MappingCollectionOutcome] = []
        runs: list[CollectionRun] = []

        for item in company_sources:
            cs_id = item.id if isinstance(item, CompanySource) else item
            try:
                outcome = await self.run_mapping(item, run_type=run_type)
                outcomes.append(outcome)
                if outcome.collection_run_id:
                    run = await self.collection_run_repo.get_by_id(outcome.collection_run_id)
                    if run:
                        runs.append(run)
            except SQLAlchemyError:
                # Database integrity errors invalidate the session transaction; propagate to caller
                raise
            except Exception as exc:
                logger.error(
                    "Unexpected error during collection for CompanySource %s: %s",
                    cs_id,
                    exc,
                )
                outcomes.append(
                    MappingCollectionOutcome(
                        company_source_id=cs_id,
                        status=STATUS_FAILED,
                        error_message=f"Unexpected error during collection: {exc}",
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                    )
                )

        finished_at = datetime.now(UTC)

        total_selected = len(company_sources)
        total_processed = len([o for o in outcomes if o.status not in (STATUS_SKIPPED_INACTIVE,)])
        total_succeeded = len(
            [
                o
                for o in outcomes
                if o.status in (STATUS_SUCCESS, STATUS_EMPTY, STATUS_PARTIAL_SUCCESS)
            ]
        )
        total_failed = len(
            [o for o in outcomes if o.status in (STATUS_FAILED, STATUS_UNSUPPORTED_SOURCE)]
        )
        total_skipped = len([o for o in outcomes if o.status == STATUS_SKIPPED_INACTIVE])

        return CollectionRunnerResult(
            run_type=run_type,
            total_mappings_selected=total_selected,
            total_mappings_processed=total_processed,
            total_mappings_succeeded=total_succeeded,
            total_mappings_failed=total_failed,
            total_mappings_skipped=total_skipped,
            records_discovered=sum(o.records_discovered for o in outcomes),
            records_ingested=sum(o.records_ingested for o in outcomes),
            records_failed=sum(o.records_failed for o in outcomes),
            duplicates_detected=sum(o.duplicates_detected for o in outcomes),
            http_2xx_count=sum(o.http_2xx_count for o in outcomes),
            http_4xx_count=sum(o.http_4xx_count for o in outcomes),
            http_5xx_count=sum(o.http_5xx_count for o in outcomes),
            mapping_outcomes=outcomes,
            collection_runs=runs,
            started_at=started_at,
            finished_at=finished_at,
        )
