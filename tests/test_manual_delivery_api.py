import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from reliable_webhook_service import api, dependencies, main
from reliable_webhook_service.config import Settings
from reliable_webhook_service.database import get_session
from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_service import (
    InactiveWebhookEndpointError,
    WebhookEndpointNotFoundError,
    WebhookEventNotFoundError,
)
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.models import WebhookDeliveryAttempt

EVENT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
ATTEMPT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ATTEMPTED_AT = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
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


class FakeRawHttpClient:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(main.httpx2, "Client", FakeRawHttpClient)
    app = main.create_app()
    yield app
    app.dependency_overrides.clear()


def _configure_dependencies(
    application: FastAPI,
    *,
    timeout_seconds: float = 2.5,
) -> tuple[Mock, Mock]:
    session = Mock(spec=Session)
    http_client = Mock(spec=WebhookHttpClient)
    settings = Settings(
        _env_file=None,
        webhook_delivery_timeout_seconds=timeout_seconds,
    )

    application.dependency_overrides[get_session] = lambda: session
    application.dependency_overrides[get_webhook_http_client] = lambda: http_client
    application.dependency_overrides[get_settings] = lambda: settings
    return session, http_client


def _attempt(
    *,
    outcome: str,
    response_status_code: int | None,
    error_message: str | None,
) -> WebhookDeliveryAttempt:
    return WebhookDeliveryAttempt(
        id=ATTEMPT_ID,
        event_id=EVENT_ID,
        attempt_number=1,
        outcome=outcome,
        target_url="https://example.test/webhooks/orders",
        response_status_code=response_status_code,
        error_message=error_message,
        duration_ms=125,
        attempted_at=ATTEMPTED_AT,
    )


def test_attempt_routes_are_registered_once_without_request_body(
    application: FastAPI,
) -> None:
    routes = [
        (method, route.path)
        for route in api.webhook_event_router.routes
        for method in (route.methods or set())
    ]

    assert routes.count(("GET", "/webhook-events/{event_id}/delivery-attempts")) == 1
    assert routes.count(("POST", "/webhook-events/{event_id}/delivery-attempts")) == 1

    schema = application.openapi()
    post_operation = schema["paths"]["/webhook-events/{event_id}/delivery-attempts"]["post"]
    assert "requestBody" not in post_operation
    assert "/webhook-delivery-attempts" not in schema["paths"]


def test_successful_attempt_returns_201_and_calls_service_once(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_client = _configure_dependencies(application)
    service = Mock(
        return_value=_attempt(
            outcome="succeeded",
            response_status_code=204,
            error_message=None,
        )
    )
    monkeypatch.setattr(api, "execute_webhook_delivery", service)

    with TestClient(application) as client:
        response = client.post(
            f"/webhook-events/{EVENT_ID}/delivery-attempts?timeout_seconds=999",
        )

    assert response.status_code == 201
    assert set(response.json()) == RESPONSE_FIELDS
    assert response.json()["outcome"] == "succeeded"
    service.assert_called_once_with(
        session,
        event_id=EVENT_ID,
        http_client=http_client,
        timeout_seconds=2.5,
    )
    assert session.mock_calls == []
    assert http_client.mock_calls == []


def test_failed_attempt_returns_201(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_dependencies(application)
    service = Mock(
        return_value=_attempt(
            outcome="failed",
            response_status_code=503,
            error_message="HTTP response returned status 503",
        )
    )
    monkeypatch.setattr(api, "execute_webhook_delivery", service)

    with TestClient(application) as client:
        response = client.post(f"/webhook-events/{EVENT_ID}/delivery-attempts")

    assert response.status_code == 201
    assert response.json()["outcome"] == "failed"
    assert response.json()["response_status_code"] == 503
    assert response.json()["error_message"] == "HTTP response returned status 503"
    service.assert_called_once()


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_detail"),
    [
        (
            WebhookEventNotFoundError("Webhook event not found"),
            404,
            "Webhook event not found",
        ),
        (
            WebhookEndpointNotFoundError("Webhook endpoint not found"),
            409,
            "Webhook endpoint not found",
        ),
        (
            InactiveWebhookEndpointError("Webhook endpoint is inactive"),
            409,
            "Webhook endpoint is inactive",
        ),
    ],
)
def test_preparation_error_mapping(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    expected_status: int,
    expected_detail: str,
) -> None:
    _configure_dependencies(application)
    service = Mock(side_effect=error)
    monkeypatch.setattr(api, "execute_webhook_delivery", service)

    with TestClient(application) as client:
        response = client.post(f"/webhook-events/{EVENT_ID}/delivery-attempts")

    assert response.status_code == expected_status
    assert response.json() == {"detail": expected_detail}
    service.assert_called_once()


def test_invalid_uuid_returns_422_without_calling_service(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_dependencies(application)
    service = Mock()
    monkeypatch.setattr(api, "execute_webhook_delivery", service)

    with TestClient(application) as client:
        response = client.post("/webhook-events/not-a-uuid/delivery-attempts")

    assert response.status_code == 422
    service.assert_not_called()


def test_settings_dependency_is_lazy_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = Settings(
        _env_file=None,
        webhook_delivery_timeout_seconds=4.5,
    )
    created: list[Settings] = []

    def create_settings() -> Settings:
        created.append(expected)
        return expected

    get_settings.cache_clear()
    monkeypatch.setattr(dependencies, "Settings", create_settings)

    assert created == []
    first = get_settings()
    second = get_settings()

    assert first is expected
    assert second is expected
    assert first.webhook_delivery_timeout_seconds == 4.5
    assert created == [expected]
    get_settings.cache_clear()
