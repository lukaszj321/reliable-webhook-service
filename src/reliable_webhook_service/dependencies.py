from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request

from reliable_webhook_service.config import Settings
from reliable_webhook_service.delivery_http import WebhookHttpClient

WEBHOOK_HTTP_CLIENT_NOT_INITIALIZED = "Webhook HTTP client is not initialized"


def get_webhook_http_client(request: Request) -> WebhookHttpClient:
    try:
        client = request.app.state.webhook_http_client
    except AttributeError as error:
        raise RuntimeError(WEBHOOK_HTTP_CLIENT_NOT_INITIALIZED) from error

    return cast(WebhookHttpClient, client)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


WebhookHttpClientDependency = Annotated[
    WebhookHttpClient,
    Depends(get_webhook_http_client),
]

SettingsDependency = Annotated[
    Settings,
    Depends(get_settings),
]
