"""Abstract base collector and collection lifecycle contracts.

Boundary: Defines standard collect_company() lifecycle and outcome contracts.
Collectors must NEVER filter openings by title, experience, or salary; all discovered
vacancies are passed directly to JobIngestionService for normalization and evaluation.
"""

import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.collectors.base.http import PoliteHttpClient
from app.orchestration.ingestion_service import JobIngestionService
from app.persistence.models.company import Company
from app.persistence.models.source import CompanySource

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
STATUS_FAILED = "FAILED"
STATUS_EMPTY = "EMPTY"

CollectionStatus = Literal["SUCCESS", "PARTIAL_SUCCESS", "FAILED", "EMPTY"]


class JobCollectionFailure(BaseModel):
    """Diagnostic detail for an individual job observation that failed capture or ingestion."""

    model_config = ConfigDict(frozen=True)

    source_job_id: str | None = Field(
        default=None,
        description="Platform-native identifier of the job post if available",
    )
    source_url: str | None = Field(
        default=None,
        description="Endpoint URL attempted for the observation",
    )
    http_status: int | None = Field(
        default=None,
        description="HTTP response status code if an HTTP query occurred",
    )
    error_type: str = Field(
        ...,
        description="Category: HTTP_ERROR, TIMEOUT, PARSE_ERROR, INVALID_DATA, INGESTION_ERROR",
    )
    error_message: str = Field(
        ...,
        description="Non-secret descriptive error message",
    )


class CollectorResult(BaseModel):
    """Aggregate result of executing a collector against a CompanySource endpoint."""

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        frozen=True,
    )

    company_source_id: uuid.UUID = Field(
        ...,
        description="CompanySource mapping UUID",
    )
    source_id: uuid.UUID = Field(
        ...,
        description="Origin source UUID",
    )
    company_id: uuid.UUID = Field(
        ...,
        description="Hiring company UUID",
    )
    ats_identifier: str = Field(
        ...,
        description="Target ATS board token or identifier",
    )
    status: CollectionStatus = Field(
        ...,
        description="Outcome status: SUCCESS, PARTIAL_SUCCESS, FAILED, EMPTY",
    )
    total_discovered: int = Field(
        default=0,
        description="Total job IDs discovered on the job board listing",
    )
    total_ingested: int = Field(
        default=0,
        description="Total job observations successfully processed through ingestion pipeline",
    )
    total_failed: int = Field(
        default=0,
        description="Total job observations that failed retrieval or pipeline ingestion",
    )
    ingestion_outcomes: list[Any] = Field(
        default_factory=list,
        description="List of pipeline ingestion outcomes for captured jobs",
    )
    failures: list[JobCollectionFailure] = Field(
        default_factory=list,
        description="Diagnostic failure records for un-ingested observations",
    )
    error_message: str | None = Field(
        default=None,
        description="Fatal board-level error message if the entire board request failed",
    )
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when collection started",
    )
    finished_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp when collection finished",
    )

    @property
    def is_success(self) -> bool:
        """True if collection completed with no fatal board error and zero failed jobs."""
        return self.status == STATUS_SUCCESS

    @property
    def is_partial(self) -> bool:
        """True if some jobs were ingested but others failed."""
        return self.status == STATUS_PARTIAL_SUCCESS

    @property
    def is_failed(self) -> bool:
        """True if board request failed or all discovered jobs failed."""
        return self.status == STATUS_FAILED

    @property
    def is_empty(self) -> bool:
        """True if board was successfully queried and contained zero job listings."""
        return self.status == STATUS_EMPTY


class BaseCollector(ABC):
    """Abstract base collector defining the standard company collection lifecycle."""

    def __init__(
        self,
        ingestion_service: JobIngestionService,
        *,
        http_client: PoliteHttpClient | None = None,
    ) -> None:
        """Initialize collector.

        Args:
            ingestion_service: JobIngestionService instance for pipeline processing.
            http_client: Optional PoliteHttpClient instance; defaults to standard polite client.
        """
        self.ingestion_service = ingestion_service
        self.http_client = http_client or PoliteHttpClient()

    @abstractmethod
    async def collect_company(
        self,
        company_source: CompanySource,
        company: Company | None = None,
        *,
        collection_run_id: uuid.UUID | None = None,
    ) -> CollectorResult:
        """Collect and ingest all active job vacancies for a given company endpoint.

        Args:
            company_source: Configured CompanySource mapping with ATS identifier.
            company: Associated Company entity (if omitted, extracted from company_source.company).
            collection_run_id: Optional collection run context UUID.

        Returns:
            CollectorResult: Aggregate outcome including ingested jobs and diagnostic failures.
        """
        raise NotImplementedError
