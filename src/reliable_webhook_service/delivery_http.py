import math
from dataclasses import dataclass
from typing import Protocol

import httpx2

from reliable_webhook_service.models import JsonValue

__all__ = [
    "Httpx2WebhookHttpClient",
    "WebhookHttpClient",
    "WebhookHttpResponse",
    "WebhookTimeoutError",
    "WebhookTransportError",
]


class WebhookTransportError(RuntimeError):
    def __init__(self, *, error_type_name: str) -> None:
        self.error_type_name = error_type_name
        super().__init__(f"Webhook transport failed: {error_type_name}")


class WebhookTimeoutError(WebhookTransportError):
    pass


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

        try:
            response = self._client.post(
                target_url,
                json=payload,
                timeout=timeout_seconds,
                follow_redirects=False,
            )
        except httpx2.TimeoutException as error:
            raise WebhookTimeoutError(error_type_name=type(error).__name__) from error
        except httpx2.RequestError as error:
            raise WebhookTransportError(error_type_name=type(error).__name__) from error
        return WebhookHttpResponse(status_code=response.status_code)
