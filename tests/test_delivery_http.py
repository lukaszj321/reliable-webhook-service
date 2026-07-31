import json

import httpx2
import pytest

from reliable_webhook_service.delivery_http import (
    Httpx2WebhookHttpClient,
    WebhookHttpResponse,
    WebhookTimeoutError,
    WebhookTransportError,
)


@pytest.mark.parametrize("error_type_name", ["ReadTimeout", "ConnectError", "HTTP_2Error"])
def test_transport_error_accepts_safe_exception_class_identifier(
    error_type_name: str,
) -> None:
    error = WebhookTransportError(error_type_name=error_type_name)

    assert error.error_type_name == error_type_name
    assert str(error) == f"Webhook transport failed: {error_type_name}"


def test_transport_error_accepts_64_character_identifier() -> None:
    error_type_name = "E" * 64

    error = WebhookTransportError(error_type_name=error_type_name)

    assert error.error_type_name == error_type_name


@pytest.mark.parametrize(
    ("error_type_name", "expected_error"),
    [
        (None, TypeError),
        (42, TypeError),
        ("", ValueError),
        (" ", ValueError),
        ("Connect Error", ValueError),
        ("Connect.Error", ValueError),
        ("connection failed", ValueError),
        ("1ConnectError", ValueError),
        ("Érror", ValueError),
    ],
)
def test_transport_error_rejects_unsafe_exception_class_identifier(
    error_type_name: object,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        WebhookTransportError(error_type_name=error_type_name)  # type: ignore[arg-type]


def test_transport_error_rejects_oversized_exception_class_identifier() -> None:
    with pytest.raises(ValueError):
        WebhookTransportError(error_type_name="E" * 65)


def test_http_client_posts_json_and_returns_status_code() -> None:
    target_url = "https://example.test/webhooks/orders?tenant=alpha"
    payload = {
        "event": "order.created",
        "attempt": 1,
        "active": True,
        "optional": None,
        "order": {"id": "ord-123", "total": 149},
        "items": ["SKU-1", "SKU-2"],
    }
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(202)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        result = webhook_client.post_json(
            target_url=target_url,
            payload=payload,
            timeout_seconds=5.0,
        )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == target_url
    assert request.headers["Content-Type"].startswith("application/json")
    assert json.loads(request.content) == payload
    assert isinstance(result, WebhookHttpResponse)
    assert result.status_code == 202


def test_http_client_does_not_follow_redirects() -> None:
    target_url = "https://example.test/webhooks/redirect"
    redirect_url = "https://redirect.example.test/webhooks/orders"
    requested_urls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested_urls.append(str(request.url))
        if len(requested_urls) == 1:
            return httpx2.Response(302, headers={"Location": redirect_url})
        return httpx2.Response(200)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        result = webhook_client.post_json(
            target_url=target_url,
            payload={"event": "order.created"},
            timeout_seconds=5.0,
        )

    assert requested_urls == [target_url]
    assert redirect_url not in requested_urls
    assert result.status_code == 302


def test_http_client_applies_explicit_timeout() -> None:
    captured_timeout: dict[str, float] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        timeout = request.extensions["timeout"]
        assert isinstance(timeout, dict)
        captured_timeout.update(timeout)
        return httpx2.Response(204)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        result = webhook_client.post_json(
            target_url="https://example.test/webhooks/timeout",
            payload={"event": "order.created"},
            timeout_seconds=2.5,
        )

    assert captured_timeout == {
        "connect": 2.5,
        "read": 2.5,
        "write": 2.5,
        "pool": 2.5,
    }
    assert result.status_code == 204


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        0.0,
        -0.1,
        float("inf"),
        float("-inf"),
        float("nan"),
    ],
)
def test_http_client_rejects_invalid_timeout_before_request(
    timeout_seconds: float,
) -> None:
    request_count = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        with pytest.raises(
            ValueError,
            match="timeout_seconds must be a finite positive number",
        ):
            webhook_client.post_json(
                target_url="https://example.test/webhooks/invalid-timeout",
                payload={"event": "order.created"},
                timeout_seconds=timeout_seconds,
            )

    assert request_count == 0


def test_http_client_translates_read_timeout_without_exposing_details() -> None:
    private_error = "private timeout details"

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout(private_error, request=request)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        with pytest.raises(WebhookTimeoutError) as captured:
            webhook_client.post_json(
                target_url="https://example.test/webhooks/timeout",
                payload={"event": "order.created"},
                timeout_seconds=5.0,
            )

    assert captured.value.error_type_name == "ReadTimeout"
    assert private_error not in str(captured.value)
    assert isinstance(captured.value.__cause__, httpx2.ReadTimeout)


def test_http_client_translates_connect_error_without_exposing_details() -> None:
    private_error = "private connection details"

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(private_error, request=request)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        with pytest.raises(WebhookTransportError) as captured:
            webhook_client.post_json(
                target_url="https://example.test/webhooks/connect-error",
                payload={"event": "order.created"},
                timeout_seconds=5.0,
            )

    assert type(captured.value) is WebhookTransportError
    assert captured.value.error_type_name == "ConnectError"
    assert private_error not in str(captured.value)
    assert isinstance(captured.value.__cause__, httpx2.ConnectError)


def test_http_client_rejects_invalid_request_error_class_name() -> None:
    invalid_request_error_type = type("E" * 65, (httpx2.RequestError,), {})

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise invalid_request_error_type("private transport details", request=request)

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        with pytest.raises(
            ValueError,
            match="^error_type_name must be a 1-64 character ASCII identifier starting with a letter$",
        ) as captured:
            webhook_client.post_json(
                target_url="https://example.test/webhooks/invalid-error-name",
                payload={"event": "order.created"},
                timeout_seconds=5.0,
            )

    assert type(captured.value) is ValueError
    assert isinstance(captured.value.__context__, invalid_request_error_type)


def test_http_client_propagates_non_transport_error_unchanged() -> None:
    unexpected_error = RuntimeError("unexpected programming failure")

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise unexpected_error

    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as client:
        webhook_client = Httpx2WebhookHttpClient(client)
        with pytest.raises(RuntimeError) as captured:
            webhook_client.post_json(
                target_url="https://example.test/webhooks/programming-error",
                payload={"event": "order.created"},
                timeout_seconds=5.0,
            )

    assert captured.value is unexpected_error
