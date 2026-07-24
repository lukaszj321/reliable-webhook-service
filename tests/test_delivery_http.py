import json

import httpx2
import pytest

from reliable_webhook_service.delivery_http import (
    Httpx2WebhookHttpClient,
    WebhookHttpResponse,
)


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
