"""Health check API router."""

from typing import Any

from fastapi import APIRouter

from app.config.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health", summary="Health Check")
async def health_check() -> dict[str, Any]:
    """Return application operational health status and version metadata."""
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.APP_ENV,
    }
