from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI

from reliable_webhook_service.api import (
    router as webhook_endpoint_router,
)
from reliable_webhook_service.api import (
    webhook_delivery_job_router,
    webhook_event_router,
)
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.operations_api import router as operations_router


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    with httpx2.Client() as raw_http_client:
        application.state.webhook_http_client = Httpx2WebhookHttpClient(raw_http_client)
        try:
            yield
        finally:
            del application.state.webhook_http_client


def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    application = FastAPI(
        title="Reliable Webhook Delivery Service",
        lifespan=lifespan,
    )
    application.include_router(webhook_endpoint_router)
    application.include_router(webhook_event_router)
    application.include_router(webhook_delivery_job_router)
    application.include_router(operations_router)
    application.add_api_route("/health", health, methods=["GET"])
    return application


app = create_app()
