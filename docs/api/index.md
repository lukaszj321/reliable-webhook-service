# API documentation

This documentation describes the HTTP endpoints currently available in the FastAPI application.

## Available API areas

- Health check
- [Webhook endpoint configuration](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
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

`POST /webhook-events` validates a webhook event request and stores the event in PostgreSQL for an
existing webhook endpoint. A request that references a missing endpoint returns HTTP 404.

See [Webhook event API](webhook-events.md) for request, response, validation, persistence, and error
details.

## Webhook delivery attempt API

`GET /webhook-events/{event_id}/delivery-attempts` reads stored completed delivery attempts for one
existing event. It returns an empty list when the event has no attempts and HTTP 404 when the event
does not exist. The endpoint does not create attempts automatically.

See [Webhook delivery attempt API](webhook-delivery-attempts.md) for response fields, ordering,
empty results, errors, and read-only behavior.

## Interactive documentation

FastAPI exposes interactive API documentation when the application is running locally:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Navigation

- [Webhook endpoint API](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Documentation index](../index.md)
- [Development setup](../development.md)
- [Project README](../../README.md)
