from fastapi import FastAPI

from reliable_webhook_service.api import (
    router as webhook_endpoint_router,
)
from reliable_webhook_service.api import (
    webhook_event_router,
)

app = FastAPI(title="Reliable Webhook Delivery Service")
app.include_router(webhook_endpoint_router)
app.include_router(webhook_event_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
