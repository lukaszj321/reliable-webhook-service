from typing import Self

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from reliable_webhook_service import main
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.dependencies import (
    WEBHOOK_HTTP_CLIENT_NOT_INITIALIZED,
    get_webhook_http_client,
)


class FakeRawHttpClient:
    def __init__(self) -> None:
        self.close_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.close_count += 1


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
) -> list[FakeRawHttpClient]:
    instances: list[FakeRawHttpClient] = []

    def create_client() -> FakeRawHttpClient:
        client = FakeRawHttpClient()
        instances.append(client)
        return client

    monkeypatch.setattr(main.httpx2, "Client", create_client)
    return instances


def _request_for(application: FastAPI) -> Request:
    return Request({"type": "http", "app": application})


def test_module_app_exists_without_initialized_http_client() -> None:
    assert isinstance(main.app, FastAPI)
    assert not hasattr(main.app.state, "webhook_http_client")


def test_create_app_does_not_create_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = _install_fake_client(monkeypatch)

    application = main.create_app()

    assert instances == []
    assert not hasattr(application.state, "webhook_http_client")


def test_lifespan_initializes_reuses_and_closes_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = _install_fake_client(monkeypatch)
    application = main.create_app()
    request = _request_for(application)

    with TestClient(application):
        assert len(instances) == 1
        assert instances[0].close_count == 0
        stored_client = application.state.webhook_http_client
        assert isinstance(stored_client, Httpx2WebhookHttpClient)
        assert get_webhook_http_client(request) is stored_client
        assert get_webhook_http_client(request) is stored_client

    assert instances[0].close_count == 1
    assert not hasattr(application.state, "webhook_http_client")
    with pytest.raises(
        RuntimeError,
        match=f"^{WEBHOOK_HTTP_CLIENT_NOT_INITIALIZED}$",
    ):
        get_webhook_http_client(request)


def test_new_application_lifespan_creates_new_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances = _install_fake_client(monkeypatch)

    first_application = main.create_app()
    with TestClient(first_application):
        first_wrapper = first_application.state.webhook_http_client

    second_application = main.create_app()
    with TestClient(second_application):
        second_wrapper = second_application.state.webhook_http_client

    assert len(instances) == 2
    assert instances[0] is not instances[1]
    assert instances[0].close_count == 1
    assert instances[1].close_count == 1
    assert first_wrapper is not second_wrapper


def test_dependency_requires_active_lifespan() -> None:
    application = main.create_app()

    with pytest.raises(
        RuntimeError,
        match=f"^{WEBHOOK_HTTP_CLIENT_NOT_INITIALIZED}$",
    ):
        get_webhook_http_client(_request_for(application))


def test_health_and_business_routes_match_current_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch)
    application = main.create_app()

    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    schema = application.openapi()
    business_routes = sorted(
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
    )
    assert business_routes == sorted(
        [
            ("GET", "/health"),
            ("GET", "/webhook-endpoints"),
            ("POST", "/webhook-endpoints"),
            ("POST", "/webhook-events"),
            ("GET", "/webhook-events/{event_id}/delivery-attempts"),
            ("POST", "/webhook-events/{event_id}/delivery-attempts"),
        ]
    )
    assert business_routes.count(("GET", "/webhook-events/{event_id}/delivery-attempts")) == 1
    assert business_routes.count(("POST", "/webhook-events/{event_id}/delivery-attempts")) == 1
    assert business_routes.count(("POST", "/webhook-delivery-attempts")) == 0
