from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import WebhookDeliveryJob

__all__ = ["claim_due_webhook_delivery_jobs"]


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be an integer greater than or equal to 1")


def _normalize_claimed_at(claimed_at: datetime) -> datetime:
    if (
        not isinstance(claimed_at, datetime)
        or claimed_at.tzinfo is None
        or claimed_at.utcoffset() is None
    ):
        raise ValueError("claimed_at must be a timezone-aware datetime")

    return claimed_at.astimezone(UTC)


def claim_due_webhook_delivery_jobs(
    session: Session,
    *,
    claimed_at: datetime,
    limit: int,
) -> list[WebhookDeliveryJob]:
    _validate_limit(limit)
    normalized_claimed_at = _normalize_claimed_at(claimed_at)

    statement = (
        select(WebhookDeliveryJob)
        .where(
            WebhookDeliveryJob.status == "pending",
            WebhookDeliveryJob.next_attempt_at.is_not(None),
            WebhookDeliveryJob.next_attempt_at <= normalized_claimed_at,
        )
        .order_by(
            WebhookDeliveryJob.next_attempt_at,
            WebhookDeliveryJob.created_at,
            WebhookDeliveryJob.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(session.scalars(statement).all())

    if not jobs:
        return []

    for job in jobs:
        job.status = "processing"
        job.updated_at = normalized_claimed_at

    session.flush()
    return jobs
