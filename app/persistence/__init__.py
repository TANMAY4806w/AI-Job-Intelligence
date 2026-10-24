"""PostgreSQL persistence layer.

SQLAlchemy declarative ORM models, session management, and repository data access objects.
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
from app.persistence.repositories import (
    BaseRepository,
    CompanyRepository,
    CompanySourceRepository,
    JobRepository,
    JobSourceReferenceRepository,
    RawJobRepository,
    SourceRepository,
)
from app.persistence.session import (
    dispose_engine,
    get_db_session,
    get_engine,
    get_sessionmaker,
    session_scope,
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
    "BaseRepository",
    "CompanyRepository",
    "CompanySourceRepository",
    "JobRepository",
    "JobSourceReferenceRepository",
    "RawJobRepository",
    "SourceRepository",
    "dispose_engine",
    "get_db_session",
    "get_engine",
    "get_sessionmaker",
    "session_scope",
]
