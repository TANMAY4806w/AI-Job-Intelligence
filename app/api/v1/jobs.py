"""REST API router for Job Feed, Detail, Filtering, Review Queue, and Audit actions."""

import math
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas.jobs import (
    ApplicationStatusUpdateRequest,
    ApplicationStatusUpdateResponse,
    DismissActionRequest,
    DismissActionResponse,
    FilterDecisionSchema,
    JobDetailSchema,
    JobFilterFacetsSchema,
    JobSourceSummarySchema,
    JobSummarySchema,
    PaginatedResponse,
    ReviewActionRequest,
    ReviewActionResponse,
)
from app.persistence.models.job import Job
from app.persistence.repositories.job_repository import JobRepository
from app.persistence.session import get_db_session

router = APIRouter(tags=["Jobs & Review Queue"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def sanitize_url(url: str | None) -> str | None:
    """Validate URL scheme is strictly http or https.

    Rejects javascript:, data:, vbscript:, and relative malicious vectors.
    """
    if not url:
        return None
    stripped = url.strip()
    if stripped.lower().startswith(("http://", "https://")):
        return stripped
    return None


def _extract_primary_source(job: Job) -> str | None:
    """Safely extract the primary or first source provider name for a job."""
    if not job.source_references:
        return None
    for ref in job.source_references:
        if getattr(ref, "is_primary", False) and ref.source:
            return ref.source.source_name
    first_ref = job.source_references[0]
    return first_ref.source.source_name if first_ref.source else None


def _serialize_job_summary(job: Job) -> JobSummarySchema:
    """Serialize a canonical Job into a JobSummarySchema."""
    return JobSummarySchema(
        id=job.id,
        company_id=job.company_id,
        company_name=job.company_name,
        title=job.title,
        normalized_title=job.normalized_title,
        location=job.location,
        normalized_location=job.normalized_location,
        employment_type=job.employment_type,
        remote_type=job.remote_type,
        role_priority=job.role_priority,
        relevance_status=job.relevance_status,
        eligibility_status=job.eligibility_status,
        rejection_reason=job.rejection_reason,
        review_reason=job.review_reason,
        human_review_status=job.human_review_status,
        application_status=job.application_status,
        application_url=sanitize_url(job.application_url),
        experience_min_years=(
            float(job.experience_min_years) if job.experience_min_years is not None else None
        ),
        experience_max_years=(
            float(job.experience_max_years) if job.experience_max_years is not None else None
        ),
        salary_min=float(job.salary_min) if job.salary_min is not None else None,
        salary_max=float(job.salary_max) if job.salary_max is not None else None,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        posted_at=job.posted_at,
        collected_at=job.collected_at,
        verification_confidence=float(job.verification_confidence),
        primary_source=_extract_primary_source(job),
    )


def _serialize_job_detail(job: Job) -> JobDetailSchema:
    """Serialize a canonical Job into a JobDetailSchema including sources, 8 dimensions, and decisions."""
    sources: list[JobSourceSummarySchema] = []
    if job.source_references:
        for ref in job.source_references:
            sources.append(
                JobSourceSummarySchema(
                    source_name=ref.source.source_name if ref.source else "UNKNOWN",
                    source_type=ref.source.source_type if ref.source else "UNKNOWN",
                    source_job_id=ref.source_job_id,
                    source_url=sanitize_url(ref.source_url) or "#",
                    application_url=sanitize_url(ref.application_url),
                    is_primary=ref.is_primary,
                )
            )

    decisions: list[FilterDecisionSchema] = []
    if job.filter_decisions:
        for dec in sorted(job.filter_decisions, key=lambda d: d.evaluated_at, reverse=True):
            decisions.append(
                FilterDecisionSchema(
                    id=dec.id,
                    rule_version=dec.rule_version,
                    final_status=dec.final_status,
                    role_priority=dec.role_priority,
                    reason_codes=dec.reason_codes or [],
                    evaluation_context=dec.evaluation_context or {},
                    evaluated_at=dec.evaluated_at,
                )
            )

    # Resolve all 8 verification dimensions from latest VerificationRecord if available
    latest_vr = (
        max(job.verification_records, key=lambda r: r.verification_run_at)
        if hasattr(job, "verification_records") and job.verification_records
        else None
    )

    source_verified = latest_vr.source_verified if latest_vr else job.source_verified
    jd_verified = latest_vr.jd_verified if latest_vr else job.jd_verified
    application_url_verified = (
        latest_vr.application_url_verified if latest_vr else bool(sanitize_url(job.application_url))
    )
    company_verified = latest_vr.company_verified if latest_vr else job.company_verified
    location_verified = latest_vr.location_verified if latest_vr else job.location_verified
    experience_verified = latest_vr.experience_verified if latest_vr else job.experience_verified
    salary_verified = latest_vr.salary_verified if latest_vr else job.salary_verified
    posting_date_verified = (
        latest_vr.posting_date_verified if latest_vr else bool(job.posted_at is not None)
    )

    return JobDetailSchema(
        id=job.id,
        company_id=job.company_id,
        company_name=job.company_name,
        title=job.title,
        normalized_title=job.normalized_title,
        location=job.location,
        normalized_location=job.normalized_location,
        employment_type=job.employment_type,
        remote_type=job.remote_type,
        role_priority=job.role_priority,
        relevance_status=job.relevance_status,
        eligibility_status=job.eligibility_status,
        rejection_reason=job.rejection_reason,
        review_reason=job.review_reason,
        human_review_status=job.human_review_status,
        application_status=job.application_status,
        application_url=sanitize_url(job.application_url),
        experience_min_years=(
            float(job.experience_min_years) if job.experience_min_years is not None else None
        ),
        experience_max_years=(
            float(job.experience_max_years) if job.experience_max_years is not None else None
        ),
        salary_min=float(job.salary_min) if job.salary_min is not None else None,
        salary_max=float(job.salary_max) if job.salary_max is not None else None,
        salary_currency=job.salary_currency,
        salary_period=job.salary_period,
        posted_at=job.posted_at,
        collected_at=job.collected_at,
        verification_confidence=float(job.verification_confidence),
        primary_source=_extract_primary_source(job),
        description=job.description,
        responsibilities=job.responsibilities,
        requirements=job.requirements,
        skills=job.skills,
        experience_raw=job.experience_raw,
        source_verified=source_verified,
        jd_verified=jd_verified,
        application_url_verified=application_url_verified,
        company_verified=company_verified,
        location_verified=location_verified,
        experience_verified=experience_verified,
        salary_verified=salary_verified,
        posting_date_verified=posting_date_verified,
        sources=sources,
        filter_decisions=decisions,
    )


@router.get(
    "/jobs",
    response_model=PaginatedResponse[JobSummarySchema],
    summary="List and filter canonical jobs",
)
async def list_jobs(
    session: DbSession,
    page: Annotated[int, Query(ge=1, description="Page number (1-indexed)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
    search: Annotated[
        str | None,
        Query(description="Search keyword across title, company, location, JD"),
    ] = None,
    eligibility: Annotated[
        str | None,
        Query(description="Filter by eligibility status (ELIGIBLE, REVIEW, REJECTED)"),
    ] = None,
    priority: Annotated[
        str | None,
        Query(description="Filter by priority tier (P1, P2, P3)"),
    ] = None,
    location: Annotated[
        str | None,
        Query(description="Filter by location substring"),
    ] = None,
    provider: Annotated[
        str | None,
        Query(description="Filter by ATS provider (greenhouse, lever, ashby)"),
    ] = None,
    remote_type: Annotated[
        str | None,
        Query(description="Filter by remote type (REMOTE, HYBRID, ONSITE)"),
    ] = None,
    min_salary: Annotated[
        float | None,
        Query(description="Minimum salary threshold"),
    ] = None,
    max_experience: Annotated[
        float | None,
        Query(description="Maximum required experience in years"),
    ] = None,
    application_status: Annotated[
        str | None,
        Query(description="Filter by candidate application tracking status"),
    ] = None,
    human_review_status: Annotated[
        str | None,
        Query(description="Filter by human triage status (APPROVED, REJECTED, PENDING, DISMISSED)"),
    ] = None,
    sort_by: Annotated[
        str,
        Query(pattern="^(freshness|priority|company)$", description="Sort order"),
    ] = "freshness",
    include_dismissed: Annotated[
        bool,
        Query(description="Whether to include dismissed jobs"),
    ] = False,
) -> Any:
    """Paginated canonical job listings with faceted filters, text search, and zero N+1 queries."""
    job_repo = JobRepository(session)
    offset = (page - 1) * page_size

    jobs, total = await job_repo.search_and_filter_jobs(
        search=search,
        eligibility=eligibility,
        priority=priority,
        location=location,
        provider=provider,
        remote_type=remote_type,
        min_salary=min_salary,
        max_experience=max_experience,
        application_status=application_status,
        human_review_status=human_review_status,
        sort_by=sort_by,
        include_dismissed=include_dismissed,
        limit=page_size,
        offset=offset,
    )

    items = [_serialize_job_summary(job) for job in jobs]
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get(
    "/jobs/filters",
    response_model=JobFilterFacetsSchema,
    summary="Get dynamic job filter facets and aggregations",
)
async def get_job_filters(
    session: DbSession,
) -> Any:
    """Compute and return aggregated filter facets, counts, and distribution metrics."""
    job_repo = JobRepository(session)
    return await job_repo.get_filter_facets()


@router.get(
    "/jobs/{job_id}",
    response_model=JobDetailSchema,
    summary="Get full job details and verification audit trail",
)
async def get_job_detail(
    job_id: uuid.UUID,
    session: DbSession,
) -> Any:
    """Retrieve complete canonical job details, ATS provenance, 8-dimension verification, and audit logs."""
    job_repo = JobRepository(session)
    job = await job_repo.get_by_id_with_details(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )
    return _serialize_job_detail(job)


@router.get(
    "/review-queue",
    response_model=PaginatedResponse[JobSummarySchema],
    summary="List jobs pending human review",
)
async def get_review_queue(
    session: DbSession,
    page: Annotated[int, Query(ge=1, description="Page number (1-indexed)")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Items per page")] = 20,
) -> Any:
    """Retrieve jobs with eligibility_status == 'REVIEW' requiring human decision triage."""
    job_repo = JobRepository(session)
    offset = (page - 1) * page_size

    jobs, total = await job_repo.get_review_queue(limit=page_size, offset=offset)
    items = [_serialize_job_summary(job) for job in jobs]
    total_pages = math.ceil(total / page_size) if page_size > 0 else 0

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post(
    "/jobs/{job_id}/review",
    response_model=ReviewActionResponse,
    summary="Record human review decision",
)
async def review_job(
    job_id: uuid.UUID,
    body: ReviewActionRequest,
    session: DbSession,
) -> Any:
    """Approve or reject a job in review without modifying deterministic eligibility rules or application status."""
    job_repo = JobRepository(session)
    job = await job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    decision = await job_repo.record_human_review(
        job=job,
        action=body.action,
        notes=body.notes,
        reviewer=body.reviewer,
    )
    await session.commit()

    return ReviewActionResponse(
        job_id=job.id,
        human_review_status=decision.final_status,
        application_status=job.application_status,
        eligibility_status=job.eligibility_status,
        message=f"Human review successfully recorded as {decision.final_status}.",
        audit_decision_id=decision.id,
    )


@router.post(
    "/jobs/{job_id}/dismiss",
    response_model=DismissActionResponse,
    summary="Dismiss a job vacancy",
)
async def dismiss_job(
    job_id: uuid.UUID,
    body: DismissActionRequest,
    session: DbSession,
) -> Any:
    """Dismiss a job from the active feed while logging an immutable audit record."""
    job_repo = JobRepository(session)
    job = await job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    decision = await job_repo.record_dismissal(
        job=job,
        reason=body.reason,
        reviewer=body.reviewer,
    )
    await session.commit()

    return DismissActionResponse(
        job_id=job.id,
        human_review_status="DISMISSED",
        application_status=job.application_status,
        message="Job dismissed successfully.",
        audit_decision_id=decision.id,
    )


@router.patch(
    "/jobs/{job_id}/application-status",
    response_model=ApplicationStatusUpdateResponse,
    summary="Update candidate application workflow tracking status",
)
async def update_job_application_status(
    job_id: uuid.UUID,
    body: ApplicationStatusUpdateRequest,
    session: DbSession,
) -> Any:
    """Update candidate job application tracking status independently of human triage and eligibility."""
    job_repo = JobRepository(session)
    job = await job_repo.get_by_id(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job with ID '{job_id}' not found.",
        )

    await job_repo.update_application_status(job=job, new_status=body.application_status)
    await session.commit()

    return ApplicationStatusUpdateResponse(
        job_id=job.id,
        application_status=job.application_status,
        message=f"Candidate application status updated to '{job.application_status}'.",
    )
