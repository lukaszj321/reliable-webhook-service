import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)

__all__ = [
    "InactiveWebhookEndpointError",
    "PreparedWebhookDelivery",
    "WebhookEndpointNotFoundError",
    "WebhookEventNotFoundError",
    "prepare_webhook_delivery",
]


@dataclass(frozen=True, slots=True)
class PreparedWebhookDelivery:
    event_id: uuid.UUID
    target_url: str
    payload: dict[str, JsonValue]
    attempt_number: int


class WebhookEventNotFoundError(RuntimeError):
    pass


class WebhookEndpointNotFoundError(RuntimeError):
    pass


class InactiveWebhookEndpointError(RuntimeError):
    pass


def prepare_webhook_delivery(
    session: Session,
    *,
    event_id: uuid.UUID,
) -> PreparedWebhookDelivery:
    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise WebhookEventNotFoundError("Webhook event not found")

    endpoint = session.get(WebhookEndpoint, event.endpoint_id)
    if endpoint is None:
        raise WebhookEndpointNotFoundError("Webhook endpoint not found")

    if endpoint.is_active is False:
        raise InactiveWebhookEndpointError("Webhook endpoint is inactive")

    maximum_attempt_number = session.scalar(
        select(func.max(WebhookDeliveryAttempt.attempt_number)).where(
            WebhookDeliveryAttempt.event_id == event_id
        )
    )
    next_attempt_number = 1 if maximum_attempt_number is None else maximum_attempt_number + 1

    return PreparedWebhookDelivery(
        event_id=event.id,
        target_url=endpoint.target_url,
        payload=event.payload,
        attempt_number=next_attempt_number,
    )
