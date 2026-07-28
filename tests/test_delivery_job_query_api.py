import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service import api, main
from reliable_webhook_service.database import get_session
from reliable_webhook_service.delivery_job_query_service import (
    DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    WebhookDeliveryJobCursorValidationError,
    WebhookDeliveryJobEventNotFoundError,
    WebhookDeliveryJobLimitValidationError,
    WebhookDeliveryJobNotFoundError,
    WebhookDeliveryJobPage,
    WebhookDeliveryJobSnapshot,
    WebhookDeliveryJobStatus,
    WebhookDeliveryJobStatusValidationError,
)
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.schemas import (
    WebhookDeliveryJobListResponse,
    WebhookDeliveryJobResponse,
)

EVENT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
CREATED_AT = datetime(2026, 8, 1, 11, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _FakeRawHttpClient:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(main.httpx2, "Client", _FakeRawHttpClient)
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


def _snapshot(
    status: WebhookDeliveryJobStatus = "pending",
) -> WebhookDeliveryJobSnapshot:
    return WebhookDeliveryJobSnapshot(
        id=JOB_ID,
        event_id=EVENT_ID,
        status=status,
        attempt_count=2,
        next_attempt_at=UPDATED_AT if status in {"pending", "processing"} else None,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _response_body(
    status: WebhookDeliveryJobStatus = "pending",
) -> dict[str, object]:
    return {
        "id": str(JOB_ID),
        "event_id": str(EVENT_ID),
        "status": status,
        "attempt_count": 2,
        "next_attempt_at": (
            "2026-08-01T12:00:00Z" if status in {"pending", "processing"} else None
        ),
        "created_at": "2026-08-01T11:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
    }


def _assert_read_only(
    session: Mock,
    http_dependency: Mock,
    settings_dependency: Mock,
) -> None:
    for method in ("commit", "rollback", "flush", "refresh", "close", "add", "delete"):
        getattr(session, method).assert_not_called()
    http_dependency.assert_not_called()
    settings_dependency.assert_not_called()


def test_routes_are_registered_with_expected_openapi_contract(
    application: FastAPI,
) -> None:
    schema = application.openapi()
    event_operation = schema["paths"]["/webhook-events/{event_id}/delivery-job"]["get"]
    collection_operation = schema["paths"]["/webhook-delivery-jobs"]["get"]

    assert "requestBody" not in event_operation
    assert {parameter["name"] for parameter in event_operation["parameters"]} == {"event_id"}
    assert set(event_operation["responses"]) >= {"200", "404", "409", "422"}
    assert "requestBody" not in collection_operation
    assert {parameter["name"] for parameter in collection_operation["parameters"]} == {
        "status",
        "limit",
        "cursor",
    }
    assert collection_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/WebhookDeliveryJobListResponse"
    }


@pytest.mark.parametrize(
    "job_status",
    ["pending", "processing", "succeeded", "dead_letter"],
)
def test_event_scoped_get_returns_exact_safe_snapshot(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    job_status: WebhookDeliveryJobStatus,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock(return_value=_snapshot(job_status))
    monkeypatch.setattr(api, "get_webhook_delivery_job", service)

    with TestClient(application) as client:
        response = client.get(f"/webhook-events/{EVENT_ID}/delivery-job")

    assert response.status_code == 200
    assert response.json() == _response_body(job_status)
    service.assert_called_once_with(session, event_id=EVENT_ID)
    assert set(response.json()) == {
        "id",
        "event_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }
    _assert_read_only(session, http_dependency, settings_dependency)


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (WebhookDeliveryJobEventNotFoundError("Webhook event not found"), 404),
        (WebhookDeliveryJobNotFoundError("Webhook delivery job not found"), 409),
    ],
)
def test_event_scoped_get_maps_only_application_errors(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: RuntimeError,
    expected_status: int,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock(side_effect=error)
    monkeypatch.setattr(api, "get_webhook_delivery_job", service)

    with TestClient(application) as client:
        response = client.get(f"/webhook-events/{EVENT_ID}/delivery-job")

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}
    service.assert_called_once_with(session, event_id=EVENT_ID)
    _assert_read_only(session, http_dependency, settings_dependency)


def test_event_scoped_invalid_uuid_is_standard_422_before_service(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock()
    monkeypatch.setattr(api, "get_webhook_delivery_job", service)

    with TestClient(application) as client:
        response = client.get("/webhook-events/not-a-uuid/delivery-job")

    assert response.status_code == 422
    service.assert_not_called()
    _assert_read_only(session, http_dependency, settings_dependency)


def test_event_scoped_database_error_propagates_unchanged(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    error = SQLAlchemyError("database unavailable")
    monkeypatch.setattr(api, "get_webhook_delivery_job", Mock(side_effect=error))

    with TestClient(application) as client:
        with pytest.raises(SQLAlchemyError) as raised:
            client.get(f"/webhook-events/{EVENT_ID}/delivery-job")

    assert raised.value is error
    _assert_read_only(session, http_dependency, settings_dependency)


@pytest.mark.parametrize(
    ("query", "expected_status", "expected_limit", "expected_cursor"),
    [
        ("", None, DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT, None),
        ("?status=pending", "pending", 50, None),
        ("?status=processing&limit=1", "processing", 1, None),
        ("?status=succeeded&limit=100", "succeeded", 100, None),
        ("?status=dead_letter&cursor=opaque", "dead_letter", 50, "opaque"),
    ],
)
def test_collection_get_passes_validated_arguments_once(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_status: WebhookDeliveryJobStatus | None,
    expected_limit: int,
    expected_cursor: str | None,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    page = WebhookDeliveryJobPage(items=(_snapshot(),), next_cursor="next-page")
    service = Mock(return_value=page)
    monkeypatch.setattr(api, "list_webhook_delivery_jobs", service)

    with TestClient(application) as client:
        response = client.get(f"/webhook-delivery-jobs{query}")

    assert response.status_code == 200
    assert response.json() == {
        "items": [_response_body()],
        "next_cursor": "next-page",
    }
    service.assert_called_once_with(
        session,
        status=expected_status,
        limit=expected_limit,
        cursor=expected_cursor,
    )
    _assert_read_only(session, http_dependency, settings_dependency)


def test_collection_empty_page_has_exact_envelope(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock(return_value=WebhookDeliveryJobPage(items=(), next_cursor=None))
    monkeypatch.setattr(api, "list_webhook_delivery_jobs", service)

    with TestClient(application) as client:
        response = client.get("/webhook-delivery-jobs")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    _assert_read_only(session, http_dependency, settings_dependency)


@pytest.mark.parametrize(
    "query",
    [
        "?status=invalid",
        "?status=PENDING",
        "?limit=0",
        "?limit=101",
        "?limit=1.5",
    ],
)
def test_collection_fastapi_validation_returns_422_before_service(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock()
    monkeypatch.setattr(api, "list_webhook_delivery_jobs", service)

    with TestClient(application) as client:
        response = client.get(f"/webhook-delivery-jobs{query}")

    assert response.status_code == 422
    service.assert_not_called()
    _assert_read_only(session, http_dependency, settings_dependency)


@pytest.mark.parametrize(
    "error",
    [
        WebhookDeliveryJobStatusValidationError("Invalid webhook delivery job status"),
        WebhookDeliveryJobLimitValidationError("Invalid webhook delivery job limit"),
        WebhookDeliveryJobCursorValidationError("Invalid webhook delivery job cursor"),
    ],
)
def test_collection_maps_precise_service_validation_errors(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: ValueError,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    service = Mock(side_effect=error)
    monkeypatch.setattr(api, "list_webhook_delivery_jobs", service)

    with TestClient(application) as client:
        response = client.get("/webhook-delivery-jobs?cursor=malformed")

    assert response.status_code == 422
    assert response.json() == {"detail": str(error)}
    service.assert_called_once()
    _assert_read_only(session, http_dependency, settings_dependency)


def test_collection_database_error_propagates_unchanged(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, http_dependency, settings_dependency = _configure_session(application)
    error = SQLAlchemyError("database unavailable")
    monkeypatch.setattr(api, "list_webhook_delivery_jobs", Mock(side_effect=error))

    with TestClient(application) as client:
        with pytest.raises(SQLAlchemyError) as raised:
            client.get("/webhook-delivery-jobs")

    assert raised.value is error
    _assert_read_only(session, http_dependency, settings_dependency)


def test_public_schemas_expose_only_operational_fields() -> None:
    job_schema = WebhookDeliveryJobResponse.model_json_schema()
    list_schema = WebhookDeliveryJobListResponse.model_json_schema()

    assert set(job_schema["properties"]) == {
        "id",
        "event_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }
    assert set(list_schema["properties"]) == {"items", "next_cursor"}
    forbidden = {
        "payload",
        "event_type",
        "endpoint_id",
        "target_url",
        "is_active",
        "attempts",
        "response_body",
        "error_message",
        "idempotency_key",
        "has_more",
        "total",
    }
    assert forbidden.isdisjoint(job_schema["properties"])
    assert forbidden.isdisjoint(list_schema["properties"])
