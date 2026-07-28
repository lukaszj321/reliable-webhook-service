import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

__all__ = [
    "WebhookEndpointNotFoundError",
    "WebhookEventCreationResult",
    "WebhookEventIdempotencyConflictError",
    "WebhookIdempotencyKeyValidationError",
    "create_idempotent_webhook_event_with_delivery_job",
    "create_webhook_event_with_delivery_job",
    "normalize_webhook_idempotency_key",
]

_IDEMPOTENCY_UNIQUE_CONSTRAINT = "uq_webhook_events_endpoint_id_idempotency_key"


class WebhookEndpointNotFoundError(RuntimeError):
    pass


class WebhookEventIdempotencyConflictError(RuntimeError):
    pass


class WebhookIdempotencyKeyValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebhookEventCreationResult:
    event: WebhookEvent
    created: bool


def normalize_webhook_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        return None

    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise WebhookIdempotencyKeyValidationError("Idempotency key must not be empty")
    if len(normalized_key) > 255:
        raise WebhookIdempotencyKeyValidationError("Idempotency key must not exceed 255 characters")
    return normalized_key


def _create_webhook_event_with_delivery_job(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
    idempotency_key: str | None,
) -> WebhookEvent:
    event = WebhookEvent(
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    session.add(event)
    session.flush()

    job = WebhookDeliveryJob(
        event_id=event.id,
        status="pending",
        next_attempt_at=event.created_at,
        attempt_count=0,
    )
    session.add(job)
    session.flush()

    return event


def _find_equivalent_webhook_event(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
    idempotency_key: str,
) -> WebhookEvent | None:
    statement = select(WebhookEvent).where(
        WebhookEvent.endpoint_id == endpoint_id,
        WebhookEvent.idempotency_key == idempotency_key,
        WebhookEvent.event_type == event_type,
        WebhookEvent.payload == payload,
    )
    return session.scalars(statement).one_or_none()


def _find_webhook_event_by_scoped_key(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    idempotency_key: str,
) -> WebhookEvent | None:
    statement = select(WebhookEvent).where(
        WebhookEvent.endpoint_id == endpoint_id,
        WebhookEvent.idempotency_key == idempotency_key,
    )
    return session.scalars(statement).one_or_none()


def _integrity_error_constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


def _resolve_existing_idempotent_event(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
    idempotency_key: str,
) -> WebhookEventCreationResult | None:
    equivalent_event = _find_equivalent_webhook_event(
        session,
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if equivalent_event is not None:
        return WebhookEventCreationResult(event=equivalent_event, created=False)

    scoped_event = _find_webhook_event_by_scoped_key(
        session,
        endpoint_id=endpoint_id,
        idempotency_key=idempotency_key,
    )
    if scoped_event is not None:
        raise WebhookEventIdempotencyConflictError(
            "Idempotency key conflicts with an existing webhook event"
        )
    return None


def create_idempotent_webhook_event_with_delivery_job(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
    idempotency_key: str | None,
) -> WebhookEventCreationResult:
    normalized_key = normalize_webhook_idempotency_key(idempotency_key)

    endpoint = session.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise WebhookEndpointNotFoundError("Webhook endpoint not found")

    if normalized_key is None:
        event = _create_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=None,
        )
        return WebhookEventCreationResult(event=event, created=True)

    existing_result = _resolve_existing_idempotent_event(
        session,
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=normalized_key,
    )
    if existing_result is not None:
        return existing_result

    try:
        with session.begin_nested():
            event = _create_webhook_event_with_delivery_job(
                session,
                endpoint_id=endpoint_id,
                event_type=event_type,
                payload=payload,
                idempotency_key=normalized_key,
            )
    except IntegrityError as error:
        if _integrity_error_constraint_name(error) != _IDEMPOTENCY_UNIQUE_CONSTRAINT:
            raise

        existing_result = _resolve_existing_idempotent_event(
            session,
            endpoint_id=endpoint_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=normalized_key,
        )
        if existing_result is None:
            raise
        return existing_result

    return WebhookEventCreationResult(event=event, created=True)


def create_webhook_event_with_delivery_job(
    session: Session,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    payload: dict[str, JsonValue],
) -> WebhookEvent:
    result = create_idempotent_webhook_event_with_delivery_job(
        session,
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=None,
    )
    return result.event
