"""add delivery job attempt count

Revision ID: c4f7a9e21b6d
Revises: 9970fa5ecbab
Create Date: 2026-07-28 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f7a9e21b6d"
down_revision: str | Sequence[str] | None = "9970fa5ecbab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ATTEMPT_COUNT_CONSTRAINT = "ck_webhook_delivery_jobs_attempt_count_non_negative"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "webhook_delivery_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE webhook_delivery_jobs AS jobs
            SET attempt_count = (
                SELECT COUNT(*)
                FROM webhook_delivery_attempts AS attempts
                WHERE attempts.event_id = jobs.event_id
            )
            """
        )
    )
    op.alter_column(
        "webhook_delivery_jobs",
        "attempt_count",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )
    op.create_check_constraint(
        _ATTEMPT_COUNT_CONSTRAINT,
        "webhook_delivery_jobs",
        "attempt_count >= 0",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        _ATTEMPT_COUNT_CONSTRAINT,
        "webhook_delivery_jobs",
        type_="check",
    )
    op.drop_column("webhook_delivery_jobs", "attempt_count")
