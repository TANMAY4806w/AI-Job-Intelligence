"""FastAPI Application entrypoint and router registration."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config.settings import get_settings
from app.persistence.session import dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan managing startup and shutdown resource cleanup."""
    # Startup: non-blocking on database availability; schema is managed via Alembic
    yield
    # Shutdown: cleanly dispose of pooled database connections
    await dispose_engine()


def create_app() -> FastAPI:
    """Application factory creating and configuring the FastAPI instance."""
    settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="Centralized, multi-source job aggregation, verification, and intelligence engine.",
        docs_url="/docs" if settings.DEBUG else None,
        redoc_url="/redoc" if settings.DEBUG else None,
        lifespan=lifespan,
    )

    # Register routers
    app.include_router(health_router)

    return app


app = create_app()
