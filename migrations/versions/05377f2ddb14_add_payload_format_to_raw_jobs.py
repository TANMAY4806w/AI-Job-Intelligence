"""add_payload_format_to_raw_jobs

Revision ID: 05377f2ddb14
Revises: 0dcb0808bacc
Create Date: 2026-10-25 02:24:50.581196

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05377f2ddb14"
down_revision: str | Sequence[str] | None = "0dcb0808bacc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "raw_jobs",
        sa.Column(
            "payload_format",
            sa.String(length=20),
            server_default=sa.text("'JSON'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("raw_jobs", "payload_format")
