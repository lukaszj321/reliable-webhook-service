from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import WebhookDeliveryJob

__all__ = [
    "WebhookDeliveryJobRecoveryResult",
    "recover_stale_webhook_delivery_jobs",
]


@dataclass(frozen=True, slots=True)
class WebhookDeliveryJobRecoveryResult:
    recovered_job_ids: tuple[UUID, ...]

    @property
    def recovered_count(self) -> int:
        return len(self.recovered_job_ids)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be an integer greater than or equal to 1")


def _normalize_datetime(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")

    return value.astimezone(UTC)


def recover_stale_webhook_delivery_jobs(
    session: Session,
    *,
    stale_before: datetime,
    recovered_at: datetime,
    limit: int,
) -> WebhookDeliveryJobRecoveryResult:
    _validate_limit(limit)
    normalized_stale_before = _normalize_datetime(stale_before, name="stale_before")
    normalized_recovered_at = _normalize_datetime(recovered_at, name="recovered_at")
    if normalized_recovered_at < normalized_stale_before:
        raise ValueError("recovered_at must be greater than or equal to stale_before")

    statement = (
        select(WebhookDeliveryJob)
        .where(
            WebhookDeliveryJob.status == "processing",
            WebhookDeliveryJob.updated_at <= normalized_stale_before,
        )
        .order_by(
            WebhookDeliveryJob.updated_at,
            WebhookDeliveryJob.created_at,
            WebhookDeliveryJob.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list(session.scalars(statement).all())

    if not jobs:
        return WebhookDeliveryJobRecoveryResult(recovered_job_ids=())

    for job in jobs:
        job.status = "pending"
        job.next_attempt_at = normalized_recovered_at
        job.updated_at = normalized_recovered_at

    recovered_job_ids = tuple(job.id for job in jobs)
    session.flush()
    return WebhookDeliveryJobRecoveryResult(recovered_job_ids=recovered_job_ids)
