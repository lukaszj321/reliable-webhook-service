# Operational endpoints

The operational endpoints separate process liveness, database readiness, and a safe aggregate
view of the webhook delivery queue.

## Contents

- [Endpoint roles](#endpoint-roles)
  - [Liveness and readiness](#liveness-and-readiness)
- [Liveness](#liveness)
- [Database readiness](#database-readiness)
  - [Successful response](#successful-response)
  - [Database unavailable](#database-unavailable)
- [Queue summary](#queue-summary)
  - [Response](#response)
  - [Delivery job counts](#delivery-job-counts)
  - [Time boundaries](#time-boundaries)
  - [Snapshot semantics](#snapshot-semantics)
- [Privacy and access](#privacy-and-access)
- [Deployment use](#deployment-use)
- [Current boundaries](#current-boundaries)
- [Source](#source)
- [Navigation](#navigation)

---

## Endpoint roles

| Endpoint | Purpose | Dependency check |
|---|---|---|
| `GET /health` | Confirm that the API process can serve requests | None |
| `GET /ready` | Confirm that PostgreSQL accepts a minimal query | PostgreSQL only |
| `GET /operations/summary` | Inspect aggregate delivery job state | PostgreSQL only |

### Liveness and readiness

Liveness and readiness answer different questions. Liveness reports that the application process
is responsive. Readiness reports whether the application can currently use its required database.
A database outage therefore changes `/ready` to HTTP 503 without changing the dependency-free
`/health` contract.

[Back to contents](#contents)

---

## Liveness

```text
GET /health
```

The endpoint returns HTTP 200 with the exact response:

```json
{
  "status": "ok"
}
```

It does not resolve a database session, settings, or the outbound webhook HTTP client. It does not
check PostgreSQL, the worker process, downstream webhook targets, or migration state.

[Back to contents](#contents)

---

## Database readiness

```text
GET /ready
```

Readiness performs one minimal SQLAlchemy query equivalent to `SELECT 1`. It is read-only and does
not commit, roll back, flush, lock rows, run migrations, or make an HTTP request.

### Successful response

When PostgreSQL returns the expected value, the endpoint returns HTTP 200:

```json
{
  "status": "ready",
  "checks": {
    "database": "ok"
  }
}
```

### Database unavailable

An expected SQLAlchemy database failure returns HTTP 503 with the same public response schema:

```json
{
  "status": "not_ready",
  "checks": {
    "database": "unavailable"
  }
}
```

The warning log contains only the stable event name `database_readiness_failed` and the exception
class. It does not include the exception message, SQL, a database URL, credentials, or a
traceback. Programming errors are not converted into readiness failures.

[Back to contents](#contents)

---

## Queue summary

```text
GET /operations/summary
```

The endpoint returns HTTP 200 after executing one conditional aggregate statement against
`webhook_delivery_jobs`. An expected SQLAlchemy database failure returns HTTP 503:

```json
{
  "detail": "Operational summary unavailable"
}
```

### Response

```json
{
  "generated_at": "2026-08-02T12:00:00Z",
  "delivery_jobs": {
    "pending": 8,
    "processing": 2,
    "succeeded": 125,
    "dead_letter": 3,
    "due_pending": 4,
    "stale_processing": 1
  },
  "oldest_due_pending_at": "2026-08-02T11:45:00Z",
  "oldest_processing_updated_at": "2026-08-02T11:50:00Z",
  "stale_processing_before": "2026-08-02T11:55:00Z"
}
```

When no matching job exists, a count is `0` and the corresponding oldest timestamp is `null`.
All non-null timestamps are timezone-aware UTC values.

### Delivery job counts

| Field | Meaning |
|---|---|
| `pending` | All jobs with status `pending` |
| `processing` | All jobs with status `processing` |
| `succeeded` | All jobs with status `succeeded` |
| `dead_letter` | All jobs with status `dead_letter` |
| `due_pending` | Pending jobs whose `next_attempt_at` is due |
| `stale_processing` | Processing jobs older than the configured stale threshold |

The summary intentionally does not expose a total event count, total attempt count, payload,
event type, endpoint or target URL, idempotency key, attempt error, SQL, or exception details.

### Time boundaries

`generated_at` is captured once for a request. A pending job is due when:

```text
status = pending AND next_attempt_at <= generated_at
```

The boundary is inclusive: a job scheduled exactly at `generated_at` is due.
`oldest_due_pending_at` is the minimum `next_attempt_at` among due pending jobs.

The existing `WEBHOOK_WORKER_STALE_PROCESSING_TIMEOUT_SECONDS` setting defines:

```text
stale_processing_before = generated_at - stale processing timeout
```

A processing job is stale when:

```text
status = processing AND updated_at < stale_processing_before
```

The boundary is exclusive: a job updated exactly at `stale_processing_before` is not stale.
`oldest_processing_updated_at` is the minimum `updated_at` among all processing jobs, not only
stale jobs.

### Snapshot semantics

The response reflects committed rows visible to the database transaction that executes the
aggregate statement. An uncommitted job in another session is not included. The endpoint does not
mutate jobs or create delivery attempts.

The summary is observational rather than a lock or reservation. Normal races remain possible:
after the GET completes, a worker or API transaction can immediately change the queue. Operators
should treat the response as a point-in-time signal, not as a durable scheduling decision.

[Back to contents](#contents)

---

## Privacy and access

Responses contain only stable status values, aggregate counts, and operational timestamps. They
do not expose webhook content, target details, credentials, database details, error messages, or
internal SQL.

These endpoints are currently unauthenticated. In a deployment, restrict operator-facing access
to `/ready` and `/operations/summary` through the gateway, network policy, or future application
authorization. Do not assume that safe aggregate fields make unrestricted public access
appropriate.

[Back to contents](#contents)

---

## Deployment use

Container platforms and orchestrators can use:

- `/health` as a liveness probe;
- `/ready` as a readiness probe;
- `/operations/summary` for operator inspection, not as a liveness probe.

The probes only describe the API process and its PostgreSQL dependency. They do not verify that a
worker process is alive, that downstream webhook targets are reachable, or that the database is at
the current Alembic migration head.

[Back to contents](#contents)

---

## Current boundaries

This feature does not add Prometheus metrics, OpenTelemetry, tracing, worker heartbeats, dashboards,
alerts, remote target checks, or migration execution. Existing worker logging and execution
behavior are unchanged.

[Back to contents](#contents)

---

## Source

- [Operations API](../src/reliable_webhook_service/operations_api.py)
- [Operations service](../src/reliable_webhook_service/operations_service.py)
- [Response schemas](../src/reliable_webhook_service/schemas.py)
- [Application registration](../src/reliable_webhook_service/main.py)

## Navigation

- [Documentation index](index.md)
- [Architecture](architecture.md)
- [API documentation](api/index.md)
- [Webhook delivery job API](api/webhook-delivery-jobs.md)
- [Webhook delivery execution](delivery-execution.md)
- [Database and migrations](database.md)
- [Development setup](development.md)
- [Project README](../README.md)
