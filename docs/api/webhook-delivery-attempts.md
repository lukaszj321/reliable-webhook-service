# Webhook Delivery Attempt API

This endpoint reads completed delivery attempts stored for one `WebhookEvent`.

## Contents

- [Endpoint](#endpoint)
- [Path parameter](#path-parameter)
- [Successful response](#successful-response)
- [Ordering](#ordering)
- [Empty result](#empty-result)
- [Error responses](#error-responses)
- [Read-only behavior](#read-only-behavior)
- [Non-goals and current limitations](#non-goals-and-current-limitations)
- [Navigation](#navigation)

## Endpoint

- Method: `GET`
- Path: `/webhook-events/{event_id}/delivery-attempts`
- Success status: `200 OK`
- Content-Type: `application/json`

## Path parameter

`event_id`:

- must be a valid UUID;
- must reference an existing `WebhookEvent`;
- returns FastAPI HTTP 422 when its UUID format is invalid;
- returns HTTP 404 when the UUID is valid but the event does not exist.

## Successful response

The endpoint returns a JSON array. Each item contains exactly these fields:

- `id`
- `event_id`
- `attempt_number`
- `outcome`
- `target_url`
- `response_status_code`
- `error_message`
- `duration_ms`
- `attempted_at`

`id` and `event_id` are UUID values. `outcome` is either `succeeded` or `failed`.
`response_status_code` can be `null` when no HTTP response was received, and `error_message` can be
`null` when the attempt succeeded. `attempted_at` is timezone-aware. `target_url` is the exact
snapshot of the URL used for that attempt.

Example response:

```json
[
  {
    "id": "5c3cce16-5a8d-4e32-a31d-54fca8c9db1b",
    "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
    "attempt_number": 1,
    "outcome": "succeeded",
    "target_url": "https://example.com/webhooks/orders",
    "response_status_code": 200,
    "error_message": null,
    "duration_ms": 125,
    "attempted_at": "2026-07-24T09:00:00Z"
  },
  {
    "id": "5579bb49-1e78-463b-bcbe-30c369ad8c44",
    "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
    "attempt_number": 2,
    "outcome": "failed",
    "target_url": "https://example.com/webhooks/orders",
    "response_status_code": 503,
    "error_message": "Service unavailable",
    "duration_ms": 480,
    "attempted_at": "2026-07-24T09:01:00Z"
  },
  {
    "id": "decd6f3a-61d8-49ee-886c-009802d3c6f8",
    "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
    "attempt_number": 3,
    "outcome": "failed",
    "target_url": "https://example.com/webhooks/orders",
    "response_status_code": null,
    "error_message": "Connection timed out",
    "duration_ms": 3000,
    "attempted_at": "2026-07-24T09:02:00Z"
  }
]
```

## Ordering

Results are ordered by:

1. `attempt_number` ascending;
2. `attempted_at` ascending;
3. `id` ascending.

The additional timestamp and UUID sort keys provide deterministic ordering when earlier values are
equal. The endpoint does not support user-selected sorting.

## Empty result

An existing event with no stored delivery attempts returns HTTP 200 with:

```json
[]
```

## Error responses

A valid UUID that does not identify an existing event returns HTTP 404 with exactly:

```json
{
  "detail": "Webhook event not found"
}
```

An invalid UUID format returns FastAPI HTTP 422. The validation response uses FastAPI's standard
validation payload.

## Read-only behavior

Calling this endpoint:

- does not create delivery attempts;
- does not update delivery attempts;
- does not modify the event;
- does not modify the webhook endpoint;
- does not commit database changes;
- only reads existing data.

## Non-goals and current limitations

- Delivery attempts are not created automatically.
- HTTP delivery is not implemented.
- Background processing is not implemented.
- Retry and backoff are not implemented.
- Replay is not implemented.
- Pagination is not implemented.
- Filtering by outcome or response status is not implemented.
- A top-level `GET /webhook-delivery-attempts` endpoint does not exist.
- Authentication is not implemented.

## Navigation

- [API documentation index](index.md)
- [Webhook endpoint API](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
- [Main documentation index](../index.md)
- [Database and migrations](../database.md)
- [Project README](../../README.md)
