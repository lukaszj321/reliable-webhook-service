import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from reliable_webhook_service.config import Settings
from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.delivery_job_execution_service import (
    execute_webhook_delivery_job,
)
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.main import create_app
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

RESPONSE_FIELDS = {
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
EXPECTED_TIMEOUT = {
    "connect": 2.5,
    "read": 2.5,
    "write": 2.5,
    "pool": 2.5,
}


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
                name=f"Manual delivery integration {marker}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="manual.delivery.integration",
                payload=payload,
            )
        )
        session.commit()


def _persist_attempt(
    *,
    attempt_id: uuid.UUID,
    event_id: uuid.UUID,
    target_url: str,
) -> None:
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryAttempt(
                id=attempt_id,
                event_id=event_id,
                attempt_number=1,
                outcome="succeeded",
                target_url=target_url,
                response_status_code=200,
                error_message=None,
                duration_ms=1,
            )
        )
        session.commit()


def _persist_processing_job(
    *,
    job_id: uuid.UUID,
    event_id: uuid.UUID,
) -> None:
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status="processing",
                next_attempt_at=datetime(2026, 7, 28, 8, 0, tzinfo=UTC),
            )
        )
        session.commit()


def _attempt_ids_for_events(event_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    if not event_ids:
        return []

    with SessionFactory() as session:
        return list(
            session.scalars(
                select(WebhookDeliveryAttempt.id).where(
                    WebhookDeliveryAttempt.event_id.in_(event_ids)
                )
            ).all()
        )


def _attempts_for_event(event_id: uuid.UUID) -> list[WebhookDeliveryAttempt]:
    with SessionFactory() as session:
        statement = (
            select(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.event_id == event_id)
            .order_by(WebhookDeliveryAttempt.attempt_number.asc())
        )
        return list(session.scalars(statement).all())


def _attempt_count() -> int:
    with SessionFactory() as session:
        count = session.scalar(select(func.count()).select_from(WebhookDeliveryAttempt))
        assert count is not None
        return count


def _endpoint_snapshot(
    endpoint_id: uuid.UUID,
) -> tuple[uuid.UUID, str, str, bool, datetime, datetime]:
    with SessionFactory() as session:
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        assert endpoint is not None
        return (
            endpoint.id,
            endpoint.name,
            endpoint.target_url,
            endpoint.is_active,
            endpoint.created_at,
            endpoint.updated_at,
        )


def _event_snapshot(
    event_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, str, dict[str, JsonValue], datetime]:
    with SessionFactory() as session:
        event = session.get(WebhookEvent, event_id)
        assert event is not None
        return (
            event.id,
            event.endpoint_id,
            event.event_type,
            event.payload,
            event.created_at,
        )


def _cleanup_records(
    *,
    event_ids: list[uuid.UUID],
    endpoint_ids: list[uuid.UUID],
) -> None:
    attempt_ids = _attempt_ids_for_events(event_ids)

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


@contextmanager
def _application_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> Iterator[TestClient]:
    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as raw_client:
        webhook_client = Httpx2WebhookHttpClient(raw_client)
        settings = Settings(
            _env_file=None,
            webhook_delivery_timeout_seconds=2.5,
        )
        application = create_app()
        application.dependency_overrides[get_webhook_http_client] = lambda: webhook_client
        application.dependency_overrides[get_settings] = lambda: settings
        get_settings.cache_clear()
        try:
            with TestClient(application) as client:
                yield client
        finally:
            application.dependency_overrides.clear()
            get_settings.cache_clear()


def test_successful_delivery_persists_and_lists_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}?tenant=alpha"
    payload: dict[str, JsonValue] = {
        "event": "order.created",
        "order": {
            "id": str(marker),
            "paid": True,
            "lines": [{"sku": "SKU-1", "quantity": 2}],
        },
        "optional": None,
    }
    requests: list[httpx2.Request] = []
    captured_timeout: dict[str, float] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        timeout = request.extensions["timeout"]
        assert isinstance(timeout, dict)
        captured_timeout.update(timeout)
        return httpx2.Response(204)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload=payload,
        )
        _persist_processing_job(job_id=job_id, event_id=event_id)
        endpoint_before = _endpoint_snapshot(endpoint_id)
        event_before = _event_snapshot(event_id)

        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{event_id}/delivery-attempts")

            assert response.status_code == 201
            response_body = response.json()
            assert set(response_body) == RESPONSE_FIELDS
            assert response_body["event_id"] == str(event_id)
            assert response_body["attempt_number"] == 1
            assert response_body["outcome"] == "succeeded"
            assert response_body["target_url"] == target_url
            assert response_body["response_status_code"] == 204
            assert response_body["error_message"] is None
            assert isinstance(response_body["duration_ms"], int)
            assert response_body["duration_ms"] >= 0
            assert uuid.UUID(response_body["id"])
            attempted_at = datetime.fromisoformat(
                response_body["attempted_at"].replace("Z", "+00:00")
            )
            assert attempted_at.utcoffset() is not None

            listing_response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert len(requests) == 1
        request = requests[0]
        assert request.method == "POST"
        assert str(request.url) == target_url
        assert request.headers["Content-Type"].startswith("application/json")
        assert json.loads(request.content) == payload
        assert captured_timeout == EXPECTED_TIMEOUT

        attempts = _attempts_for_event(event_id)
        assert len(attempts) == 1
        stored_attempt = attempts[0]
        assert str(stored_attempt.id) == response_body["id"]
        assert str(stored_attempt.event_id) == response_body["event_id"]
        assert stored_attempt.attempt_number == response_body["attempt_number"]
        assert stored_attempt.outcome == response_body["outcome"]
        assert stored_attempt.target_url == response_body["target_url"]
        assert stored_attempt.response_status_code == response_body["response_status_code"]
        assert stored_attempt.error_message == response_body["error_message"]
        assert stored_attempt.duration_ms == response_body["duration_ms"]
        assert stored_attempt.attempted_at == attempted_at
        assert _endpoint_snapshot(endpoint_id) == endpoint_before
        assert _event_snapshot(event_id) == event_before
        with SessionFactory() as session:
            stored_job = session.get(WebhookDeliveryJob, job_id)
            assert stored_job is not None
            assert stored_job.status == "processing"
            assert stored_job.attempt_count == 0

        assert listing_response.status_code == 200
        assert listing_response.json() == [response_body]
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_worker_attempt_after_manual_delivery_uses_first_cycle_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}/then-worker"
    payload: dict[str, JsonValue] = {"marker": str(marker)}
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(204)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload=payload,
        )
        _persist_processing_job(job_id=job_id, event_id=event_id)

        with _application_client(handler) as client:
            manual_response = client.post(f"/webhook-events/{event_id}/delivery-attempts")

        assert manual_response.status_code == 201
        assert manual_response.json()["attempt_number"] == 1
        with SessionFactory() as verification_session:
            job_after_manual_delivery = verification_session.get(WebhookDeliveryJob, job_id)
            assert job_after_manual_delivery is not None
            assert job_after_manual_delivery.attempt_count == 0
            assert job_after_manual_delivery.status == "processing"

        with httpx2.Client(transport=httpx2.MockTransport(handler)) as raw_client:
            worker_client = Httpx2WebhookHttpClient(raw_client)
            with SessionFactory() as worker_session:
                worker_result = execute_webhook_delivery_job(
                    worker_session,
                    job_id=job_id,
                    http_client=worker_client,
                    timeout_seconds=2.5,
                    max_attempts=3,
                    base_delay_seconds=5.0,
                    max_delay_seconds=300.0,
                    utc_now=lambda: datetime(2026, 7, 28, 8, 1, tzinfo=UTC),
                    decision_now=lambda: datetime(2026, 7, 28, 8, 2, tzinfo=UTC),
                    monotonic_ns=iter([1_000_000_000, 1_010_000_000]).__next__,
                )
                assert worker_result.attempt.attempt_number == 2
                assert worker_result.job.attempt_count == 1
                assert worker_result.job.status == "succeeded"
                worker_session.commit()

        attempts = _attempts_for_event(event_id)
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        with SessionFactory() as verification_session:
            stored_job = verification_session.get(WebhookDeliveryJob, job_id)
            assert stored_job is not None
            assert stored_job.attempt_count == 1
            assert stored_job.status == "succeeded"
        assert len(requests) == 2
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_failed_http_status_persists_without_response_body() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}"
    private_body = "private upstream response body"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(503, content=private_body.encode())

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload={"marker": str(marker)},
        )

        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 201
        response_body = response.json()
        assert len(requests) == 1
        assert response_body["outcome"] == "failed"
        assert response_body["response_status_code"] == 503
        assert response_body["error_message"] == "HTTP response returned status 503"
        assert private_body not in response.text

        attempts = _attempts_for_event(event_id)
        assert len(attempts) == 1
        assert attempts[0].outcome == "failed"
        assert attempts[0].response_status_code == 503
        assert attempts[0].error_message == "HTTP response returned status 503"
        assert private_body not in (attempts[0].error_message or "")
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


@pytest.mark.parametrize(
    ("error_type", "expected_error"),
    [
        pytest.param(
            httpx2.ReadTimeout,
            "Webhook request timed out",
            id="timeout",
        ),
        pytest.param(
            httpx2.ConnectError,
            "Webhook request failed: ConnectError",
            id="connection-failure",
        ),
    ],
)
def test_transport_failure_persists_and_lists_attempt(
    error_type: type[httpx2.RequestError],
    expected_error: str,
) -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}"
    private_error = f"private {error_type.__name__} details"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise error_type(private_error, request=request)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload={"marker": str(marker)},
        )

        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{event_id}/delivery-attempts")
            listing_response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 201
        response_body = response.json()
        assert len(requests) == 1
        assert response_body["outcome"] == "failed"
        assert response_body["response_status_code"] is None
        assert response_body["error_message"] == expected_error
        assert private_error not in response.text

        attempts = _attempts_for_event(event_id)
        assert len(attempts) == 1
        assert attempts[0].outcome == "failed"
        assert attempts[0].response_status_code is None
        assert attempts[0].error_message == expected_error
        assert private_error not in (attempts[0].error_message or "")
        assert listing_response.status_code == 200
        assert listing_response.json() == [response_body]

        with SessionFactory() as session:
            assert session.scalar(select(1)) == 1
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_attempt_numbering_is_isolated_by_event() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    other_endpoint_id = uuid.uuid4()
    other_event_id = uuid.uuid4()
    other_attempt_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}"
    other_target_url = f"https://example.test/manual-delivery/{marker}/other"
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200)

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload={"marker": str(marker), "event": "primary"},
        )
        _persist_endpoint_and_event(
            endpoint_id=other_endpoint_id,
            event_id=other_event_id,
            marker=marker,
            target_url=other_target_url,
            payload={"marker": str(marker), "event": "other"},
        )
        _persist_attempt(
            attempt_id=other_attempt_id,
            event_id=other_event_id,
            target_url=other_target_url,
        )

        with _application_client(handler) as client:
            first_response = client.post(f"/webhook-events/{event_id}/delivery-attempts")
            second_response = client.post(f"/webhook-events/{event_id}/delivery-attempts")
            listing_response = client.get(f"/webhook-events/{event_id}/delivery-attempts")

        assert first_response.status_code == 201
        assert second_response.status_code == 201
        first_body = first_response.json()
        second_body = second_response.json()
        assert len(requests) == 2
        assert first_body["attempt_number"] == 1
        assert second_body["attempt_number"] == 2

        attempts = _attempts_for_event(event_id)
        assert len(attempts) == 2
        assert [attempt.attempt_number for attempt in attempts] == [1, 2]
        assert [str(attempt.id) for attempt in attempts] == [
            first_body["id"],
            second_body["id"],
        ]
        other_attempts = _attempts_for_event(other_event_id)
        assert len(other_attempts) == 1
        assert other_attempts[0].id == other_attempt_id
        assert other_attempts[0].attempt_number == 1

        assert listing_response.status_code == 200
        assert listing_response.json() == [first_body, second_body]
    finally:
        _cleanup_records(
            event_ids=[event_id, other_event_id],
            endpoint_ids=[endpoint_id, other_endpoint_id],
        )


def test_redirect_is_not_followed_and_persists_failed_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}/redirect"
    redirect_url = f"https://example.test/manual-delivery/{marker}/redirected"
    requested_urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_urls.append(str(request.url))
        if str(request.url) == target_url:
            return httpx2.Response(302, headers={"Location": redirect_url})
        raise AssertionError(f"Unexpected redirected request: {request.url}")

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload={"marker": str(marker)},
        )

        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 201
        assert requested_urls == [target_url]
        assert redirect_url not in requested_urls
        response_body = response.json()
        assert response_body["outcome"] == "failed"
        assert response_body["response_status_code"] == 302
        assert response_body["error_message"] == "HTTP response returned status 302"

        attempts = _attempts_for_event(event_id)
        assert len(attempts) == 1
        assert attempts[0].outcome == "failed"
        assert attempts[0].response_status_code == 302
        assert attempts[0].error_message == "HTTP response returned status 302"
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )


def test_missing_event_returns_404_without_request_or_attempt() -> None:
    missing_event_id = uuid.uuid4()
    attempts_before = _attempt_count()
    request_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    try:
        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{missing_event_id}/delivery-attempts")

        assert response.status_code == 404
        assert response.json() == {"detail": "Webhook event not found"}
        assert request_count == 0
        assert _attempt_ids_for_events([missing_event_id]) == []
        assert _attempt_count() == attempts_before
    finally:
        with SessionFactory() as session:
            assert session.get(WebhookEvent, missing_event_id) is None
        assert _attempt_ids_for_events([missing_event_id]) == []
        assert _attempt_count() == attempts_before


def test_inactive_endpoint_returns_409_without_request_or_attempt() -> None:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = f"https://example.test/manual-delivery/{marker}/inactive"
    request_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    try:
        _persist_endpoint_and_event(
            endpoint_id=endpoint_id,
            event_id=event_id,
            marker=marker,
            target_url=target_url,
            payload={"marker": str(marker)},
            is_active=False,
        )
        endpoint_before = _endpoint_snapshot(endpoint_id)
        event_before = _event_snapshot(event_id)

        with _application_client(handler) as client:
            response = client.post(f"/webhook-events/{event_id}/delivery-attempts")

        assert response.status_code == 409
        assert response.json() == {"detail": "Webhook endpoint is inactive"}
        assert request_count == 0
        assert _attempt_ids_for_events([event_id]) == []
        assert _endpoint_snapshot(endpoint_id) == endpoint_before
        assert _event_snapshot(event_id) == event_before
    finally:
        _cleanup_records(
            event_ids=[event_id],
            endpoint_ids=[endpoint_id],
        )
