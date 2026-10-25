"""Application ingestion pipeline module.

Boundary: Lightweight orchestration entry point for end-to-end ingestion.
Re-exports JobIngestionService, JobIngestionEnvelope, JobIngestionInput, and JobIngestionOutcome.
"""

from app.orchestration.ingestion_service import (
    STATUS_AMBIGUOUS_DUPLICATE,
    STATUS_PROCESSED_MATCH,
    STATUS_PROCESSED_NEW,
    JobIngestionEnvelope,
    JobIngestionInput,
    JobIngestionOutcome,
    JobIngestionService,
)

__all__ = [
    "STATUS_AMBIGUOUS_DUPLICATE",
    "STATUS_PROCESSED_MATCH",
    "STATUS_PROCESSED_NEW",
    "JobIngestionEnvelope",
    "JobIngestionInput",
    "JobIngestionOutcome",
    "JobIngestionService",
]
