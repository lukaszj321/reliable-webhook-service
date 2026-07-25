# Webhook Delivery Attempt API

This API manually executes one webhook delivery and lists completed delivery attempts stored for a
`WebhookEvent`.

## Contents

- [Manual delivery endpoint](#manual-delivery-endpoint)
- [Request behavior](#request-behavior)
- [Attempt numbering](#attempt-numbering)
- [Manual delivery response](#manual-delivery-response)
- [Delivery outcomes](#delivery-outcomes)
- [Manual delivery errors](#manual-delivery-errors)
- [Listing endpoint](#listing-endpoint)
- [Listing response](#listing-response)
- [Ordering](#ordering)
- [Empty listing](#empty-listing)
- [Listing errors](#listing-errors)
- [Current limitations](#current-limitations)
- [Navigation](#navigation)

## Manual delivery endpoint

- Method: `POST`
- Path: `/webhook-events/{event_id}/delivery-attempts`
- Success status: `201 Created`
- Response Content-Type: `application/json`
- Request body: none

`event_id` must be a valid UUID identifying an existing `WebhookEvent`.

PowerShell example:

```powershell
$eventId = "<existing-webhook-event-uuid>"
Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/webhook-events/$eventId/delivery-attempts"
```

Replace the placeholder with the UUID of an existing event. The example does not guarantee that an
arbitrary UUID exists.

## Request behavior

The endpoint synchronously performs exactly one outgoing JSON `POST`:

- the JSON payload comes from the stored `WebhookEvent`;
- the target URL comes from its associated `WebhookEndpoint`;
- the timeout comes from the `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` application setting;
- redirects are disabled;
- retries are not performed.

The endpoint accepts no request body and no timeout query parameter. Clients cannot override the
configured timeout through the request.

Creating an event through `POST /webhook-events` only stores the event. It does not invoke manual
delivery automatically.

## Attempt numbering

The first attempt for an event has `attempt_number` 1. Each later manual execution uses the maximum
existing number for the same event plus 1. Attempts for other events do not affect this sequence.
Concurrent number allocation is not handled by the current implementation.

## Manual delivery response

The response contains exactly these fields:

- `id`
- `event_id`
- `attempt_number`
- `outcome`
- `target_url`
- `response_status_code`
- `error_message`
- `duration_ms`
- `attempted_at`

Example succeeded attempt:

```json
{
  "id": "5c3cce16-5a8d-4e32-a31d-54fca8c9db1b",
  "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
  "attempt_number": 1,
  "outcome": "succeeded",
  "target_url": "https://example.com/webhooks/orders",
  "response_status_code": 204,
  "error_message": null,
  "duration_ms": 125,
  "attempted_at": "2026-07-24T09:00:00Z"
}
```

Example failed attempt after an HTTP 503 response:

```json
{
  "id": "5579bb49-1e78-463b-bcbe-30c369ad8c44",
  "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
  "attempt_number": 2,
  "outcome": "failed",
  "target_url": "https://example.com/webhooks/orders",
  "response_status_code": 503,
  "error_message": "HTTP response returned status 503",
  "duration_ms": 480,
  "attempted_at": "2026-07-24T09:01:00Z"
}
```

Both responses use HTTP 201 because a completed attempt was persisted successfully.

## Delivery outcomes

| Result | `outcome` | `response_status_code` | `error_message` |
|---|---|---|---|
| HTTP 200-299 | `succeeded` | Actual response status | `null` |
| Other HTTP status | `failed` | Actual response status | `HTTP response returned status {status_code}` |
| Timeout | `failed` | `null` | `Webhook request timed out` |
| Other transport error | `failed` | `null` | `Webhook request failed: {ExceptionClassName}` |

An expected delivery failure is not an API failure: the endpoint returns HTTP 201 after persisting
the failed attempt. A non-2xx response body is neither returned nor stored. Timeout and transport
errors do not have an HTTP response status. Exception details and tracebacks are not exposed.

## Manual delivery errors

Preparation errors happen before the outgoing request and do not create a delivery attempt:

| HTTP status | Detail | Cause |
|---|---|---|
| 404 | `Webhook event not found` | The event UUID does not identify an event |
| 409 | `Webhook endpoint not found` | The stored event references no available endpoint |
| 409 | `Webhook endpoint is inactive` | The associated endpoint is inactive |
| 422 | Standard FastAPI validation response | `event_id` is not a valid UUID |

The missing-endpoint response is part of the endpoint contract, although normal public event
creation requires an existing endpoint.

## Listing endpoint

- Method: `GET`
- Path: `/webhook-events/{event_id}/delivery-attempts`
- Success status: `200 OK`
- Content-Type: `application/json`

The GET endpoint is read-only. It does not execute a request, create an attempt, or modify database
records.

## Listing response

The endpoint returns a JSON array. Each item contains the same nine fields as the manual delivery
response:

- `id`
- `event_id`
- `attempt_number`
- `outcome`
- `target_url`
- `response_status_code`
- `error_message`
- `duration_ms`
- `attempted_at`

`id` and `event_id` are UUID values. `outcome` is `succeeded` or `failed`.
`response_status_code` can be `null` when no HTTP response was received, and `error_message` can be
`null` when the attempt succeeded. `attempted_at` is timezone-aware, and `target_url` is the URL
snapshot used for that attempt.

Example:

```json
[
  {
    "id": "5c3cce16-5a8d-4e32-a31d-54fca8c9db1b",
    "event_id": "764b61fb-6508-4464-a05d-6621712d03e9",
    "attempt_number": 1,
    "outcome": "succeeded",
    "target_url": "https://example.com/webhooks/orders",
    "response_status_code": 204,
    "error_message": null,
    "duration_ms": 125,
    "attempted_at": "2026-07-24T09:00:00Z"
  }
]
```

## Ordering

Results are ordered by:

1. `attempt_number` ascending;
2. `attempted_at` ascending;
3. `id` ascending.

The timestamp and UUID sort keys make ordering deterministic when earlier values are equal.
User-selected sorting is not supported.

## Empty listing

An existing event with no stored delivery attempts returns HTTP 200 with:

```json
[]
```

## Listing errors

A valid UUID that does not identify an existing event returns HTTP 404:

```json
{
  "detail": "Webhook event not found"
}
```

An invalid UUID returns FastAPI HTTP 422 with its standard validation payload.

## Current limitations

- Delivery is not triggered automatically after event creation.
- The POST endpoint is manual execution, not replay.
- Retry and backoff are not implemented.
- Replay is not implemented.
- Background processing is not implemented.
- Pagination and filtering are not implemented for GET.
- Authentication is not implemented.
- A top-level `/webhook-delivery-attempts` endpoint does not exist.

## Navigation

- [API documentation index](index.md)
- [Webhook endpoint API](webhook-endpoints.md)
- [Webhook event API](webhook-events.md)
- [Webhook delivery execution](../delivery-execution.md)
- [Main documentation index](../index.md)
- [Database and migrations](../database.md)
- [Project README](../../README.md)
