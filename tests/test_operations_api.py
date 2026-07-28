import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Self
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service import main, operations_api
from reliable_webhook_service.config import Settings
from reliable_webhook_service.database import get_session
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.operations_service import (
    DatabaseReadinessResult,
    WebhookDeliveryJobOperationalCounts,
    WebhookOperationalSummary,
)

GENERATED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
STALE_BEFORE = datetime(2026, 8, 2, 11, 55, tzinfo=UTC)


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


def _configure_dependencies(application: FastAPI) -> tuple[Mock, Settings, Mock, Mock]:
    session = Mock(spec=Session)
    settings = Settings(webhook_worker_stale_processing_timeout_seconds=123.0)
    settings_dependency = Mock()
    http_dependency = Mock(side_effect=AssertionError("HTTP dependency must not be resolved"))

    def override_settings() -> Settings:
        settings_dependency()
        return settings

    application.dependency_overrides[get_session] = lambda: session
    application.dependency_overrides[get_settings] = override_settings
    application.dependency_overrides[get_webhook_http_client] = http_dependency
    return session, settings, settings_dependency, http_dependency


def _assert_read_only(session: Mock, http_dependency: Mock) -> None:
    for method in (
        "add",
        "add_all",
        "delete",
        "flush",
        "commit",
        "rollback",
        "refresh",
        "close",
        "begin",
        "begin_nested",
    ):
        getattr(session, method).assert_not_called()
    http_dependency.assert_not_called()


def _summary(*, populated: bool) -> WebhookOperationalSummary:
    return WebhookOperationalSummary(
        generated_at=GENERATED_AT,
        delivery_jobs=WebhookDeliveryJobOperationalCounts(
            pending=3 if populated else 0,
            processing=2 if populated else 0,
            succeeded=5 if populated else 0,
            dead_letter=1 if populated else 0,
            due_pending=2 if populated else 0,
            stale_processing=1 if populated else 0,
        ),
        oldest_due_pending_at=GENERATED_AT if populated else None,
        oldest_processing_updated_at=STALE_BEFORE if populated else None,
        stale_processing_before=STALE_BEFORE,
    )


def test_operational_routes_are_registered_with_openapi_models(
    application: FastAPI,
) -> None:
    schema = application.openapi()
    assert {"/health", "/ready", "/operations/summary"} <= set(schema["paths"])
    readiness = schema["paths"]["/ready"]["get"]
    summary = schema["paths"]["/operations/summary"]["get"]

    assert set(readiness["responses"]) >= {"200", "503"}
    assert readiness["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert readiness["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadinessResponse"
    }
    assert set(summary["responses"]) >= {"200", "503"}


def test_readiness_success_returns_exact_response_without_settings_or_http(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, settings_dependency, http_dependency = _configure_dependencies(application)
    service = Mock(return_value=DatabaseReadinessResult(database="ok"))
    monkeypatch.setattr(operations_api, "check_database_readiness", service)

    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok"},
    }
    service.assert_called_once_with(session)
    settings_dependency.assert_not_called()
    _assert_read_only(session, http_dependency)


def test_readiness_database_failure_is_safe_and_logs_one_warning(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, _, settings_dependency, http_dependency = _configure_dependencies(application)
    secret = "postgresql://user:password@secret-host/db SELECT 1"
    service = Mock(side_effect=SQLAlchemyError(secret))
    monkeypatch.setattr(operations_api, "check_database_readiness", service)
    monkeypatch.setattr(operations_api.logger, "disabled", False)
    caplog.set_level(logging.WARNING, logger=operations_api.__name__)

    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "unavailable"},
    }
    warnings = [record for record in caplog.records if record.name == operations_api.__name__]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == ("database_readiness_failed error_type=SQLAlchemyError")
    assert warnings[0].exc_info is None
    combined = f"{response.text} {warnings[0].getMessage()}"
    for forbidden in (
        secret,
        "postgresql://",
        "user",
        "password",
        "secret-host",
        "SELECT 1",
    ):
        assert forbidden not in combined
    settings_dependency.assert_not_called()
    _assert_read_only(session, http_dependency)


@pytest.mark.parametrize("error", [RuntimeError("programming error"), ValueError("invalid")])
def test_readiness_does_not_map_programming_errors_to_503(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    session, _, settings_dependency, http_dependency = _configure_dependencies(application)
    monkeypatch.setattr(
        operations_api,
        "check_database_readiness",
        Mock(side_effect=error),
    )

    with TestClient(application) as client:
        with pytest.raises(type(error)) as raised:
            client.get("/ready")

    assert raised.value is error
    settings_dependency.assert_not_called()
    _assert_read_only(session, http_dependency)


@pytest.mark.parametrize("populated", [False, True])
def test_summary_success_returns_exact_safe_response(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    populated: bool,
) -> None:
    session, settings, settings_dependency, http_dependency = _configure_dependencies(application)
    clock = Mock(return_value=GENERATED_AT)
    result = _summary(populated=populated)
    service = Mock(return_value=result)
    monkeypatch.setattr(operations_api, "_utc_now", clock)
    monkeypatch.setattr(operations_api, "get_webhook_operational_summary", service)

    with TestClient(application) as client:
        response = client.get("/operations/summary")

    assert response.status_code == 200
    assert response.json() == {
        "generated_at": "2026-08-02T12:00:00Z",
        "delivery_jobs": {
            "pending": 3 if populated else 0,
            "processing": 2 if populated else 0,
            "succeeded": 5 if populated else 0,
            "dead_letter": 1 if populated else 0,
            "due_pending": 2 if populated else 0,
            "stale_processing": 1 if populated else 0,
        },
        "oldest_due_pending_at": ("2026-08-02T12:00:00Z" if populated else None),
        "oldest_processing_updated_at": ("2026-08-02T11:55:00Z" if populated else None),
        "stale_processing_before": "2026-08-02T11:55:00Z",
    }
    assert set(response.json()) == {
        "generated_at",
        "delivery_jobs",
        "oldest_due_pending_at",
        "oldest_processing_updated_at",
        "stale_processing_before",
    }
    clock.assert_called_once_with()
    service.assert_called_once_with(
        session,
        generated_at=GENERATED_AT,
        stale_processing_timeout_seconds=(settings.webhook_worker_stale_processing_timeout_seconds),
    )
    settings_dependency.assert_called_once()
    _assert_read_only(session, http_dependency)


def test_summary_database_failure_maps_to_503_without_logging(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session, _, settings_dependency, http_dependency = _configure_dependencies(application)
    error = SQLAlchemyError("secret database detail")
    monkeypatch.setattr(operations_api, "_utc_now", Mock(return_value=GENERATED_AT))
    monkeypatch.setattr(
        operations_api,
        "get_webhook_operational_summary",
        Mock(side_effect=error),
    )
    caplog.set_level(logging.WARNING, logger=operations_api.__name__)

    with TestClient(application) as client:
        response = client.get("/operations/summary")

    assert response.status_code == 503
    assert response.json() == {"detail": "Operational summary unavailable"}
    assert not [record for record in caplog.records if record.name == operations_api.__name__]
    settings_dependency.assert_called_once()
    _assert_read_only(session, http_dependency)


@pytest.mark.parametrize("error", [RuntimeError("programming error"), ValueError("invalid")])
def test_summary_does_not_map_programming_errors_to_503(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    session, _, settings_dependency, http_dependency = _configure_dependencies(application)
    monkeypatch.setattr(operations_api, "_utc_now", Mock(return_value=GENERATED_AT))
    monkeypatch.setattr(
        operations_api,
        "get_webhook_operational_summary",
        Mock(side_effect=error),
    )

    with TestClient(application) as client:
        with pytest.raises(type(error)) as raised:
            client.get("/operations/summary")

    assert raised.value is error
    settings_dependency.assert_called_once()
    _assert_read_only(session, http_dependency)


def test_summary_response_contains_no_sensitive_or_internal_fields() -> None:
    schema = operations_api.WebhookOperationalSummaryResponse.model_json_schema()
    serialized = operations_api.WebhookOperationalSummaryResponse.model_validate(
        {
            "generated_at": GENERATED_AT,
            "delivery_jobs": {
                "pending": 0,
                "processing": 0,
                "succeeded": 0,
                "dead_letter": 0,
                "due_pending": 0,
                "stale_processing": 0,
            },
            "oldest_due_pending_at": None,
            "oldest_processing_updated_at": None,
            "stale_processing_before": STALE_BEFORE,
        }
    ).model_dump()
    forbidden = {
        "payload",
        "event_type",
        "endpoint_url",
        "target_url",
        "idempotency_key",
        "error_message",
        "exception",
        "sql",
        "total",
    }
    assert forbidden.isdisjoint(str(schema).lower())
    assert forbidden.isdisjoint(str(serialized).lower())
