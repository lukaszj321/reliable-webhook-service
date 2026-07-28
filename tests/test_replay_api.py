import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import Mock, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service import api, main
from reliable_webhook_service.database import get_session
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.replay_service import (
    WebhookReplayDeliveryJobNotFoundError,
    WebhookReplayDeliveryJobNotReplayableError,
    WebhookReplayEndpointInactiveError,
    WebhookReplayEndpointNotFoundError,
    WebhookReplayEventNotFoundError,
    WebhookReplayResult,
)
from reliable_webhook_service.schemas import WebhookReplayResponse

EVENT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REPLAYED_AT = datetime(2026, 7, 29, 15, 0, tzinfo=UTC)
RESPONSE_BODY = {
    "event_id": str(EVENT_ID),
    "delivery_job_id": str(JOB_ID),
    "status": "pending",
    "next_attempt_at": "2026-07-29T15:00:00Z",
}


class FakeRawHttpClient:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(main.httpx2, "Client", FakeRawHttpClient)
    application = main.create_app()
    yield application
    application.dependency_overrides.clear()


def _configure_session(application: FastAPI) -> tuple[Mock, Mock, Mock]:
    session = Mock(spec=Session)
    http_dependency = Mock(side_effect=AssertionError("HTTP dependency must not be resolved"))
    settings_dependency = Mock(
        side_effect=AssertionError("Settings dependency must not be resolved")
    )
    application.dependency_overrides[get_session] = lambda: session
    application.dependency_overrides[get_webhook_http_client] = http_dependency
    application.dependency_overrides[get_settings] = settings_dependency
    return session, http_dependency, settings_dependency


def _result() -> WebhookReplayResult:
    return WebhookReplayResult(
        event_id=EVENT_ID,
        delivery_job_id=JOB_ID,
        status="pending",
        next_attempt_at=REPLAYED_AT,
    )


def test_replay_route_is_registered_without_request_body(
    application: FastAPI,
) -> None:
    routes = [
        (method, route.path)
        for route in api.webhook_event_router.routes
        for method in (route.methods or set())
    ]
    assert routes.count(("POST", "/webhook-events/{event_id}/replay")) == 1

    operation = application.openapi()["paths"]["/webhook-events/{event_id}/replay"]["post"]
    assert "requestBody" not in operation
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WebhookReplayResponse"
    }
    parameter_names = {parameter["name"] for parameter in operation["parameters"]}
    assert parameter_names == {"event_id"}
    assert "Idempotency-Key" not in parameter_names


def test_success_returns_202_snapshot_and_commits_once(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    clock = Mock(return_value=REPLAYED_AT)
    service = Mock(return_value=_result())
    delivery = Mock()
    monkeypatch.setattr(api, "_utc_now", clock)
    monkeypatch.setattr(api, "replay_webhook_event", service)
    monkeypatch.setattr(api, "execute_webhook_delivery", delivery)

    with TestClient(application) as client:
        response = client.post(f"/webhook-events/{EVENT_ID}/replay")

    assert response.status_code == 202
    assert response.json() == RESPONSE_BODY
    clock.assert_called_once_with()
    assert clock.return_value.tzinfo is UTC
    service.assert_called_once_with(
        session,
        event_id=EVENT_ID,
        replayed_at=REPLAYED_AT,
    )
    assert session.mock_calls == [call.commit()]
    session.commit.assert_called_once_with()
    session.refresh.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()
    http_dependency.assert_not_called()
    settings_dependency.assert_not_called()
    delivery.assert_not_called()


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (WebhookReplayEventNotFoundError("Webhook event not found"), 404),
        (WebhookReplayEndpointNotFoundError("Webhook endpoint not found"), 409),
        (WebhookReplayEndpointInactiveError("Webhook endpoint is inactive"), 409),
        (WebhookReplayDeliveryJobNotFoundError("Webhook delivery job not found"), 409),
        (
            WebhookReplayDeliveryJobNotReplayableError("Webhook delivery job is not replayable"),
            409,
        ),
    ],
)
def test_application_error_mapping_has_exact_detail_without_commit(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    expected_status: int,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    clock = Mock(return_value=REPLAYED_AT)
    service = Mock(side_effect=error)
    monkeypatch.setattr(api, "_utc_now", clock)
    monkeypatch.setattr(api, "replay_webhook_event", service)

    with TestClient(application) as client:
        response = client.post(f"/webhook-events/{EVENT_ID}/replay")

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}
    clock.assert_called_once_with()
    service.assert_called_once_with(
        session,
        event_id=EVENT_ID,
        replayed_at=REPLAYED_AT,
    )
    session.commit.assert_not_called()
    session.refresh.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()
    http_dependency.assert_not_called()
    settings_dependency.assert_not_called()


def test_service_database_error_propagates_unchanged(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _ = _configure_session(application)
    error = SQLAlchemyError("database unavailable")
    service = Mock(side_effect=error)
    monkeypatch.setattr(api, "_utc_now", Mock(return_value=REPLAYED_AT))
    monkeypatch.setattr(api, "replay_webhook_event", service)

    with TestClient(application) as client:
        with pytest.raises(SQLAlchemyError) as error_info:
            client.post(f"/webhook-events/{EVENT_ID}/replay")

    assert error_info.value is error
    session.commit.assert_not_called()
    session.refresh.assert_not_called()


def test_commit_error_propagates_without_refresh(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _ = _configure_session(application)
    error = SQLAlchemyError("commit failed")
    session.commit.side_effect = error
    service = Mock(return_value=_result())
    monkeypatch.setattr(api, "_utc_now", Mock(return_value=REPLAYED_AT))
    monkeypatch.setattr(api, "replay_webhook_event", service)

    with TestClient(application) as client:
        with pytest.raises(SQLAlchemyError) as error_info:
            client.post(f"/webhook-events/{EVENT_ID}/replay")

    assert error_info.value is error
    service.assert_called_once()
    session.commit.assert_called_once_with()
    session.refresh.assert_not_called()
    session.rollback.assert_not_called()
    session.close.assert_not_called()


def test_invalid_uuid_returns_standard_422_without_service_call(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _ = _configure_session(application)
    clock = Mock()
    service = Mock()
    monkeypatch.setattr(api, "_utc_now", clock)
    monkeypatch.setattr(api, "replay_webhook_event", service)

    with TestClient(application) as client:
        response = client.post("/webhook-events/not-a-uuid/replay")

    assert response.status_code == 422
    clock.assert_not_called()
    service.assert_not_called()
    session.commit.assert_not_called()
    session.refresh.assert_not_called()


def test_response_schema_exposes_only_safe_replay_snapshot() -> None:
    schema = WebhookReplayResponse.model_json_schema()

    assert set(schema["properties"]) == {
        "event_id",
        "delivery_job_id",
        "status",
        "next_attempt_at",
    }
    assert set(schema["required"]) == {
        "event_id",
        "delivery_job_id",
        "status",
        "next_attempt_at",
    }
    serialized = WebhookReplayResponse(
        event_id=EVENT_ID,
        delivery_job_id=JOB_ID,
        status="pending",
        next_attempt_at=REPLAYED_AT,
    ).model_dump()
    assert set(serialized) == set(RESPONSE_BODY)
    assert "attempt_count" not in serialized
    assert "payload" not in serialized
    assert "event_type" not in serialized
    assert "endpoint_id" not in serialized
    assert "idempotency_key" not in serialized
    assert "attempts" not in serialized
