"""API v1 router aggregator."""

from fastapi import APIRouter

from app.api.v1.companies import router as companies_router
from app.api.v1.jobs import router as jobs_router

router = APIRouter(prefix="/api/v1")
router.include_router(jobs_router)
router.include_router(companies_router)
