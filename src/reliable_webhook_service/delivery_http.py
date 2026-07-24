import math
from dataclasses import dataclass
from typing import Protocol

import httpx2

from reliable_webhook_service.models import JsonValue

__all__ = [
    "Httpx2WebhookHttpClient",
    "WebhookHttpClient",
    "WebhookHttpResponse",
]


@dataclass(frozen=True, slots=True)
class WebhookHttpResponse:
    status_code: int


class WebhookHttpClient(Protocol):
    def post_json(
        self,
        *,
        target_url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
    ) -> WebhookHttpResponse: ...


class Httpx2WebhookHttpClient:
    def __init__(self, client: httpx2.Client) -> None:
        self._client = client

    def post_json(
        self,
        *,
        target_url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
    ) -> WebhookHttpResponse:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be a finite positive number")

        response = self._client.post(
            target_url,
            json=payload,
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        return WebhookHttpResponse(status_code=response.status_code)
