"""add_notification_deliveries_table

Revision ID: 20a5ee5a034d
Revises: 05377f2ddb14
Create Date: 2026-11-01 15:15:44.339488

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20a5ee5a034d"
down_revision: str | Sequence[str] | None = "05377f2ddb14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create notification_deliveries table, indexes, and unique constraints."""
    op.create_table(
        "notification_deliveries",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            primary_key=True,
        ),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column(
            "channel",
            sa.String(length=32),
            server_default=sa.text("'EMAIL'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "delivery_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "recipient_email",
            "channel",
            name="uq_notification_deliveries_job_recipient_channel",
        ),
    )
    op.create_index(
        "idx_notification_deliveries_job_id",
        "notification_deliveries",
        ["job_id"],
    )
    op.create_index(
        "idx_notification_deliveries_status_created",
        "notification_deliveries",
        ["status", "created_at"],
    )


def downgrade() -> None:
    """Drop notification_deliveries table and associated indexes."""
    op.drop_index(
        "idx_notification_deliveries_status_created",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "idx_notification_deliveries_job_id",
        table_name="notification_deliveries",
    )
    op.drop_table("notification_deliveries")
