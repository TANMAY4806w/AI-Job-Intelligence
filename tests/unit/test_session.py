"""Unit tests for asynchronous database session management.

Tests session lifecycle, exception rollback behavior, connection disposal,
and FastAPI lifespan integration without requiring a live PostgreSQL instance.
"""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.persistence.session as session_module
from app.api.main import create_app, lifespan
from app.persistence.session import (
    dispose_engine,
    get_db_session,
    get_engine,
    get_sessionmaker,
    session_scope,
)


@pytest.fixture(autouse=True)
def reset_session_singletons() -> None:
    """Ensure session module singletons are clean before and after each unit test."""
    session_module._engine = None
    session_module._sessionmaker = None
    yield
    session_module._engine = None
    session_module._sessionmaker = None


@pytest.mark.asyncio
async def test_get_db_session_yields_session_and_closes_without_auto_commit() -> None:
    """Verify get_db_session yields session, closes it, and never commits automatically."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_factory = MagicMock(return_value=mock_session)

    with patch.object(session_module, "get_sessionmaker", return_value=mock_factory):
        generator: AsyncGenerator[AsyncSession, None] = get_db_session()
        yielded_session = await anext(generator)

        assert yielded_session is mock_session
        assert mock_session.commit.call_count == 0

        # Complete the generator execution
        with pytest.raises(StopAsyncIteration):
            await anext(generator)

        # Confirm rollback was not called on clean exit
        assert mock_session.rollback.call_count == 0
        # Confirm commit was not called on clean exit
        assert mock_session.commit.call_count == 0
        # Confirm session was closed
        assert mock_session.close.call_count == 1


@pytest.mark.asyncio
async def test_get_db_session_rolls_back_and_closes_on_exception() -> None:
    """Verify get_db_session rolls back uncommitted changes and closes on unhandled errors."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_factory = MagicMock(return_value=mock_session)

    with patch.object(session_module, "get_sessionmaker", return_value=mock_factory):
        generator: AsyncGenerator[AsyncSession, None] = get_db_session()
        yielded_session = await anext(generator)
        assert yielded_session is mock_session

        # Throw exception into generator
        with pytest.raises(RuntimeError, match="Simulated application error"):
            await generator.athrow(RuntimeError("Simulated application error"))

        # Verify rollback was invoked
        assert mock_session.rollback.call_count == 1
        # Verify close was invoked in finally block
        assert mock_session.close.call_count == 1
        # Verify commit was never invoked
        assert mock_session.commit.call_count == 0


@pytest.mark.asyncio
async def test_session_scope_context_manager() -> None:
    """Verify programmatic session_scope context manager behavior."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_factory = MagicMock(return_value=mock_session)

    with patch.object(session_module, "get_sessionmaker", return_value=mock_factory):
        async with session_scope() as session:
            assert session is mock_session

        assert mock_session.close.call_count == 1
        assert mock_session.commit.call_count == 0
        assert mock_session.rollback.call_count == 0


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_exception() -> None:
    """Verify programmatic session_scope rolls back on unhandled error."""
    mock_session = AsyncMock(spec=AsyncSession)
    mock_factory = MagicMock(return_value=mock_session)

    with patch.object(session_module, "get_sessionmaker", return_value=mock_factory):
        with pytest.raises(ValueError, match="Scope failure"):
            async with session_scope():
                raise ValueError("Scope failure")

        assert mock_session.rollback.call_count == 1
        assert mock_session.close.call_count == 1


def test_engine_and_sessionmaker_singleton_reuse() -> None:
    """Verify that get_engine and get_sessionmaker reuse singleton instances."""
    with patch("app.persistence.session.create_async_engine") as mock_create_engine:
        mock_engine = MagicMock(spec=AsyncEngine)
        mock_create_engine.return_value = mock_engine

        engine1 = get_engine()
        engine2 = get_engine()

        assert engine1 is engine2
        assert mock_create_engine.call_count == 1

        sm1 = get_sessionmaker()
        sm2 = get_sessionmaker()

        assert sm1 is sm2
        assert isinstance(sm1, async_sessionmaker)


@pytest.mark.asyncio
async def test_dispose_engine_cleans_up_singletons() -> None:
    """Verify dispose_engine disposes the engine and resets singletons."""
    mock_engine = AsyncMock(spec=AsyncEngine)
    session_module._engine = mock_engine
    session_module._sessionmaker = MagicMock()

    await dispose_engine()

    assert mock_engine.dispose.call_count == 1
    assert session_module._engine is None
    assert session_module._sessionmaker is None


@pytest.mark.asyncio
async def test_fastapi_lifespan_disposes_engine_on_shutdown() -> None:
    """Verify FastAPI lifespan cleans up database engine on shutdown."""
    app = create_app()

    with patch("app.api.main.dispose_engine", new_callable=AsyncMock) as mock_dispose:
        async with lifespan(app):
            # Startup phase executed
            assert mock_dispose.call_count == 0

        # Post-shutdown phase executed
        assert mock_dispose.call_count == 1
