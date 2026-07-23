import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.main import app
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)

_MARKER_PREFIX = "delivery-attempt-listing-api-test"
_RESPONSE_FIELDS = {
    "id",
    "event_id",
    "attempt_number",
    "outcome",
    "target_url",
    "response_status_code",
    "error_message",
    "duration_ms",
    "attempted_at",
}


def _create_endpoint_and_events(
    endpoint_id: uuid.UUID,
    event_ids: list[uuid.UUID],
    marker: uuid.UUID,
) -> None:
    marker_text = f"{_MARKER_PREFIX}-{marker}"

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=marker_text,
                target_url=f"https://example.com/{marker_text}/endpoint",
                is_active=True,
            )
        )
        session.flush()

        for position, event_id in enumerate(event_ids, start=1):
            session.add(
                WebhookEvent(
                    id=event_id,
                    endpoint_id=endpoint_id,
                    event_type=f"delivery.attempt.listing.{position}",
                    payload={"marker": marker_text, "position": position},
                )
            )

        session.commit()


def _cleanup_records(
    *,
    attempt_ids: list[uuid.UUID],
    event_ids: list[uuid.UUID],
    endpoint_id: uuid.UUID,
) -> None:
    with SessionFactory() as session:
        for attempt_id in attempt_ids:
            attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            if attempt is not None:
                session.delete(attempt)
        session.commit()

        for event_id in event_ids:
            event = session.get(WebhookEvent, event_id)
            if event is not None:
                session.delete(event)
        session.commit()

        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is not None:
            session.delete(endpoint)
        session.commit()

    with SessionFactory() as session:
        for attempt_id in attempt_ids:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        for event_id in event_ids:
            assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


def _record_counts() -> tuple[int, int, int]:
    with SessionFactory() as session:
        endpoint_count = len(session.scalars(select(WebhookEndpoint.id)).all())
        event_count = len(session.scalars(select(WebhookEvent.id)).all())
        attempt_count = len(session.scalars(select(WebhookDeliveryAttempt.id)).all())

    return endpoint_count, event_count, attempt_count


def _endpoint_values(endpoint: WebhookEndpoint) -> tuple[object, ...]:
    return (
        endpoint.id,
        endpoint.name,
        endpoint.target_url,
        endpoint.is_active,
        endpoint.created_at,
        endpoint.updated_at,
    )


def _event_values(event: WebhookEvent) -> tuple[object, ...]:
    return (
        event.id,
        event.endpoint_id,
        event.event_type,
        event.payload,
        event.created_at,
    )


def _attempt_values(attempt: WebhookDeliveryAttempt) -> tuple[object, ...]:
    return (
        attempt.id,
        attempt.event_id,
        attempt.attempt_number,
        attempt.outcome,
        attempt.target_url,
        attempt.response_status_code,
        attempt.error_message,
        attempt.duration_ms,
        attempt.attempted_at,
    )


def test_list_delivery_attempts_returns_empty_list() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()

    try:
        _create_endpoint_and_events(endpoint_id, [event_id], marker)

        with TestClient(app) as client:
            response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 200
        assert response.json() == []

        with SessionFactory() as session:
            attempt_ids = session.scalars(
                select(WebhookDeliveryAttempt.id).where(WebhookDeliveryAttempt.event_id == event_id)
            ).all()
            assert attempt_ids == []
    finally:
        _cleanup_records(
            attempt_ids=[],
            event_ids=[event_id],
            endpoint_id=endpoint_id,
        )


def test_list_delivery_attempts_returns_ordered_attempts() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids = [uuid.uuid4() for _ in range(3)]
    marker_text = f"{_MARKER_PREFIX}-{marker}"
    attempts = [
        WebhookDeliveryAttempt(
            id=attempt_ids[0],
            event_id=event_id,
            attempt_number=3,
            outcome="failed",
            target_url=f"https://example.com/{marker_text}/attempt-3",
            response_status_code=None,
            error_message="Connection timed out",
            duration_ms=3000,
            attempted_at=datetime(2026, 1, 3, 12, 0, tzinfo=UTC),
        ),
        WebhookDeliveryAttempt(
            id=attempt_ids[1],
            event_id=event_id,
            attempt_number=1,
            outcome="succeeded",
            target_url=f"https://example.com/{marker_text}/attempt-1",
            response_status_code=200,
            error_message=None,
            duration_ms=125,
            attempted_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        ),
        WebhookDeliveryAttempt(
            id=attempt_ids[2],
            event_id=event_id,
            attempt_number=2,
            outcome="failed",
            target_url=f"https://example.com/{marker_text}/attempt-2",
            response_status_code=503,
            error_message="Service unavailable",
            duration_ms=480,
            attempted_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
        ),
    ]

    try:
        _create_endpoint_and_events(endpoint_id, [event_id], marker)
        with SessionFactory() as session:
            session.add_all(attempts)
            session.commit()

        with TestClient(app) as client:
            response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 200
        response_body = response.json()
        assert len(response_body) == 3
        assert [item["attempt_number"] for item in response_body] == [1, 2, 3]

        expected_attempts = {attempt.attempt_number: attempt for attempt in attempts}
        for item in response_body:
            assert set(item) == _RESPONSE_FIELDS
            expected = expected_attempts[item["attempt_number"]]
            assert isinstance(item["id"], str)
            assert uuid.UUID(item["id"]) == expected.id
            assert isinstance(item["event_id"], str)
            assert item["event_id"] == str(event_id)
            assert item["outcome"] == expected.outcome
            assert item["target_url"] == expected.target_url
            assert item["response_status_code"] == expected.response_status_code
            assert item["error_message"] == expected.error_message
            assert item["duration_ms"] == expected.duration_ms

            attempted_at = datetime.fromisoformat(item["attempted_at"].replace("Z", "+00:00"))
            assert attempted_at.tzinfo is not None
            assert attempted_at.utcoffset() is not None
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_id=endpoint_id,
        )


def test_list_delivery_attempts_isolates_events() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    first_event_id = uuid.uuid4()
    second_event_id = uuid.uuid4()
    first_attempt_id = uuid.uuid4()
    second_attempt_id = uuid.uuid4()
    marker_text = f"{_MARKER_PREFIX}-{marker}"

    try:
        _create_endpoint_and_events(
            endpoint_id,
            [first_event_id, second_event_id],
            marker,
        )
        with SessionFactory() as session:
            session.add_all(
                [
                    WebhookDeliveryAttempt(
                        id=first_attempt_id,
                        event_id=first_event_id,
                        attempt_number=1,
                        outcome="succeeded",
                        target_url=f"https://example.com/{marker_text}/first-event",
                        response_status_code=200,
                        error_message=None,
                        duration_ms=30,
                        attempted_at=datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
                    ),
                    WebhookDeliveryAttempt(
                        id=second_attempt_id,
                        event_id=second_event_id,
                        attempt_number=1,
                        outcome="failed",
                        target_url=f"https://example.com/{marker_text}/second-event",
                        response_status_code=500,
                        error_message="Other event failure",
                        duration_ms=45,
                        attempted_at=datetime(2026, 2, 1, 12, 1, tzinfo=UTC),
                    ),
                ]
            )
            session.commit()

        with TestClient(app) as client:
            response = client.get(f"/webhook-events/{first_event_id}/delivery-attempts")

        assert response.status_code == 200
        response_body = response.json()
        assert len(response_body) == 1
        assert [item["id"] for item in response_body] == [str(first_attempt_id)]
        assert str(second_attempt_id) not in {item["id"] for item in response_body}
        assert all(item["event_id"] == str(first_event_id) for item in response_body)
    finally:
        _cleanup_records(
            attempt_ids=[first_attempt_id, second_attempt_id],
            event_ids=[first_event_id, second_event_id],
            endpoint_id=endpoint_id,
        )


def test_list_delivery_attempts_returns_404_for_missing_event() -> None:
    missing_event_id = uuid.uuid4()

    try:
        with SessionFactory() as session:
            assert session.get(WebhookEvent, missing_event_id) is None
            assert (
                session.scalars(
                    select(WebhookDeliveryAttempt.id).where(
                        WebhookDeliveryAttempt.event_id == missing_event_id
                    )
                ).all()
                == []
            )

        with TestClient(app) as client:
            response = client.get(f"/webhook-events/{missing_event_id}/delivery-attempts")

        assert response.status_code == 404
        assert response.json() == {"detail": "Webhook event not found"}
    finally:
        with SessionFactory() as session:
            assert session.get(WebhookEvent, missing_event_id) is None
            assert (
                session.scalars(
                    select(WebhookDeliveryAttempt.id).where(
                        WebhookDeliveryAttempt.event_id == missing_event_id
                    )
                ).all()
                == []
            )


def test_list_delivery_attempts_is_read_only() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids = [uuid.uuid4(), uuid.uuid4()]
    marker_text = f"{_MARKER_PREFIX}-{marker}"

    try:
        _create_endpoint_and_events(endpoint_id, [event_id], marker)
        with SessionFactory() as session:
            session.add_all(
                [
                    WebhookDeliveryAttempt(
                        id=attempt_ids[0],
                        event_id=event_id,
                        attempt_number=1,
                        outcome="failed",
                        target_url=f"https://example.com/{marker_text}/read-only-1",
                        response_status_code=502,
                        error_message="First read-only attempt",
                        duration_ms=80,
                        attempted_at=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
                    ),
                    WebhookDeliveryAttempt(
                        id=attempt_ids[1],
                        event_id=event_id,
                        attempt_number=2,
                        outcome="succeeded",
                        target_url=f"https://example.com/{marker_text}/read-only-2",
                        response_status_code=204,
                        error_message=None,
                        duration_ms=95,
                        attempted_at=datetime(2026, 3, 1, 12, 1, tzinfo=UTC),
                    ),
                ]
            )
            session.commit()

        counts_before = _record_counts()
        with SessionFactory() as session:
            endpoint_before = session.get(WebhookEndpoint, endpoint_id)
            event_before = session.get(WebhookEvent, event_id)
            attempts_before = [
                session.get(WebhookDeliveryAttempt, attempt_id) for attempt_id in attempt_ids
            ]

            assert endpoint_before is not None
            assert event_before is not None
            assert all(attempt is not None for attempt in attempts_before)

            endpoint_snapshot = _endpoint_values(endpoint_before)
            event_snapshot = _event_values(event_before)
            attempt_snapshots = [
                _attempt_values(attempt) for attempt in attempts_before if attempt is not None
            ]
            attempt_ids_before = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())

        with TestClient(app) as client:
            response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 200
        assert len(response.json()) == 2
        assert _record_counts() == counts_before

        with SessionFactory() as session:
            endpoint_after = session.get(WebhookEndpoint, endpoint_id)
            event_after = session.get(WebhookEvent, event_id)
            attempts_after = [
                session.get(WebhookDeliveryAttempt, attempt_id) for attempt_id in attempt_ids
            ]

            assert endpoint_after is not None
            assert event_after is not None
            assert all(attempt is not None for attempt in attempts_after)
            assert _endpoint_values(endpoint_after) == endpoint_snapshot
            assert _event_values(event_after) == event_snapshot
            assert [
                _attempt_values(attempt) for attempt in attempts_after if attempt is not None
            ] == attempt_snapshots
            assert (
                set(session.scalars(select(WebhookDeliveryAttempt.id)).all()) == attempt_ids_before
            )
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_id=endpoint_id,
        )
