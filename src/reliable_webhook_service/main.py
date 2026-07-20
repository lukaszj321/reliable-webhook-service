from fastapi import FastAPI

from reliable_webhook_service.api import router as webhook_endpoint_router

app = FastAPI(title="Reliable Webhook Delivery Service")
app.include_router(webhook_endpoint_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
