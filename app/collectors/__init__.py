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
from app.collectors.greenhouse.collector import GreenhouseCollector

__all__ = [
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_PARTIAL_SUCCESS",
    "STATUS_SUCCESS",
    "BaseCollector",
    "CollectionStatus",
    "CollectorResult",
    "GreenhouseCollector",
    "JobCollectionFailure",
    "PoliteHttpClient",
]
