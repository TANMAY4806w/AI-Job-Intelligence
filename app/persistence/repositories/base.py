"""Base asynchronous repository class."""

from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository:
    """Base persistence repository bound to an active AsyncSession.

    Repositories encapsulate relational database queries and operations.
    Transaction lifecycle (commit, rollback) is controlled by the caller
    or FastAPI dependency/session_scope, not by repository methods.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Initialize the repository with an active asynchronous SQLAlchemy session."""
        self.session = session
