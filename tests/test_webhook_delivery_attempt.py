import uuid
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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


def test_reject_delivery_attempt_for_missing_event() -> None:
    missing_event_id = uuid.uuid4()
    attempt_id = uuid.uuid4()

    with SessionFactory() as session:
        attempt = WebhookDeliveryAttempt(
            id=attempt_id,
            event_id=missing_event_id,
            attempt_number=1,
            outcome="failed",
            target_url="https://example.com/missing-event",
            response_status_code=None,
            error_message="Referenced event does not exist",
            duration_ms=1,
        )
        session.add(attempt)

        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        attempt_ids = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())
        assert attempt_id not in attempt_ids
        assert session.get(WebhookEvent, missing_event_id) is None

    with SessionFactory() as session:
        assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        assert session.get(WebhookEvent, missing_event_id) is None


@pytest.mark.parametrize(
    (
        "attempt_number",
        "outcome",
        "response_status_code",
        "duration_ms",
    ),
    [
        (0, "failed", 500, 1),
        (-1, "failed", 500, 1),
        (1, "pending", 500, 1),
        (1, "failed", 99, 1),
        (1, "failed", 600, 1),
        (1, "failed", 500, -1),
    ],
    ids=[
        "zero-attempt-number",
        "negative-attempt-number",
        "invalid-outcome",
        "status-code-below-range",
        "status-code-above-range",
        "negative-duration",
    ],
)
def test_reject_invalid_webhook_delivery_attempt(
    attempt_number: int,
    outcome: str,
    response_status_code: int,
    duration_ms: int,
) -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    attempt_id = uuid.uuid4()
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "constraint": True,
    }
    target_url = f"https://example.com/delivery-constraint/{marker}"

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery attempt constraint {marker}",
                target_url=target_url,
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.constraint.test",
                payload=payload,
            )
            session.add(event)
            session.commit()
            session.refresh(event)
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            attempt = WebhookDeliveryAttempt(
                id=attempt_id,
                event_id=event_id,
                attempt_number=attempt_number,
                outcome=outcome,
                target_url=target_url,
                response_status_code=response_status_code,
                error_message="Expected constraint violation",
                duration_ms=duration_ms,
            )
            session.add(attempt)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            attempt_ids = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())
            assert attempt_id not in attempt_ids
            assert session.get(WebhookEndpoint, endpoint_id) is not None
            assert session.get(WebhookEvent, event_id) is not None
    finally:
        with SessionFactory() as session:
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

    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


def test_reject_duplicate_delivery_attempt_number() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    first_attempt_id: uuid.UUID | None = None
    duplicate_attempt_id = uuid.uuid4()
    target_url = f"https://example.com/delivery-duplicate/{marker}"
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "duplicate_test": True,
    }

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery attempt duplicate {marker}",
                target_url=target_url,
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.duplicate.test",
                payload=payload,
            )
            session.add(event)
            session.flush()
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            first_attempt = WebhookDeliveryAttempt(
                event_id=event_id,
                attempt_number=1,
                outcome="failed",
                target_url=target_url,
                response_status_code=503,
                error_message="First attempt",
                duration_ms=25,
            )
            session.add(first_attempt)
            session.commit()
            session.refresh(first_attempt)
            first_attempt_id = first_attempt.id

            assert isinstance(first_attempt_id, uuid.UUID)

            duplicate_attempt = WebhookDeliveryAttempt(
                id=duplicate_attempt_id,
                event_id=event_id,
                attempt_number=1,
                outcome="failed",
                target_url=target_url,
                response_status_code=500,
                error_message="Duplicate attempt number",
                duration_ms=30,
            )
            session.add(duplicate_attempt)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            attempt_ids = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())
            assert first_attempt_id in attempt_ids
            assert duplicate_attempt_id not in attempt_ids

            matching_attempt_ids = session.scalars(
                select(WebhookDeliveryAttempt.id).where(
                    WebhookDeliveryAttempt.event_id == event_id,
                    WebhookDeliveryAttempt.attempt_number == 1,
                )
            ).all()
            assert matching_attempt_ids == [first_attempt_id]

        with SessionFactory() as session:
            stored_first_attempt = session.get(WebhookDeliveryAttempt, first_attempt_id)
            stored_duplicate_attempt = session.get(
                WebhookDeliveryAttempt,
                duplicate_attempt_id,
            )
            stored_event = session.get(WebhookEvent, event_id)
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)

            assert stored_first_attempt is not None
            assert stored_duplicate_attempt is None
            assert stored_first_attempt.attempt_number == 1
            assert stored_first_attempt.error_message == "First attempt"
            assert stored_event is not None
            assert stored_endpoint is not None
    finally:
        with SessionFactory() as session:
            stored_duplicate_attempt = session.get(
                WebhookDeliveryAttempt,
                duplicate_attempt_id,
            )
            if stored_duplicate_attempt is not None:
                session.delete(stored_duplicate_attempt)
            session.commit()

            if first_attempt_id is not None:
                stored_first_attempt = session.get(
                    WebhookDeliveryAttempt,
                    first_attempt_id,
                )
                if stored_first_attempt is not None:
                    session.delete(stored_first_attempt)
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

    assert first_attempt_id is not None
    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryAttempt, duplicate_attempt_id) is None
        assert session.get(WebhookDeliveryAttempt, first_attempt_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


def test_webhook_event_delete_blocked_by_delivery_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    attempt_id: uuid.UUID | None = None
    target_url = f"https://example.com/delivery-fk/{marker}"
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "foreign_key_test": True,
    }

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery attempt foreign key {marker}",
                target_url=target_url,
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.foreign-key.test",
                payload=payload,
            )
            session.add(event)
            session.flush()
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            attempt = WebhookDeliveryAttempt(
                event_id=event_id,
                attempt_number=1,
                outcome="failed",
                target_url=target_url,
                response_status_code=None,
                error_message="Foreign key delete behavior",
                duration_ms=10,
            )
            session.add(attempt)
            session.commit()
            session.refresh(attempt)
            attempt_id = attempt.id

            assert isinstance(attempt_id, uuid.UUID)

        with SessionFactory() as session:
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
            stored_event = session.get(WebhookEvent, event_id)
            stored_attempt = session.get(WebhookDeliveryAttempt, attempt_id)

            assert stored_endpoint is not None
            assert stored_event is not None
            assert stored_attempt is not None

            session.delete(stored_event)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            attempt_ids = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())
            assert attempt_id in attempt_ids
            assert session.get(WebhookDeliveryAttempt, attempt_id) is not None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

            stored_attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            assert stored_attempt is not None
            session.delete(stored_attempt)
            session.commit()

            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

            stored_event = session.get(WebhookEvent, event_id)
            assert stored_event is not None
            session.delete(stored_event)
            session.commit()

            assert session.get(WebhookEvent, event_id) is None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
            assert stored_endpoint is not None
            session.delete(stored_endpoint)
            session.commit()

        with SessionFactory() as session:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
            assert session.get(WebhookEvent, event_id) is None
            assert session.get(WebhookEndpoint, endpoint_id) is None
    finally:
        with SessionFactory() as session:
            session.rollback()

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
