# Webhook Event API

This API stores webhook events for existing webhook endpoint configurations.

## Contents

- [Endpoint](#endpoint)
- [Request body](#request-body)
- [Successful response](#successful-response)
- [Error responses](#error-responses)
- [Persistence behavior](#persistence-behavior)
- [Non-goals and current limitations](#non-goals-and-current-limitations)
- [Navigation](#navigation)

## Endpoint

- Method: `POST`
- Path: `/webhook-events`
- Success status: `201 Created`
- Content-Type: `application/json`

## Request body

The request contains three required fields.

`endpoint_id`:

- must be a valid UUID;
- must reference an existing `WebhookEndpoint`;
- returns HTTP 422 when its UUID format is invalid;
- returns HTTP 404 when the UUID is valid but the endpoint does not exist;
- can reference an endpoint whose `is_active` value is `false`.

`event_type`:

- must be a string;
- has leading and trailing whitespace removed;
- must contain at least 1 character after trimming;
- has a maximum length of 255 characters;
- is not restricted by an enum or event type registry.

`payload`:

- must be a top-level JSON object;
- supports nested objects and lists within that object;
- supports strings, integers, floating-point numbers, Boolean values, and `null`;
- returns HTTP 422 when the top-level value is an array, scalar, or `null`;
- has no configured size limit or event-specific schema.

Example request:

```json
{
  "endpoint_id": "5dce6a1d-f4c7-4c16-b709-2b0d08683ed2",
  "event_type": "  order.created  ",
  "payload": {
    "order": {
      "id": "ord_12345",
      "amount": 149.99,
      "paid": true
    },
    "items": [
      {
        "sku": "SKU-1",
        "quantity": 2
      }
    ],
    "note": null
  }
}
```

## Successful response

The endpoint returns HTTP `201 Created` with these fields:

- `id`
- `endpoint_id`
- `event_type`
- `payload`
- `created_at`

The application generates `id`. PostgreSQL assigns the timezone-aware `created_at` value and stores
`payload` as `JSONB`. The returned `event_type` has already been trimmed.

Example response:

```json
{
  "id": "c2f0c529-b738-4e50-bc23-415ba3d0cf18",
  "endpoint_id": "5dce6a1d-f4c7-4c16-b709-2b0d08683ed2",
  "event_type": "order.created",
  "payload": {
    "order": {
      "id": "ord_12345",
      "amount": 149.99,
      "paid": true
    },
    "items": [
      {
        "sku": "SKU-1",
        "quantity": 2
      }
    ],
    "note": null
  },
  "created_at": "2026-07-22T10:15:30Z"
}
```

## Error responses

HTTP 404 means that the request contained a valid UUID, but no webhook endpoint with that ID exists.
The response is exactly:

```json
{
  "detail": "Webhook endpoint not found"
}
```

HTTP 422 indicates request validation failure. It is returned for:

- a malformed `endpoint_id`;
- an empty or whitespace-only `event_type`;
- an `event_type` longer than 255 characters;
- a missing `endpoint_id`, `event_type`, or `payload` field;
- a top-level `payload` that is a list, scalar value, or `null`.

## Persistence behavior

The handler first checks that the referenced `WebhookEndpoint` exists. It then creates a
`WebhookEvent` and stores it through SQLAlchemy in PostgreSQL. The event references the endpoint
through `endpoint_id`, and its JSON object is stored in the `JSONB` payload column. An endpoint whose
`is_active` value is `false` can still accept an event.

Creating an event does not send it to `target_url`, create a delivery attempt, or activate delivery,
retry, or replay processing. See [Database and migrations](../database.md) for schema and persistence
details.

## Non-goals and current limitations

- General event listing through `GET /webhook-events` is not available. The only read operation
  nested under an event is the delivery attempt listing for one existing event.
- Event delivery is not implemented.
- Retry, idempotency, and replay are not implemented.
- No payload size limit is configured.
- Authentication is not implemented.

## Navigation

- [API documentation index](index.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Main documentation index](../index.md)
- [Database and migrations](../database.md)
- [Project README](../../README.md)
