"""REST API router for Company profiles and ATS source configurations."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.company import CompanySourceSummarySchema, CompanySummarySchema
from app.persistence.models.company import Company
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.session import get_db_session

router = APIRouter(prefix="/companies", tags=["Companies"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _serialize_company_summary(company: Company, active_jobs_count: int) -> CompanySummarySchema:
    """Serialize Company model instance and aggregate count into CompanySummarySchema."""
    sources: list[CompanySourceSummarySchema] = []
    if company.company_sources:
        for cs in company.company_sources:
            sources.append(
                CompanySourceSummarySchema(
                    source_name=cs.source.source_name if cs.source else "UNKNOWN",
                    source_type=cs.source.source_type if cs.source else "UNKNOWN",
                    ats_identifier=cs.ats_identifier,
                    career_url=cs.career_url,
                    is_active=cs.is_active,
                )
            )

    return CompanySummarySchema(
        id=company.id,
        name=company.name,
        normalized_name=company.normalized_name,
        website=company.website,
        industry=company.industry,
        sector=company.sector,
        is_verified=company.is_verified,
        confidence=float(company.confidence),
        sources=sources,
        active_jobs_count=active_jobs_count,
    )


@router.get(
    "",
    response_model=list[CompanySummarySchema],
    summary="List companies with ATS sources and active job counts",
)
async def list_companies(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200, description="Items limit")] = 100,
    offset: Annotated[int, Query(ge=0, description="Items offset")] = 0,
) -> Any:
    """Retrieve companies configured in the intelligence system with their ATS boards and active job counts."""
    company_repo = CompanyRepository(session)
    results = await company_repo.list_all_with_stats(limit=limit, offset=offset)

    return [_serialize_company_summary(company, active_count) for company, active_count in results]
