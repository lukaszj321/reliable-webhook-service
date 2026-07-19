from fastapi import FastAPI

app = FastAPI(title="Reliable Webhook Delivery Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
