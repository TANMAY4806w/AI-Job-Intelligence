"""Asynchronous persistence repositories.

Encapsulates database operations for canonical entities, raw evidence, and provenance links.
All repositories accept an active AsyncSession; transaction boundaries remain with the caller.
"""

from app.persistence.repositories.base import BaseRepository
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.company_source_repository import CompanySourceRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.repositories.job_source_reference_repository import (
    JobSourceReferenceRepository,
)
from app.persistence.repositories.raw_job_repository import RawJobRepository
from app.persistence.repositories.source_repository import SourceRepository

__all__ = [
    "BaseRepository",
    "CompanyRepository",
    "CompanySourceRepository",
    "JobRepository",
    "JobSourceReferenceRepository",
    "RawJobRepository",
    "SourceRepository",
]
