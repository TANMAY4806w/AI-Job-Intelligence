"""add_raw_body_to_raw_jobs

Revision ID: 0dcb0808bacc
Revises: 091bce25752c
Create Date: 2026-10-25 01:58:42.469397

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0dcb0808bacc"
down_revision: str | Sequence[str] | None = "091bce25752c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("raw_jobs", sa.Column("raw_body", sa.LargeBinary(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("raw_jobs", "raw_body")
