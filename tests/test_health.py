from collections.abc import Iterator
from typing import Self
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from reliable_webhook_service import main, operations_api
from reliable_webhook_service.database import get_session
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)


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


def test_health_remains_dependency_free_liveness(
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_dependency = Mock(side_effect=SQLAlchemyError("database unavailable"))
    settings_dependency = Mock(side_effect=AssertionError("settings must not be resolved"))
    http_dependency = Mock(side_effect=AssertionError("HTTP dependency must not be resolved"))
    readiness_service = Mock()
    summary_service = Mock()
    application.dependency_overrides[get_session] = database_dependency
    application.dependency_overrides[get_settings] = settings_dependency
    application.dependency_overrides[get_webhook_http_client] = http_dependency
    monkeypatch.setattr(operations_api, "check_database_readiness", readiness_service)
    monkeypatch.setattr(operations_api, "get_webhook_operational_summary", summary_service)

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    database_dependency.assert_not_called()
    settings_dependency.assert_not_called()
    http_dependency.assert_not_called()
    readiness_service.assert_not_called()
    summary_service.assert_not_called()


def test_health_openapi_has_no_body_or_parameters(application: FastAPI) -> None:
    operation = application.openapi()["paths"]["/health"]["get"]

    assert "requestBody" not in operation
    assert "parameters" not in operation
