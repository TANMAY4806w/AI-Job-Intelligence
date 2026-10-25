"""Data ingestion layer.

Interfaces with external platforms and ATS endpoints to capture raw external job listings.
Boundary: Collectors must NEVER filter openings by title, experience, or salary.
"""

from app.collectors.base.collector import (
    STATUS_EMPTY,
    STATUS_FAILED,
    STATUS_PARTIAL_SUCCESS,
    STATUS_SUCCESS,
    BaseCollector,
    CollectionStatus,
    CollectorResult,
    JobCollectionFailure,
)
from app.collectors.base.http import PoliteHttpClient
from app.collectors.greenhouse.collector import (
    GREENHOUSE_API_BASE,
    GreenhouseCollector,
    validate_board_token,
)
from app.collectors.registry import (
    CollectorRegistry,
    CollectorRegistryError,
    UnsupportedSourceError,
    default_collector_registry,
)

__all__ = [
    "GREENHOUSE_API_BASE",
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_PARTIAL_SUCCESS",
    "STATUS_SUCCESS",
    "BaseCollector",
    "CollectionStatus",
    "CollectorRegistry",
    "CollectorRegistryError",
    "CollectorResult",
    "GreenhouseCollector",
    "JobCollectionFailure",
    "PoliteHttpClient",
    "UnsupportedSourceError",
    "default_collector_registry",
    "validate_board_token",
]
