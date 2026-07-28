"""add webhook event idempotency key

Revision ID: 9970fa5ecbab
Revises: 200628ca5044
Create Date: 2026-07-28 16:22:40.576917

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9970fa5ecbab"
down_revision: str | Sequence[str] | None = "200628ca5044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "webhook_events",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "uq_webhook_events_endpoint_id_idempotency_key",
        "webhook_events",
        ["endpoint_id", "idempotency_key"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "uq_webhook_events_endpoint_id_idempotency_key",
        "webhook_events",
        type_="unique",
    )
    op.drop_column("webhook_events", "idempotency_key")
