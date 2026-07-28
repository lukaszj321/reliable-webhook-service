# Webhook Event API

This API stores webhook events for existing webhook endpoint configurations and atomically creates
one initial `pending` delivery job for each new event. Optional endpoint-scoped idempotency can
reuse an equivalent existing event without creating another job. The endpoint does not execute
the webhook.

## Contents

- [Endpoint](#endpoint)
- [Optional idempotency header](#optional-idempotency-header)
- [Request body](#request-body)
- [New event response](#new-event-response)
- [Equivalent request response](#equivalent-request-response)
- [Conflict response](#conflict-response)
- [Validation and error responses](#validation-and-error-responses)
- [Persistence behavior](#persistence-behavior)
- [Manual replay](#manual-replay)
- [Non-goals and current limitations](#non-goals-and-current-limitations)
- [Navigation](#navigation)

## Endpoint

- Method: `POST`
- Path: `/webhook-events`
- Success statuses: `201 Created` for a new event; `200 OK` for equivalent keyed reuse
- Content-Type: `application/json`

## Optional idempotency header

`Idempotency-Key` is an optional HTTP header. It is not part of the JSON body and is not an
authentication token.

Rules:

- uniqueness is scoped to `(endpoint_id, idempotency_key)`;
- leading and trailing whitespace is removed;
- the normalized value must not be empty and must not exceed 255 characters;
- the value is case-sensitive, and internal whitespace is preserved;
- the key is not returned in the public response;
- the same non-null key can be used for different endpoints;
- keys do not expire, and there is no TTL, deletion API, or automatic cleanup;
- keys are not documented as encrypted or hashed.

Without the header, every valid request creates a new event and pending job and returns HTTP 201.
The same body can therefore create multiple unkeyed events.

Example keyed request:

```http
POST /webhook-events HTTP/1.1
Host: 127.0.0.1:8000
Content-Type: application/json
Idempotency-Key: order-created-ord-12345

{
  "endpoint_id": "5dce6a1d-f4c7-4c16-b709-2b0d08683ed2",
  "event_type": "order.created",
  "payload": {
    "order_id": "ord-12345"
  }
}
```

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

## New event response

An unkeyed request or the first use of a scoped key returns HTTP `201 Created` with these fields:

- `id`
- `endpoint_id`
- `event_type`
- `payload`
- `created_at`

The application generates `id`. PostgreSQL assigns the timezone-aware `created_at` value and stores
`payload` as `JSONB`. The returned `event_type` has already been trimmed.

The associated delivery job is not part of the response. No `idempotency_key`, `created`, `job`,
`job_id`, job status, `next_attempt_at`, attempt, savepoint, or constraint field is returned.

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

## Equivalent request response

An equivalent retry with the same endpoint and normalized key returns HTTP `200 OK`. Equivalence
requires:

- the same endpoint;
- the same normalized idempotency key;
- the same normalized event type;
- an equivalent PostgreSQL `JSONB` payload value.

JSON object key order does not affect equivalence. JSON Boolean and number values remain distinct
for this contract, so `true` is not equivalent to `1`.

The body has exactly the same five fields as the HTTP 201 response and identifies the same event,
including the same `id` and `created_at`. No second event or job is created, and the existing event
and job are not updated. The body has no `created` field; clients distinguish creation from reuse
through HTTP 201 or HTTP 200.

## Conflict response

Reusing the same scoped key with a different normalized event type or a non-equivalent payload
returns HTTP `409 Conflict`:

```json
{
  "detail": "Idempotency key conflicts with an existing webhook event"
}
```

The response does not expose the key, old or new payload, event type, or existing event ID. The
existing event and job remain unchanged.

## Validation and error responses

HTTP 404 means that the request contained a valid UUID, but no webhook endpoint with that ID exists.
Neither an event nor a delivery job is created. The response is exactly:

```json
{
  "detail": "Webhook endpoint not found"
}
```

HTTP 422 indicates request validation failure. It is returned for:

- an empty or whitespace-only `Idempotency-Key`, with detail
  `Idempotency key must not be empty`;
- an `Idempotency-Key` longer than 255 characters after normalization, with detail
  `Idempotency key must not exceed 255 characters`;
- a malformed `endpoint_id`;
- an empty or whitespace-only `event_type`;
- an `event_type` longer than 255 characters;
- a missing `endpoint_id`, `event_type`, or `payload` field;
- a top-level `payload` that is a list, scalar value, or `null`.

Validation failures occur before persistence, so they create neither an event nor a delivery job.
A valid key with a missing endpoint returns HTTP 404. An invalid key with a missing endpoint
returns HTTP 422 because key validation occurs before endpoint lookup.

## Persistence behavior

The route uses `create_idempotent_webhook_event_with_delivery_job`. For an unkeyed request the
service creates a new event and job directly. For a keyed request it first looks up the scoped key
and classifies an existing record as equivalent reuse or conflict.

A new keyed insert runs inside a nested transaction/savepoint. The PostgreSQL unique constraint on
`(endpoint_id, idempotency_key)` is the final race safeguard. If another transaction wins the
insert race, only the savepoint is rolled back: an equivalent loser reads and returns the existing
event, while a conflicting loser receives the same domain conflict. Unrelated database errors are
not translated into idempotency conflicts.

For a new event, the service creates and flushes `WebhookEvent`, then creates and flushes one
`WebhookDeliveryJob` with `status=pending`, `event_id=event.id`, and
`next_attempt_at=event.created_at`. Both records remain in the caller-owned outer transaction.
The route performs one outer commit, so another session sees neither before commit and both
afterward. Duplicate reuse creates no records but still leaves outer transaction completion to the
caller.

An endpoint whose `is_active` value is `false` can still accept an event and receive a pending job.
Because `next_attempt_at` represents the same instant as `event.created_at`, the job is immediately
due. The API request does not invoke `claim_due_webhook_delivery_jobs`.

Creating the job does not send the payload to `target_url`, create a delivery attempt, invoke the
claim service, start the worker process or worker loop, invoke a one-shot worker iteration, perform
recovery, apply retry policy, or move the job to `processing`. See
[Database and migrations](../database.md#atomic-event-and-delivery-job-creation) for transaction
and persistence details.

## Manual replay

```text
POST /webhook-events/{event_id}/replay
```

The replay request has no body and does not accept `Idempotency-Key`. That header applies only to
event ingestion; replay intentionally schedules another delivery cycle for an existing event.

```powershell
curl.exe -X POST http://127.0.0.1:8000/webhook-events/00000000-0000-0000-0000-000000000001/replay
```

Only jobs in `succeeded` or `dead_letter` are eligible. A successful request returns HTTP 202:

```json
{
  "event_id": "00000000-0000-0000-0000-000000000001",
  "delivery_job_id": "00000000-0000-0000-0000-000000000002",
  "status": "pending",
  "next_attempt_at": "2026-07-30T12:00:00Z"
}
```

HTTP 202 means the existing job is due for asynchronous worker processing. The request performs no
downstream HTTP, creates no attempt, and creates neither a new event nor a new job. It resets
`WebhookDeliveryJob.attempt_count` to `0`; the global
`WebhookDeliveryAttempt.attempt_number` history is preserved.

Errors are:

| Condition | HTTP status |
|---|---:|
| Event does not exist | 404 |
| Endpoint does not exist or is inactive | 409 |
| Delivery job does not exist | 409 |
| Job is already `pending` or `processing` | 409 |

Infrastructure errors are not mapped to 409. The service locks the existing job through
`SELECT ... FOR UPDATE`, flushes the transition, and leaves commit to the API route. If two replay
requests race, the second waits for the row lock, sees `pending` after the first commit, and
receives deterministic HTTP 409.

Replay differs from `POST /webhook-events/{event_id}/delivery-attempts`: that endpoint performs one
synchronous HTTP request, stores one global attempt, returns HTTP 201, and does not update
`attempt_count` or schedule worker work.

Replay does not guarantee exactly-once delivery. A previous request may have reached downstream
before a local timeout or failure, so replay can duplicate remote side effects. Downstream systems
should use their own idempotency or deduplication where needed.

Authentication and authorization are not implemented. Production deployments should normally
restrict replay to authorized operators. The replay response contains no payload, idempotency key,
stored response body, or authorization data; replay paths should not log payloads.

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
- Event-ingestion idempotency is implemented only for `POST /webhook-events`. Downstream delivery
  idempotency is not implemented: the service does not forward `Idempotency-Key` to the target and
  cannot guarantee target-side deduplication.
- Exactly-once delivery is not implemented. HTTP may reach the target before a completion
  transaction fails, allowing recovery and later processing to deliver again.
- Idempotency keys have no expiration, deletion endpoint, or automatic cleanup.
- No payload size limit is configured.
- Authentication is not implemented.

## Navigation

- [API documentation index](index.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Webhook delivery execution](../delivery-execution.md)
- [Main documentation index](../index.md)
- [Database and migrations](../database.md)
- [Project README](../../README.md)
