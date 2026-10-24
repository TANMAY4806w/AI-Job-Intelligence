"""Database session management and async engine configuration.

Provides a singleton SQLAlchemy AsyncEngine, async_sessionmaker,
and FastAPI-compatible asynchronous session dependencies with explicit
rollback and connection-pool disposal lifecycle handlers.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

# Module-level singletons for connection reuse
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the singleton AsyncEngine instance, initializing it if necessary."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            future=True,
            pool_pre_ping=True,
        )
        logger.debug("Initialized singleton database AsyncEngine.")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async_sessionmaker instance."""
    global _sessionmaker
    if _sessionmaker is None:
        engine = get_engine()
        _sessionmaker = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.debug("Initialized singleton async_sessionmaker.")
    return _sessionmaker


async def dispose_engine() -> None:
    """Cleanly dispose of the async engine and close all pooled connections."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
        logger.info("Database engine disposed cleanly.")


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI-compatible async session dependency.

    Transaction policy:
    - Yields an active AsyncSession.
    - Does NOT automatically commit on successful request completion;
      transactions and commits must be explicitly managed by domain
      services or repository callers.
    - Automatically rolls back uncommitted changes if an unhandled exception occurs.
    - Ensures the session is closed reliably in all execution paths.
    """
    session_factory = get_sessionmaker()
    session = session_factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for programmatic session usage outside FastAPI dependencies."""
    session_factory = get_sessionmaker()
    session = session_factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
