# API documentation

This documentation describes the HTTP endpoints currently available in the FastAPI application.

## Available API areas

- Health check
- [Webhook endpoint configuration](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
- Manual webhook replay through the [Webhook event API](webhook-events.md#manual-replay)
- [Webhook delivery job API](webhook-delivery-jobs.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)

## Health check

```text
GET /health
```

The health check returns HTTP 200 and can be used to confirm that the application is available.

```json
{
  "status": "ok"
}
```

## Webhook endpoint configuration

The API supports creating webhook endpoint configurations and listing stored configurations.

Available routes:

- `POST /webhook-endpoints`
- `GET /webhook-endpoints`

See [Webhook endpoint API](webhook-endpoints.md) for request, response, and validation details.

## Webhook event API

`POST /webhook-events` accepts an optional endpoint-scoped `Idempotency-Key` header. A new event
and its initial `pending` `WebhookDeliveryJob` are stored atomically and return HTTP 201. An
equivalent keyed retry reuses the event without creating another job and returns HTTP 200;
conflicting key reuse returns HTTP 409, and an invalid key returns HTTP 422. Both successful
statuses use the same event-only response schema and do not expose the key. An inactive endpoint
is accepted, while a request that references a missing endpoint returns HTTP 404. The route does
not execute synchronous delivery.

See [Webhook event API](webhook-events.md) for request, response, validation, persistence, and error
details.

The same API area exposes `POST /webhook-events/{event_id}/replay`. It accepts no body, returns
HTTP 202, and moves an existing `succeeded` or `dead_letter` job to `pending` with a fresh worker
retry-cycle budget. Replay performs no downstream HTTP; missing events return 404, while endpoint,
job, and active-state conflicts return 409.

## Webhook delivery attempt API

Available routes:

- `POST /webhook-events/{event_id}/delivery-attempts` manually executes and persists one
  synchronous delivery attempt. It returns HTTP 201 for both `succeeded` and expected `failed`
  outcomes.
- `GET /webhook-events/{event_id}/delivery-attempts` reads stored completed delivery attempts for
  one existing event. It returns an empty list when the event has no attempts and HTTP 404 when the
  event does not exist.

Creating an event through `POST /webhook-events` creates its durable pending job but does not invoke
manual delivery automatically.

See [Webhook delivery attempt API](webhook-delivery-attempts.md) for response fields, ordering,
manual execution behavior, outcomes, empty results, and errors.

## Webhook delivery job API

`GET /webhook-events/{event_id}/delivery-job` returns the current operational job snapshot for one
event. `GET /webhook-delivery-jobs` lists jobs with an optional status filter, bounded limit, and
opaque cursor pagination.

See [Webhook delivery job API](webhook-delivery-jobs.md) for response fields, status meanings,
ordering, pagination, read-only semantics, and replay race behavior.

## Interactive documentation

FastAPI exposes interactive API documentation when the application is running locally:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Navigation

- [Webhook endpoint API](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
- [Webhook delivery job API](webhook-delivery-jobs.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Documentation index](../index.md)
- [Development setup](../development.md)
- [Project README](../../README.md)
