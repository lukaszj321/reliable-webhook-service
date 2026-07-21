# Development setup

This guide explains how to configure, run, and validate the project in a local Windows PowerShell
environment.

## Contents

- [Requirements](#requirements)
- [Create a virtual environment](#create-a-virtual-environment)
- [Install the project](#install-the-project)
- [Configure the local environment](#configure-the-local-environment)
- [Start PostgreSQL](#start-postgresql)
- [Resolve a PostgreSQL port conflict](#resolve-a-postgresql-port-conflict)
- [Apply database migrations](#apply-database-migrations)
- [Run the application](#run-the-application)
- [Health check and API documentation](#health-check-and-api-documentation)
- [Quality checks](#quality-checks)
- [Stop PostgreSQL](#stop-postgresql)

## Requirements

Python 3.12 is required. Local database commands also require Docker with Docker Compose.

## Create a virtual environment

Create a virtual environment from the repository root:

```powershell
python -m venv .venv
```

Activate it in Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Install the project

Install the package in editable mode with development dependencies:

```powershell
python -m pip install -e ".[dev]"
```

## Configure the local environment

Create the local environment file from the public development example:

```powershell
Copy-Item .env.example .env
```

The application, Alembic, and tests load the database connection from `DATABASE_URL`. Docker
Compose uses the PostgreSQL values and `POSTGRES_PORT` from the same local `.env` file. The file is
ignored by Git and should contain only local settings, not committed secrets.

## Start PostgreSQL

Start the PostgreSQL service and inspect its status:

```powershell
docker compose up -d postgres
docker compose ps
```

The default host port is 5432.

## Resolve a PostgreSQL port conflict

If host port 5432 is already occupied, update both `POSTGRES_PORT` and `DATABASE_URL` in `.env` to
use the same alternative port. For example:

```dotenv
POSTGRES_PORT=5433
DATABASE_URL=postgresql+psycopg://reliable_webhook:reliable_webhook@127.0.0.1:5433/reliable_webhook
```

`POSTGRES_PORT` controls the Docker Compose host port mapping. `DATABASE_URL` controls database
connections made by the application, Alembic, and tests.

## Apply database migrations

After PostgreSQL is healthy, upgrade the database to the latest revision:

```powershell
python -m alembic upgrade head
```

Detailed migration and schema documentation will be available in the database guide.

## Run the application

Start FastAPI with automatic reload for local development:

```powershell
python -m uvicorn reliable_webhook_service.main:app --reload
```

The application is available at:

```text
http://127.0.0.1:8000
```

## Health check and API documentation

The health endpoint is:

```text
GET /health
```

Interactive Swagger UI is available at:

```text
http://127.0.0.1:8000/docs
```

## Quality checks

The full test suite and Alembic check require a running PostgreSQL service with migrations applied.

```powershell
python -m pytest -W error
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m alembic check
```

## Stop PostgreSQL

Stop the service while preserving its named volume and stored data:

```powershell
docker compose down
```

To intentionally remove the named volume and its data, use:

```powershell
docker compose down --volumes
```

The second command permanently removes the local PostgreSQL data stored in the Compose volume.

## Navigation

- [Documentation index](index.md)
- [Project README](../README.md)
