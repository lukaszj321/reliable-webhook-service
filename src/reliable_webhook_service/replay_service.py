import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import (
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

__all__ = [
    "WebhookReplayDeliveryJobNotFoundError",
    "WebhookReplayDeliveryJobNotReplayableError",
    "WebhookReplayEndpointInactiveError",
    "WebhookReplayEndpointNotFoundError",
    "WebhookReplayEventNotFoundError",
    "WebhookReplayResult",
    "replay_webhook_event",
]


@dataclass(frozen=True, slots=True)
class WebhookReplayResult:
    event_id: uuid.UUID
    delivery_job_id: uuid.UUID
    status: str
    next_attempt_at: datetime


class WebhookReplayEventNotFoundError(RuntimeError):
    pass


class WebhookReplayEndpointNotFoundError(RuntimeError):
    pass


class WebhookReplayEndpointInactiveError(RuntimeError):
    pass


class WebhookReplayDeliveryJobNotFoundError(RuntimeError):
    pass


class WebhookReplayDeliveryJobNotReplayableError(RuntimeError):
    pass


def _normalize_replayed_at(replayed_at: datetime) -> datetime:
    if (
        not isinstance(replayed_at, datetime)
        or replayed_at.tzinfo is None
        or replayed_at.utcoffset() is None
    ):
        raise ValueError("replayed_at must be a timezone-aware datetime")

    return replayed_at.astimezone(UTC)


def replay_webhook_event(
    session: Session,
    *,
    event_id: uuid.UUID,
    replayed_at: datetime,
) -> WebhookReplayResult:
    normalized_replayed_at = _normalize_replayed_at(replayed_at)

    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise WebhookReplayEventNotFoundError("Webhook event not found")

    endpoint = session.get(WebhookEndpoint, event.endpoint_id)
    if endpoint is None:
        raise WebhookReplayEndpointNotFoundError("Webhook endpoint not found")
    if not endpoint.is_active:
        raise WebhookReplayEndpointInactiveError("Webhook endpoint is inactive")

    statement = (
        select(WebhookDeliveryJob).where(WebhookDeliveryJob.event_id == event.id).with_for_update()
    )
    job = session.scalar(statement)
    if job is None:
        raise WebhookReplayDeliveryJobNotFoundError("Webhook delivery job not found")
    if job.status not in {"succeeded", "dead_letter"}:
        raise WebhookReplayDeliveryJobNotReplayableError("Webhook delivery job is not replayable")

    job.status = "pending"
    job.next_attempt_at = normalized_replayed_at
    job.updated_at = normalized_replayed_at
    job.attempt_count = 0
    session.flush()

    return WebhookReplayResult(
        event_id=event.id,
        delivery_job_id=job.id,
        status=job.status,
        next_attempt_at=normalized_replayed_at,
    )
