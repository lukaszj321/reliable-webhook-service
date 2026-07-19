# Reliable Webhook Delivery Service

A FastAPI service for reliable webhook ingestion and delivery.

## Current scope

- Python 3.12 package foundation
- FastAPI application
- GET /health endpoint
- Automated health endpoint test
- Ruff and mypy configuration

## Planned scope

- Webhook endpoint configuration
- Webhook event ingestion
- Asynchronous delivery
- Retry and backoff
- Idempotency
- Delivery attempt history
- Manual replay

## Non-goals

- Authentication
- Frontend

## Development setup

Python 3.12 is required.

Create and activate a virtual environment on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install the package with development dependencies:

```powershell
python -m pip install -e ".[dev]"
```

## Run locally

```powershell
python -m uvicorn reliable_webhook_service.main:app --reload
```

The application will be available at:

```text
http://127.0.0.1:8000
```

## Health check

```text
GET /health
```

Expected response:

```json
{
  "status": "ok"
}
```

## Quality checks

```powershell
python -m pytest -W error
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```
