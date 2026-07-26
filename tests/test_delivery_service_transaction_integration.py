import json
import uuid
from datetime import UTC, datetime

import httpx2
from sqlalchemy import func, select

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.delivery_service import execute_webhook_delivery
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


def _table_counts() -> tuple[int, int, int, int]:
    with SessionFactory() as session:
        endpoint_count = session.scalar(select(func.count()).select_from(WebhookEndpoint))
        event_count = session.scalar(select(func.count()).select_from(WebhookEvent))
        job_count = session.scalar(select(func.count()).select_from(WebhookDeliveryJob))
        attempt_count = session.scalar(select(func.count()).select_from(WebhookDeliveryAttempt))

    assert endpoint_count is not None
    assert event_count is not None
    assert job_count is not None
    assert attempt_count is not None
    return endpoint_count, event_count, job_count, attempt_count


def _persist_endpoint_and_event(
    *,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    marker: uuid.UUID,
    target_url: str,
    event_type: str,
    payload: dict[str, JsonValue],
) -> None:
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery transaction {marker}",
                target_url=target_url,
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=event_type,
                payload=payload,
            )
        )
        session.commit()


def _cleanup_records(
    *,
    endpoint_id: uuid.UUID,
    event_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    expected_counts: tuple[int, int, int, int],
) -> None:
    with SessionFactory() as session:
        if attempt_id is not None:
            attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            if attempt is not None:
                session.delete(attempt)
        session.commit()

        event = session.get(WebhookEvent, event_id)
        if event is not None:
            session.delete(event)
        session.commit()

        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is not None:
            session.delete(endpoint)
        session.commit()

    with SessionFactory() as session:
        if attempt_id is not None:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None

    assert _table_counts() == expected_counts


def test_attempt_is_hidden_before_commit_and_visible_after_commit() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_id: uuid.UUID | None = None
    target_url = f"https://example.test/delivery-transaction/{marker}/visibility"
    event_type = f"delivery.transaction.visibility.{marker}"
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "scenario": "visibility",
    }
    requests: list[httpx2.Request] = []
    caller_session = SessionFactory()

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == target_url
        assert json.loads(request.content) == payload
        return httpx2.Response(204)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            event_type=event_type,
            payload=payload,
        )
        transport = httpx2.MockTransport(handler)
        with httpx2.Client(transport=transport) as client:
            attempt = execute_webhook_delivery(
                caller_session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
                monotonic_ns=iter([1_000_000_000, 1_025_000_000]).__next__,
            )

        assert isinstance(attempt.id, uuid.UUID)
        attempt_id = attempt.id
        assert attempt.event_id == event_id
        assert attempt.attempt_number == 1
        assert attempt in caller_session
        assert caller_session.get(WebhookDeliveryAttempt, attempt_id) is attempt

        with SessionFactory() as observer_session:
            assert observer_session.get(WebhookDeliveryAttempt, attempt_id) is None
            observer_session.rollback()

        caller_session.commit()

        with SessionFactory() as verification_session:
            stored_attempt = verification_session.get(WebhookDeliveryAttempt, attempt_id)
            assert stored_attempt is not None
            assert stored_attempt.event_id == event_id
            assert stored_attempt.attempt_number == 1
            assert stored_attempt.outcome == "succeeded"
            assert stored_attempt.target_url == target_url
            assert stored_attempt.response_status_code == 204
            assert stored_attempt.error_message is None
            assert stored_attempt.duration_ms == 25

        assert len(requests) == 1
    finally:
        if caller_session.in_transaction():
            caller_session.rollback()
        caller_session.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            attempt_id=attempt_id,
            expected_counts=initial_counts,
        )


def test_caller_rollback_removes_only_flushed_attempt() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    attempt_id: uuid.UUID | None = None
    target_url = f"https://example.test/delivery-transaction/{marker}/rollback"
    event_type = f"delivery.transaction.rollback.{marker}"
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "scenario": "rollback",
    }
    requests: list[httpx2.Request] = []
    caller_session = SessionFactory()

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == target_url
        assert json.loads(request.content) == payload
        return httpx2.Response(200)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            event_type=event_type,
            payload=payload,
        )
        transport = httpx2.MockTransport(handler)
        with httpx2.Client(transport=transport) as client:
            attempt = execute_webhook_delivery(
                caller_session,
                event_id=event_id,
                http_client=Httpx2WebhookHttpClient(client),
                timeout_seconds=5.0,
                utc_now=lambda: datetime(2026, 7, 26, 10, 1, tzinfo=UTC),
                monotonic_ns=iter([2_000_000_000, 2_010_000_000]).__next__,
            )

        assert isinstance(attempt.id, uuid.UUID)
        attempt_id = attempt.id
        assert attempt in caller_session
        assert caller_session.get(WebhookDeliveryAttempt, attempt_id) is attempt

        caller_session.rollback()

        with SessionFactory() as verification_session:
            assert verification_session.get(WebhookDeliveryAttempt, attempt_id) is None
            stored_endpoint = verification_session.get(WebhookEndpoint, endpoint_id)
            stored_event = verification_session.get(WebhookEvent, event_id)
            assert stored_endpoint is not None
            assert stored_endpoint.target_url == target_url
            assert stored_event is not None
            assert stored_event.endpoint_id == endpoint_id
            assert stored_event.event_type == event_type
            assert stored_event.payload == payload

        assert len(requests) == 1
    finally:
        if caller_session.in_transaction():
            caller_session.rollback()
        caller_session.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            attempt_id=attempt_id,
            expected_counts=initial_counts,
        )
