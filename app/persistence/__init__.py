"""PostgreSQL persistence layer.

SQLAlchemy declarative ORM models and repository data access objects.
Boundary: Encapsulates database transactions; models reflect the 3-tier persistence architecture.
"""

from app.persistence.models import (
    AIAnalysisResult,
    Base,
    CollectionRun,
    Company,
    CompanySource,
    DiscoveryRun,
    FilterDecision,
    Job,
    JobSourceReference,
    RawJob,
    Source,
    VerificationRecord,
    metadata,
)

__all__ = [
    "Base",
    "metadata",
    "Company",
    "Source",
    "CompanySource",
    "RawJob",
    "Job",
    "JobSourceReference",
    "VerificationRecord",
    "FilterDecision",
    "AIAnalysisResult",
    "DiscoveryRun",
    "CollectionRun",
]
