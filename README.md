# Reliable Webhook Delivery Service

A Python service intended to provide reliable webhook delivery.

## Current scope

- Python 3.12 package foundation
- Development tooling configuration

## Planned scope

- FastAPI application and health endpoint
- Reliable webhook ingestion, delivery, retry, and replay

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
