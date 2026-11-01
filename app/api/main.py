"""FastAPI Application entrypoint and router registration."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.health import router as health_router
from app.api.middleware.csrf import CSRFOriginValidationMiddleware
from app.api.v1 import router as api_v1_router
from app.api.web import router as web_router
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

    # Register security, Host-header validation & CSRF middlewares
    # Allowed hosts are explicit, environment-aware, and never wildcarded ("*")
    allowed_hosts: list[str] = []

    # 1. Primary host from configured DASHBOARD_BASE_URL
    if settings.DASHBOARD_BASE_URL:
        dash_host = urlparse(settings.DASHBOARD_BASE_URL).hostname
        if dash_host and dash_host not in allowed_hosts:
            allowed_hosts.append(dash_host)

    # 2. Explicitly configured additional allowed hosts
    additional_hosts = getattr(settings, "ADDITIONAL_ALLOWED_HOSTS", None) or []
    for host in additional_hosts:
        cleaned_host = host.strip()
        if cleaned_host and cleaned_host != "*" and cleaned_host not in allowed_hosts:
            allowed_hosts.append(cleaned_host)

    # 3. Development and test hostnames (strictly excluded in production / staging)
    if (
        settings.APP_ENV in ("development", "testing")
        and getattr(settings, "DEBUG", True) is not False
    ):
        dev_hosts = ["localhost", "127.0.0.1", "test", "testserver"]
        for h in dev_hosts:
            if h not in allowed_hosts:
                allowed_hosts.append(h)

    # Fallback to prevent empty host whitelist
    if not allowed_hosts:
        dash_h = (
            urlparse(settings.DASHBOARD_BASE_URL).hostname if settings.DASHBOARD_BASE_URL else None
        )
        allowed_hosts.append(dash_h or "127.0.0.1")

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    trusted_origins = sorted(CSRFOriginValidationMiddleware.get_trusted_origins(settings))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=trusted_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(CSRFOriginValidationMiddleware, settings=settings)

    # Register routers
    app.include_router(health_router)
    app.include_router(api_v1_router)
    app.include_router(web_router)

    return app


app = create_app()
