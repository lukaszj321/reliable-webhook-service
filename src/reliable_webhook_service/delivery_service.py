import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_http import WebhookHttpClient
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
    "execute_webhook_delivery",
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


def execute_webhook_delivery(
    session: Session,
    *,
    event_id: uuid.UUID,
    http_client: WebhookHttpClient,
    timeout_seconds: float,
    utc_now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
) -> WebhookDeliveryAttempt:
    prepared = prepare_webhook_delivery(
        session,
        event_id=event_id,
    )

    attempted_at = utc_now()
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError("utc_now must return a timezone-aware datetime")

    started_ns = monotonic_ns()
    outcome: str
    response_status_code: int | None
    error_message: str | None

    try:
        response = http_client.post_json(
            target_url=prepared.target_url,
            payload=prepared.payload,
            timeout_seconds=timeout_seconds,
        )
    except httpx2.RequestError as error:
        finished_ns = monotonic_ns()
        outcome = "failed"
        response_status_code = None
        if isinstance(error, httpx2.TimeoutException):
            error_message = "Webhook request timed out"
        else:
            error_message = f"Webhook request failed: {type(error).__name__}"
    else:
        finished_ns = monotonic_ns()
        response_status_code = response.status_code
        if 200 <= response.status_code <= 299:
            outcome = "succeeded"
            error_message = None
        else:
            outcome = "failed"
            error_message = f"HTTP response returned status {response.status_code}"

    duration_ms = max(0, (finished_ns - started_ns) // 1_000_000)
    attempt = WebhookDeliveryAttempt(
        event_id=prepared.event_id,
        attempt_number=prepared.attempt_number,
        outcome=outcome,
        target_url=prepared.target_url,
        response_status_code=response_status_code,
        error_message=error_message,
        duration_ms=duration_ms,
        attempted_at=attempted_at,
    )
    session.add(attempt)

    try:
        session.commit()
        session.refresh(attempt)
    except Exception:
        session.rollback()
        raise

    return attempt
