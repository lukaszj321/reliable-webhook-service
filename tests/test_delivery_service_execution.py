import json
import uuid
from datetime import UTC, datetime

import httpx2
import pytest
from sqlalchemy import select

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.delivery_service import (
    InactiveWebhookEndpointError,
    WebhookEventNotFoundError,
    execute_webhook_delivery,
)
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)


def _persist_endpoint_and_event(
    *,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    marker: uuid.UUID,
    target_url: str,
    payload: dict[str, JsonValue],
    is_active: bool = True,
) -> None:
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery execution {marker}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="delivery.execution.test",
                payload=payload,
            )
        )
        session.commit()


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


def _attempt_ids_for_event(event_id: uuid.UUID) -> list[uuid.UUID]:
    with SessionFactory() as session:
        return list(
            session.scalars(
                select(WebhookDeliveryAttempt.id).where(WebhookDeliveryAttempt.event_id == event_id)
            ).all()
        )


def test_execute_delivery_persists_successful_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    target_url = f"https://example.test/delivery-execution/{marker}?tenant=alpha"
    payload: dict[str, JsonValue] = {
        "event": "order.created",
        "order": {"id": str(marker), "paid": True},
        "items": [1, 2],
        "optional": None,
    }
    attempted_at = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
    monotonic_values = iter([1_000_000_000, 1_125_000_000])
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == target_url
        assert json.loads(request.content) == payload
        return httpx2.Response(204)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload=payload,
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

        transport = httpx2.MockTransport(handler)
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: attempted_at,
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(attempt.id, uuid.UUID)
            attempt_ids.append(attempt.id)
            assert attempt.attempt_number == 1
            assert attempt.outcome == "succeeded"
            assert attempt.target_url == target_url
            assert attempt.response_status_code == 204
            assert attempt.error_message is None
            assert attempt.duration_ms == 125
            assert attempt.attempted_at == attempted_at

        assert len(requests) == 1
        assert _attempt_ids_for_event(event_id) == attempt_ids

        with SessionFactory() as session:
            stored_attempt = session.get(WebhookDeliveryAttempt, attempt_ids[0])
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            event = session.get(WebhookEvent, event_id)
            assert stored_attempt is not None
            assert endpoint is not None
            assert event is not None
            assert stored_attempt.id == attempt_ids[0]
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
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_execute_delivery_persists_failed_http_response() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            503,
            content=b"private upstream response body",
        )

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=f"https://example.test/delivery-execution/{marker}",
            payload={"marker": str(marker)},
        )
        transport = httpx2.MockTransport(handler)
        monotonic_values = iter([1_000_000_000, 1_010_000_000])
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(attempt.id, uuid.UUID)
            attempt_ids.append(attempt.id)
            assert attempt.outcome == "failed"
            assert attempt.response_status_code == 503
            assert attempt.error_message == "HTTP response returned status 503"
            assert "private upstream response body" not in attempt.error_message

        assert len(requests) == 1
        assert _attempt_ids_for_event(event_id) == attempt_ids
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_execute_delivery_persists_timeout() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise httpx2.ReadTimeout("private timeout details", request=request)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=f"https://example.test/delivery-execution/{marker}",
            payload={"marker": str(marker)},
        )
        transport = httpx2.MockTransport(handler)
        monotonic_values = iter([1_000_000_000, 1_015_000_000])
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 25, 10, 2, tzinfo=UTC),
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(attempt.id, uuid.UUID)
            attempt_ids.append(attempt.id)
            assert attempt.outcome == "failed"
            assert attempt.response_status_code is None
            assert attempt.error_message == "Webhook request timed out"
            assert session.scalar(select(1)) == 1

        assert len(requests) == 1
        assert _attempt_ids_for_event(event_id) == attempt_ids
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_execute_delivery_persists_connection_failure() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    requests: list[httpx2.Request] = []
    private_error = "private DNS details"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise httpx2.ConnectError(private_error, request=request)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=f"https://example.test/delivery-execution/{marker}",
            payload={"marker": str(marker)},
        )
        transport = httpx2.MockTransport(handler)
        monotonic_values = iter([1_000_000_000, 1_020_000_000])
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 25, 10, 3, tzinfo=UTC),
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(attempt.id, uuid.UUID)
            attempt_ids.append(attempt.id)
            assert attempt.outcome == "failed"
            assert attempt.response_status_code is None
            assert attempt.error_message == "Webhook request failed: ConnectError"
            assert private_error not in attempt.error_message

        assert len(requests) == 1
        assert _attempt_ids_for_event(event_id) == attempt_ids
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_execute_delivery_increments_attempt_numbers() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=f"https://example.test/delivery-execution/{marker}",
            payload={"marker": str(marker)},
        )
        transport = httpx2.MockTransport(handler)
        utc_values = iter(
            [
                datetime(2026, 7, 25, 10, 4, tzinfo=UTC),
                datetime(2026, 7, 25, 10, 5, tzinfo=UTC),
            ]
        )
        monotonic_values = iter(
            [
                1_000_000_000,
                1_010_000_000,
                2_000_000_000,
                2_010_000_000,
            ]
        )
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            http_client = Httpx2WebhookHttpClient(client)
            first_attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=http_client,
                timeout_seconds=5.0,
                utc_now=utc_values.__next__,
                monotonic_ns=monotonic_values.__next__,
            )
            second_attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=http_client,
                timeout_seconds=5.0,
                utc_now=utc_values.__next__,
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(first_attempt.id, uuid.UUID)
            assert isinstance(second_attempt.id, uuid.UUID)
            attempt_ids.extend([first_attempt.id, second_attempt.id])
            assert first_attempt.attempt_number == 1
            assert second_attempt.attempt_number == 2

        assert len(requests) == 2
        with SessionFactory() as session:
            stored_numbers = list(
                session.scalars(
                    select(WebhookDeliveryAttempt.attempt_number)
                    .where(WebhookDeliveryAttempt.event_id == event_id)
                    .order_by(WebhookDeliveryAttempt.attempt_number)
                ).all()
            )
            assert stored_numbers == [1, 2]
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


@pytest.mark.parametrize(
    ("scenario", "expected_error", "message"),
    [
        (
            "missing-event",
            WebhookEventNotFoundError,
            "Webhook event not found",
        ),
        (
            "inactive-endpoint",
            InactiveWebhookEndpointError,
            "Webhook endpoint is inactive",
        ),
        (
            "invalid-timeout",
            ValueError,
            "timeout_seconds must be a finite positive number",
        ),
        (
            "naive-clock",
            ValueError,
            "utc_now must return a timezone-aware datetime",
        ),
    ],
)
def test_execute_delivery_does_not_request_or_persist_before_validation(
    scenario: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    created_endpoint_ids: list[uuid.UUID] = []
    created_event_ids: list[uuid.UUID] = []
    request_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    with SessionFactory() as session:
        attempt_ids_before = set(session.scalars(select(WebhookDeliveryAttempt.id)).all())

    try:
        if scenario != "missing-event":
            _persist_endpoint_and_event(
                endpoint_id=endpoint_id,
                event_id=event_id,
                marker=marker,
                target_url=f"https://example.test/delivery-execution/{marker}",
                payload={"marker": str(marker)},
                is_active=scenario != "inactive-endpoint",
            )
            created_endpoint_ids.append(endpoint_id)
            created_event_ids.append(event_id)

        timeout_seconds = 0.0 if scenario == "invalid-timeout" else 5.0
        clock_value = (
            datetime(2026, 7, 25, 10, 6)
            if scenario == "naive-clock"
            else datetime(2026, 7, 25, 10, 6, tzinfo=UTC)
        )
        transport = httpx2.MockTransport(handler)
        monotonic_values = iter([1_000_000_000])

        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            with pytest.raises(expected_error, match=f"^{message}$"):
                execute_webhook_delivery(
                    session,
                    event_id=event_id,
                    http_client=Httpx2WebhookHttpClient(client),
                    timeout_seconds=timeout_seconds,
                    utc_now=lambda: clock_value,
                    monotonic_ns=monotonic_values.__next__,
                )

        assert request_count == 0
        with SessionFactory() as session:
            assert (
                set(session.scalars(select(WebhookDeliveryAttempt.id)).all()) == attempt_ids_before
            )
    finally:
        _cleanup_records(
            attempt_ids=[],
            event_ids=created_event_ids,
            endpoint_ids=created_endpoint_ids,
        )


def test_execute_delivery_uses_non_negative_duration() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_ids: list[uuid.UUID] = []
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=f"https://example.test/delivery-execution/{marker}",
            payload={"marker": str(marker)},
        )
        transport = httpx2.MockTransport(handler)
        monotonic_values = iter([2_000_000_000, 1_000_000_000])
        with httpx2.Client(transport=transport) as client, SessionFactory() as session:
            attempt = execute_webhook_delivery(
                session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 25, 10, 7, tzinfo=UTC),
                monotonic_ns=monotonic_values.__next__,
            )
            assert isinstance(attempt.id, uuid.UUID)
            attempt_ids.append(attempt.id)
            assert attempt.duration_ms == 0

        assert len(requests) == 1
        assert _attempt_ids_for_event(event_id) == attempt_ids
    finally:
        _cleanup_records(
            attempt_ids=attempt_ids,
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )
