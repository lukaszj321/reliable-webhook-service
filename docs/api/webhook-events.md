# Webhook Event API

This API stores webhook events for existing webhook endpoint configurations and atomically creates
one initial `pending` delivery job for each accepted event. It does not execute the webhook.

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

The associated delivery job is not part of the response. No public `job_id`, job status, or
`next_attempt_at` field is returned.

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
Neither an event nor a delivery job is created. The response is exactly:

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

Validation failures occur before persistence, so they create neither an event nor a delivery job.

## Persistence behavior

The persistence flow is:

1. `create_webhook_event_with_delivery_job` checks the referenced `WebhookEndpoint`;
2. it creates, adds, and flushes the `WebhookEvent`;
3. the flush provides `event.id` and the server-generated `event.created_at`;
4. it creates one `WebhookDeliveryJob` with `status=pending`;
5. it sets `job.event_id=event.id` and `job.next_attempt_at=event.created_at`;
6. it adds and flushes the job;
7. the route performs one commit after both flushes.

Both records use the same caller-owned transaction. Another session sees neither before the
commit, and both become visible together afterward. A rollback before commit removes both
uncommitted records. The two flushes are not separate commits.

An endpoint whose `is_active` value is `false` can still accept an event and receive a pending job.
Because `next_attempt_at` represents the same instant as `event.created_at`, the job is immediately
due. The API request does not invoke `claim_due_webhook_delivery_jobs`.

Creating the job does not send the payload to `target_url`, create a delivery attempt, invoke the
claim service, start the worker process or worker loop, invoke a one-shot worker iteration, perform
recovery, apply retry policy, or move the job to `processing`. See
[Database and migrations](../database.md#atomic-event-and-delivery-job-creation) for transaction
and persistence details.

## Non-goals and current limitations

- General event listing through `GET /webhook-events` is not available. The only read operation
  nested under an event is the delivery attempt listing for one existing event.
- `POST /webhook-events` stores the event and pending job but does not start delivery or invoke
  `POST /webhook-events/{event_id}/delivery-attempts`.
- One synchronous delivery can be started explicitly through
  `POST /webhook-events/{event_id}/delivery-attempts`.
- `POST /webhook-events` does not perform HTTP, claim its job, call
  `execute_webhook_delivery_job`, schedule a retry, or execute a retry automatically.
- Outside this endpoint, the explicitly invoked internal `execute_webhook_delivery_job` service
  accepts a previously committed `processing` job, performs one completed delivery attempt,
  applies the retry policy, and flushes the attempt plus a `succeeded`, retryable `pending` with
  `next_attempt_at`, or `dead_letter` job transition in a caller-owned completion transaction.
- A separately and explicitly started
  [long-running worker process](../delivery-execution.md#long-running-worker-process) polls for
  work. In each one-shot worker iteration it performs stale recovery before processing and can
  claim a due pending job, including a retry scheduled by an earlier iteration.
- The API request does not start or invoke that worker process, its worker loop, an iteration,
  recovery, or retry execution. FastAPI startup does not start it either; event creation and
  delivery execution are separate lifecycles, not synchronous delivery within the request.
- The API does not control the worker lifecycle and exposes no worker start, stop, or status
  endpoint. Polling, due retry execution, and stale recovery run only while an operator has
  explicitly started the separate worker process.
- Exactly-once delivery, idempotency, and replay are not implemented.
- No payload size limit is configured.
- Authentication is not implemented.

## Navigation

- [API documentation index](index.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Webhook delivery execution](../delivery-execution.md)
- [Main documentation index](../index.md)
- [Database and migrations](../database.md)
- [Project README](../../README.md)
