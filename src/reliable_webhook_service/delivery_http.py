import math
import re
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

_ERROR_TYPE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")


class WebhookTransportError(RuntimeError):
    """Expected transport failure with a safe, stable exception-class identifier.

    ``error_type_name`` is limited to 1-64 ASCII characters. It must start with an
    ASCII letter and may otherwise contain only ASCII letters, digits, and underscores.
    """

    def __init__(self, *, error_type_name: str) -> None:
        if not isinstance(error_type_name, str):
            raise TypeError("error_type_name must be a string")
        if _ERROR_TYPE_NAME_PATTERN.fullmatch(error_type_name) is None:
            raise ValueError(
                "error_type_name must be a 1-64 character ASCII identifier starting with a letter"
            )
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
    ) -> WebhookHttpResponse:
        """Send JSON or raise a normalized expected transport failure.

        Implementations raise ``WebhookTimeoutError`` for transport timeouts and
        ``WebhookTransportError`` for other expected transport failures. Unexpected
        programming or contract errors propagate unchanged.
        """
        ...


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
