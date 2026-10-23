"""SQLAlchemy ORM models package for AI Job Intelligence.

Exports declarative Base, MetaData, and all 11 domain models:
1. Company (companies)
2. Source (sources)
3. CompanySource (company_sources)
4. RawJob (raw_jobs)
5. Job (jobs)
6. JobSourceReference (job_source_references)
7. VerificationRecord (verification_records)
8. FilterDecision (filter_decisions)
9. AIAnalysisResult (ai_analysis_results)
10. DiscoveryRun (discovery_runs)
11. CollectionRun (collection_runs)
"""

from app.persistence.models.base import Base
from app.persistence.models.company import Company
from app.persistence.models.decision import (
    AIAnalysisResult,
    FilterDecision,
    VerificationRecord,
)
from app.persistence.models.job import Job
from app.persistence.models.raw_job import JobSourceReference, RawJob
from app.persistence.models.run import CollectionRun, DiscoveryRun
from app.persistence.models.source import CompanySource, Source

metadata = Base.metadata

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
