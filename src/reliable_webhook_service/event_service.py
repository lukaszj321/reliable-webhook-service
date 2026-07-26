import uuid

from sqlalchemy.orm import Session

from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

__all__ = [
    "WebhookEndpointNotFoundError",
    "create_webhook_event_with_delivery_job",
]


class WebhookEndpointNotFoundError(RuntimeError):
    pass


def create_webhook_event_with_delivery_job(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
) -> WebhookEvent:
    endpoint = session.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise WebhookEndpointNotFoundError("Webhook endpoint not found")

    event = WebhookEvent(
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
    )
    session.add(event)
    session.flush()

    job = WebhookDeliveryJob(
        event_id=event.id,
        status="pending",
        next_attempt_at=event.created_at,
    )
    session.add(job)
    session.flush()

    return event
