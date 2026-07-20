# Reliable Webhook Delivery Service

A FastAPI service for reliable webhook ingestion and delivery.

## Current scope

- Python 3.12 package foundation
- FastAPI application
- GET /health endpoint
- Automated health endpoint test
- Ruff and mypy configuration
- PostgreSQL persistence foundation
- SQLAlchemy engine and session factory
- Alembic migration environment
- Docker Compose PostgreSQL service
- PostgreSQL connectivity test

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

## Database setup

Create a local environment file on Windows:

```powershell
Copy-Item .env.example .env
```

The `.env` file contains local development values only and is ignored by Git.

Start PostgreSQL and check its status:

```powershell
docker compose up -d postgres
docker compose ps
```

### Port conflicts

The default PostgreSQL host port is 5432. If it is occupied, update both values in `.env`
consistently, for example:

```dotenv
POSTGRES_PORT=5433
DATABASE_URL=postgresql+psycopg://reliable_webhook:reliable_webhook@127.0.0.1:5433/reliable_webhook
```

`POSTGRES_PORT` controls the Docker Compose port mapping. `DATABASE_URL` controls database
connections for the application, Alembic, and tests.

## Database migrations

```powershell
python -m alembic upgrade head
```

The Alembic environment is configured, but no revisions exist yet because the project has no
domain models.

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

The full test suite requires a running PostgreSQL service.

```powershell
python -m pytest -W error
python -m ruff check .
python -m ruff format --check .
python -m mypy src
```

## Stop the database

Stop PostgreSQL while preserving its named volume and data:

```powershell
docker compose down
```

To intentionally remove the local PostgreSQL volume and its data, use:

```powershell
docker compose down --volumes
```
