"""create webhook delivery jobs

Revision ID: 200628ca5044
Revises: 10f4dd620e97
Create Date: 2026-07-25 16:33:36.285318

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "200628ca5044"
down_revision: str | Sequence[str] | None = "10f4dd620e97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "webhook_delivery_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            (
                "(status IN ('pending', 'processing') AND next_attempt_at IS NOT NULL) "
                "OR (status IN ('succeeded', 'dead_letter') AND next_attempt_at IS NULL)"
            ),
            name="ck_webhook_delivery_jobs_status_next_attempt_at",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'dead_letter')",
            name="ck_webhook_delivery_jobs_status",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["webhook_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_webhook_delivery_jobs_event_id",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("webhook_delivery_jobs")
