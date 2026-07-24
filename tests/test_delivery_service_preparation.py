import uuid
from unittest.mock import Mock, call

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_service import (
    InactiveWebhookEndpointError,
    PreparedWebhookDelivery,
    WebhookEndpointNotFoundError,
    WebhookEventNotFoundError,
    prepare_webhook_delivery,
)
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)


def _persist_endpoint_and_events(
    *,
    endpoint_id: uuid.UUID,
    event_ids: list[uuid.UUID],
    marker: uuid.UUID,
    target_url: str,
    is_active: bool,
) -> None:
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery preparation {marker}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()

        for position, event_id in enumerate(event_ids, start=1):
            payload: dict[str, JsonValue] = {
                "marker": str(marker),
                "position": position,
                "nested": {
                    "active": True,
                    "optional": None,
                },
            }
            session.add(
                WebhookEvent(
                    id=event_id,
                    endpoint_id=endpoint_id,
                    event_type=f"delivery.preparation.{position}",
                    payload=payload,
                )
            )

        session.commit()


def _persist_attempts(
    *,
    event_id: uuid.UUID,
    attempt_numbers: list[int],
    marker: uuid.UUID,
) -> list[uuid.UUID]:
    attempt_ids = [uuid.uuid4() for _ in attempt_numbers]

    with SessionFactory() as session:
        for attempt_id, attempt_number in zip(
            attempt_ids,
            attempt_numbers,
            strict=True,
        ):
            session.add(
                WebhookDeliveryAttempt(
                    id=attempt_id,
                    event_id=event_id,
                    attempt_number=attempt_number,
                    outcome="failed",
                    target_url=(
                        f"https://example.test/delivery-preparation/{marker}/{attempt_number}"
                    ),
                    response_status_code=503,
                    error_message="Service unavailable",
                    duration_ms=100 + attempt_number,
                )
            )

        session.commit()

    return attempt_ids


def _cleanup_records(
    *,
    attempt_ids: list[uuid.UUID],
    event_ids: list[uuid.UUID],
    endpoint_ids: list[uuid.UUID],
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

        for endpoint_id in endpoint_ids:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if endpoint is not None:
                session.delete(endpoint)
        session.commit()

    with SessionFactory() as session:
        for attempt_id in attempt_ids:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        for event_id in event_ids:
            assert session.get(WebhookEvent, event_id) is None
        for endpoint_id in endpoint_ids:
            assert session.get(WebhookEndpoint, endpoint_id) is None


def test_prepare_delivery_returns_first_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/delivery-preparation/{marker}"

    try:
        _persist_endpoint_and_events(
            endpoint_id=endpoint_id,
            event_ids=[event_id],
            marker=marker,
            target_url=target_url,
            is_active=True,
        )

        with SessionFactory() as session:
            event = session.get(WebhookEvent, event_id)
            assert event is not None
            expected_payload = event.payload
            prepared = prepare_webhook_delivery(session, event_id=event_id)

        assert isinstance(prepared, PreparedWebhookDelivery)
        assert prepared.event_id == event_id
        assert prepared.target_url == target_url
        assert prepared.payload == expected_payload
        assert prepared.attempt_number == 1

        with SessionFactory() as session:
            assert (
                session.scalars(
                    select(WebhookDeliveryAttempt.id).where(
                        WebhookDeliveryAttempt.event_id == event_id
                    )
                ).all()
                == []
            )
    finally:
        _cleanup_records(
            attempt_ids=[],
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_prepare_delivery_uses_next_attempt_number() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []

    try:
        _persist_endpoint_and_events(
            endpoint_id=endpoint_id,
            event_ids=[event_id],
            marker=marker,
            target_url=f"https://example.test/delivery-preparation/{marker}",
            is_active=True,
        )
        attempt_ids = _persist_attempts(
            event_id=event_id,
            attempt_numbers=[1, 3],
            marker=marker,
        )

        with SessionFactory() as session:
            prepared = prepare_webhook_delivery(session, event_id=event_id)

        assert prepared.attempt_number == 4
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_prepare_delivery_isolates_attempts_by_event() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    first_event_id = uuid.uuid4()
    second_event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []

    try:
        _persist_endpoint_and_events(
            endpoint_id=endpoint_id,
            event_ids=[first_event_id, second_event_id],
            marker=marker,
            target_url=f"https://example.test/delivery-preparation/{marker}",
            is_active=True,
        )
        attempt_ids.extend(
            _persist_attempts(
                event_id=first_event_id,
                attempt_numbers=[1],
                marker=marker,
            )
        )
        attempt_ids.extend(
            _persist_attempts(
                event_id=second_event_id,
                attempt_numbers=[1, 2, 3],
                marker=marker,
            )
        )

        with SessionFactory() as session:
            prepared = prepare_webhook_delivery(
                session,
                event_id=first_event_id,
            )

        assert prepared.attempt_number == 2
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[first_event_id, second_event_id],
            endpoint_ids=[endpoint_id],
        )


def test_prepare_delivery_rejects_missing_event() -> None:
    missing_event_id = uuid.uuid4()

    with SessionFactory() as session:
        endpoint_ids_before = set(session.scalars(select(WebhookEndpoint.id)).all())
        event_ids_before = set(session.scalars(select(WebhookEvent.id)).all())
        attempt_ids_before = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())

    try:
        with SessionFactory() as session:
            with pytest.raises(
                WebhookEventNotFoundError,
                match="^Webhook event not found$",
            ):
                prepare_webhook_delivery(session, event_id=missing_event_id)
    finally:
        with SessionFactory() as session:
            assert session.get(WebhookEvent, missing_event_id) is None
            assert set(session.scalars(select(WebhookEndpoint.id)).all()) == (endpoint_ids_before)
            assert set(session.scalars(select(WebhookEvent.id)).all()) == (event_ids_before)
            assert (
                set(session.scalars(select(WebhookDeliveryAttempt.id)).all()) == attempt_ids_before
            )


def test_prepare_delivery_rejects_inactive_endpoint() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/delivery-preparation/{marker}"

    try:
        _persist_endpoint_and_events(
            endpoint_id=endpoint_id,
            event_ids=[event_id],
            marker=marker,
            target_url=target_url,
            is_active=False,
        )

        with SessionFactory() as session:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            event = session.get(WebhookEvent, event_id)
            assert endpoint is not None
            assert event is not None
            endpoint_values_before = (
                endpoint.id,
                endpoint.name,
                endpoint.target_url,
                endpoint.is_active,
                endpoint.created_at,
                endpoint.updated_at,
            )
            event_values_before = (
                event.id,
                event.endpoint_id,
                event.event_type,
                event.payload,
                event.created_at,
            )

        with SessionFactory() as session:
            with pytest.raises(
                InactiveWebhookEndpointError,
                match="^Webhook endpoint is inactive$",
            ):
                prepare_webhook_delivery(session, event_id=event_id)

        with SessionFactory() as session:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            event = session.get(WebhookEvent, event_id)
            assert endpoint is not None
            assert event is not None
            assert (
                endpoint.id,
                endpoint.name,
                endpoint.target_url,
                endpoint.is_active,
                endpoint.created_at,
                endpoint.updated_at,
            ) == endpoint_values_before
            assert (
                event.id,
                event.endpoint_id,
                event.event_type,
                event.payload,
                event.created_at,
            ) == event_values_before
            assert (
                session.scalars(
                    select(WebhookDeliveryAttempt.id).where(
                        WebhookDeliveryAttempt.event_id == event_id
                    )
                ).all()
                == []
            )
    finally:
        _cleanup_records(
            attempt_ids=[],
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_prepare_delivery_rejects_missing_endpoint() -> None:
    event_id = uuid.uuid4()
    missing_endpoint_id = uuid.uuid4()
    event = WebhookEvent(
        id=event_id,
        endpoint_id=missing_endpoint_id,
        event_type="delivery.preparation.missing-endpoint",
        payload={"test": "missing-endpoint"},
    )
    session = Mock(spec=Session)
    session.get.side_effect = [event, None]

    with pytest.raises(
        WebhookEndpointNotFoundError,
        match="^Webhook endpoint not found$",
    ):
        prepare_webhook_delivery(session, event_id=event_id)

    assert session.get.call_args_list == [
        call(WebhookEvent, event_id),
        call(WebhookEndpoint, missing_endpoint_id),
    ]
    session.scalar.assert_not_called()
