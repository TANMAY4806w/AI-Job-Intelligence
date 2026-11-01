"""HTML Web Dashboard router serving Jinja2-rendered interactive interfaces."""

import math
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.companies import _serialize_company_summary
from app.api.v1.jobs import _serialize_job_detail, _serialize_job_summary
from app.persistence.repositories.company_repository import CompanyRepository
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.session import get_db_session

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _safe_url_filter(url: str | None) -> str:
    """Sanitize URL for Jinja templates, returning '#' if not strictly http/https.

    Rejects javascript:, data:, vbscript:, and malicious schemes.
    """
    if not url:
        return "#"
    trimmed = str(url).strip()
    if trimmed.lower().startswith(("http://", "https://")):
        return trimmed
    return "#"


templates.env.filters["safe_url"] = _safe_url_filter

router = APIRouter(include_in_schema=False)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_view(
    request: Request,
    session: DbSession,
) -> Any:
    """Render overall analytics dashboard, pipeline health, and recent vacancies."""
    job_repo = JobRepository(session)
    facets = await job_repo.get_filter_facets()
    recent_jobs_db, _ = await job_repo.search_and_filter_jobs(
        limit=8,
        sort_by="freshness",
        include_dismissed=False,
    )
    recent_jobs = [_serialize_job_summary(j) for j in recent_jobs_db]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "metrics": facets,
            "recent_jobs": recent_jobs,
            "review_count": facets.get("total_review", 0),
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_feed_view(
    request: Request,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query()] = None,
    eligibility: Annotated[str | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    location: Annotated[str | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    remote_type: Annotated[str | None, Query()] = None,
    sort_by: Annotated[str, Query()] = "freshness",
) -> Any:
    """Render searchable, faceted job feed with pagination and action triage."""
    job_repo = JobRepository(session)
    offset = (page - 1) * page_size

    jobs_db, total = await job_repo.search_and_filter_jobs(
        search=search,
        eligibility=eligibility,
        priority=priority,
        location=location,
        provider=provider,
        remote_type=remote_type,
        sort_by=sort_by,
        include_dismissed=False,
        limit=page_size,
        offset=offset,
    )
    facets = await job_repo.get_filter_facets()
    jobs = [_serialize_job_summary(j) for j in jobs_db]
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    active_filters = {
        "search": search,
        "eligibility": eligibility,
        "priority": priority,
        "location": location,
        "provider": provider,
        "remote_type": remote_type,
        "sort_by": sort_by,
    }

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "active_page": "jobs",
            "jobs": jobs,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "facets": facets,
            "active_filters": active_filters,
            "review_count": facets.get("total_review", 0),
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_view(
    job_id: uuid.UUID,
    request: Request,
    session: DbSession,
) -> Any:
    """Render full job description, 7-dimension verification grid, and decision audit logs."""
    job_repo = JobRepository(session)
    job_db = await job_repo.get_by_id_with_details(job_id)
    if job_db is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    job = _serialize_job_detail(job_db)
    facets = await job_repo.get_filter_facets()

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "active_page": "jobs",
            "job": job,
            "review_count": facets.get("total_review", 0),
        },
    )


@router.get("/review-queue", response_class=HTMLResponse)
async def review_queue_view(
    request: Request,
    session: DbSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Any:
    """Render human triage queue for vacancies with eligibility_status == 'REVIEW'."""
    job_repo = JobRepository(session)
    offset = (page - 1) * page_size

    jobs_db, total = await job_repo.get_review_queue(limit=page_size, offset=offset)
    jobs = [_serialize_job_summary(j) for j in jobs_db]
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    return templates.TemplateResponse(
        request=request,
        name="review_queue.html",
        context={
            "active_page": "review-queue",
            "jobs": jobs,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "review_count": total,
        },
    )


@router.get("/companies", response_class=HTMLResponse)
async def companies_view(
    request: Request,
    session: DbSession,
) -> Any:
    """Render list of tracked companies and their ATS source integrations."""
    company_repo = CompanyRepository(session)
    job_repo = JobRepository(session)

    results = await company_repo.list_all_with_stats(limit=100, offset=0)
    companies = [_serialize_company_summary(c, cnt) for c, cnt in results]
    facets = await job_repo.get_filter_facets()

    return templates.TemplateResponse(
        request=request,
        name="companies.html",
        context={
            "active_page": "companies",
            "companies": companies,
            "review_count": facets.get("total_review", 0),
        },
    )
