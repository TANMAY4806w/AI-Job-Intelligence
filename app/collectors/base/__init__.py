"""Abstract collector bases and politeness-gated HTTP client wrappers."""

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

__all__ = [
    "STATUS_EMPTY",
    "STATUS_FAILED",
    "STATUS_PARTIAL_SUCCESS",
    "STATUS_SUCCESS",
    "BaseCollector",
    "CollectionStatus",
    "CollectorResult",
    "JobCollectionFailure",
    "PoliteHttpClient",
]
