"""FastAPI Application entrypoint and router registration."""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config.settings import get_settings


def create_app() -> FastAPI:
    """Application factory creating and configuring the FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Centralized, multi-source job aggregation, verification, and intelligence engine.",
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
    )

    # Register routers
    app.include_router(health_router)

    return app


app = create_app()
