import uuid
from datetime import datetime

import pytest

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)


@pytest.mark.parametrize(
    ("outcome", "response_status_code", "error_message", "duration_ms"),
    [
        ("succeeded", 200, None, 37),
        ("failed", 503, "HTTP 503 Service Unavailable", 85),
        ("failed", None, "Connection timed out", 5000),
    ],
    ids=[
        "successful-http-response",
        "failed-http-response",
        "network-failure",
    ],
)
def test_persist_webhook_delivery_attempt(
    outcome: str,
    response_status_code: int | None,
    error_message: str | None,
    duration_ms: int,
) -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "delivery": {
            "test": True,
            "optional": None,
        },
    }
    target_url = f"https://example.com/delivery-attempt/{marker}"

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery attempt persistence {marker}",
                target_url=target_url,
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.attempt.test",
                payload=payload,
            )
            session.add(event)
            session.flush()
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            attempt = WebhookDeliveryAttempt(
                event_id=event_id,
                attempt_number=1,
                outcome=outcome,
                target_url=target_url,
                response_status_code=response_status_code,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            attempt_id = attempt.id

            assert isinstance(attempt_id, uuid.UUID)
            assert attempt.event_id == event_id
            assert attempt.attempt_number == 1
            assert attempt.outcome == outcome
            assert attempt.target_url == target_url
            assert attempt.response_status_code == response_status_code
            assert attempt.error_message == error_message
            assert attempt.duration_ms == duration_ms
            assert isinstance(attempt.attempted_at, datetime)
            assert attempt.attempted_at.tzinfo is not None
            assert attempt.attempted_at.utcoffset() is not None

        with SessionFactory() as session:
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
            stored_event = session.get(WebhookEvent, event_id)
            stored_attempt = session.get(WebhookDeliveryAttempt, attempt_id)

            assert stored_endpoint is not None
            assert stored_event is not None
            assert stored_attempt is not None

            assert stored_endpoint.target_url == target_url
            assert stored_endpoint.is_active is True

            assert stored_event.endpoint_id == endpoint_id
            assert stored_event.event_type == "delivery.attempt.test"
            assert stored_event.payload == payload

            assert stored_attempt.id == attempt_id
            assert stored_attempt.event_id == event_id
            assert stored_attempt.attempt_number == 1
            assert stored_attempt.outcome == outcome
            assert stored_attempt.target_url == target_url
            assert stored_attempt.response_status_code == response_status_code
            assert stored_attempt.error_message == error_message
            assert stored_attempt.duration_ms == duration_ms
            assert isinstance(stored_attempt.attempted_at, datetime)
            assert stored_attempt.attempted_at.tzinfo is not None
            assert stored_attempt.attempted_at.utcoffset() is not None
    finally:
        with SessionFactory() as session:
            if attempt_id is not None:
                stored_attempt = session.get(WebhookDeliveryAttempt, attempt_id)
                if stored_attempt is not None:
                    session.delete(stored_attempt)
            session.commit()

            if event_id is not None:
                stored_event = session.get(WebhookEvent, event_id)
                if stored_event is not None:
                    session.delete(stored_event)
            session.commit()

            if endpoint_id is not None:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
            session.commit()

    assert attempt_id is not None
    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None
