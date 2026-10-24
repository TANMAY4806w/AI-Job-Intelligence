"""Data standardization layer.

Transforms unstructured external attributes into strongly typed canonical representations.
Boundary: Technology-agnostic; operates on generic payload dictionaries without ATS coupling.
"""

from app.normalization.dates import parse_publication_date
from app.normalization.location import normalize_location
from app.normalization.schemas import ExtractedJobPayload, NormalizedJobData
from app.normalization.service import JobNormalizationService, normalize_job_payload
from app.normalization.title import normalize_title
from app.normalization.urls import normalize_url

__all__ = [
    "ExtractedJobPayload",
    "JobNormalizationService",
    "NormalizedJobData",
    "normalize_job_payload",
    "normalize_location",
    "normalize_title",
    "normalize_url",
    "parse_publication_date",
]
