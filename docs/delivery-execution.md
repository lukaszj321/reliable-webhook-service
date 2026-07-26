# Webhook Delivery Execution

The application provides two connected execution levels: the synchronous
`execute_webhook_delivery` application service and a public manual HTTP endpoint that invokes the
service and returns one persisted `WebhookDeliveryAttempt`.

## Contents

- [Current execution model](#current-execution-model)
- [Preparation and validation](#preparation-and-validation)
- [HTTP request behavior](#http-request-behavior)
- [Result classification](#result-classification)
- [Attempt persistence](#attempt-persistence)
- [Transaction ownership](#transaction-ownership)
- [Attempt numbering](#attempt-numbering)
- [Retry decision policy](#retry-decision-policy)
- [Delivery job claiming](#delivery-job-claiming)
- [Error handling](#error-handling)
- [Invocation](#invocation)
- [Current limitations](#current-limitations)
- [Navigation](#navigation)

## Current execution model

Delivery execution is synchronous. One service call performs at most one HTTP request, and every
request that is actually executed ends with an attempt to persist one completed delivery attempt.
The public manual POST route calls that service with a database session, HTTP client, and configured
timeout. The execution service adds and flushes the completed attempt in the caller-owned
transaction, and the manual route commits it. The service does not retry requests, and creating a
webhook event does not trigger delivery automatically. Event creation atomically persists one
`WebhookEvent` and one immediately due `pending` `WebhookDeliveryJob`, but it does not call
`execute_webhook_delivery`, perform HTTP, or create a `WebhookDeliveryAttempt`.

The external endpoint can receive the request before the attempt is committed, so HTTP execution
and the database commit are not one atomic operation and this design does not provide exactly-once
delivery.

## Preparation and validation

Before making a request, the service:

1. reads the `WebhookEvent`;
2. reads the associated `WebhookEndpoint`;
3. checks that the endpoint is active;
4. reads the maximum existing `attempt_number` for the event;
5. prepares the event ID, target URL, payload, and next attempt number.

Preparation can raise these application errors:

- `Webhook event not found`
- `Webhook endpoint not found`
- `Webhook endpoint is inactive`

These errors occur before the HTTP request and do not create a delivery attempt.

## HTTP request behavior

The service sends the event payload as JSON in a `POST` request to the endpoint's exact
`target_url`. The caller supplies an explicit timeout, which must be positive and finite. Redirects
are disabled with `follow_redirects=False`.

Each execution performs exactly one request. It does not retry, and it does not read or persist the
response body.

## Result classification

| Result | `outcome` | `response_status_code` | `error_message` |
|---|---|---|---|
| HTTP 200-299 | `succeeded` | Actual response status | `null` |
| Other HTTP status | `failed` | Actual response status | `HTTP response returned status {status_code}` |
| Timeout | `failed` | `null` | `Webhook request timed out` |
| Other `RequestError` | `failed` | `null` | `Webhook request failed: {ExceptionClassName}` |

Exception text, response bodies, and tracebacks are not persisted.

## Attempt persistence

The service persists these fields:

- `event_id`
- `attempt_number`
- `outcome`
- `target_url`
- `response_status_code`
- `error_message`
- `duration_ms`
- `attempted_at`

`target_url` is a snapshot of the URL used for the request. `duration_ms` uses a monotonic
measurement and cannot be negative. `attempted_at` must be timezone-aware.

Persistence within the execution service follows this sequence:

1. create one completed `WebhookDeliveryAttempt`;
2. call `session.add(attempt)`;
3. call `session.flush()`;
4. make the generated ID available and check database constraints that can be enforced during the
   flush;
5. return the ORM object;
6. leave the caller-owned transaction open.

The uncommitted attempt is available in the caller's transaction but is not visible to an
independent PostgreSQL session. A caller rollback removes the attempt, while a caller commit makes
it visible to other sessions. An event and endpoint committed before delivery remain after a
rollback of the attempt transaction. A flush does not guarantee a commit.

## Transaction ownership

`execute_webhook_delivery` receives the caller's session. After completing the HTTP request and
classifying its result, the execution service performs `add`, `flush`, and `return`. It does not
commit, roll back, refresh, close the session, or create another session.

The public manual route calls the execution service, performs exactly one `commit` after receiving
the attempt, refreshes that attempt after the commit, and returns the response. The route does not
perform its own rollback.

A flush error propagates to the caller before commit. A commit error also propagates and prevents
refresh. A refresh error propagates after the commit has succeeded; no rollback can undo that
completed commit. Management of a failed or unfinished transaction remains with the caller and the
existing session dependency.

This boundary allows a future worker to prepare a completed delivery attempt and a delivery job
transition in one caller-owned transaction after HTTP execution. That attempt-plus-job completion
does not exist yet: this issue does not update `WebhookDeliveryJob` or connect delivery execution
to the delivery job lifecycle.

## Attempt numbering

The first attempt for an event has number 1. Each later attempt uses the maximum existing number
for that event plus 1; attempts for other events do not affect it. A database unique constraint
protects the pair of `event_id` and `attempt_number`.

Concurrent attempt-number allocation remains outside the current scope.

## Retry decision policy

The retry decision policy is separate from `execute_webhook_delivery`, which continues to execute
exactly one HTTP request. After an attempt is complete, the pure policy accepts its `outcome`,
`attempt_number`, an explicit timezone-aware `decision_at`, and the retry settings. It does not
perform HTTP, read the system time, write to PostgreSQL, or update `WebhookDeliveryJob`.

The policy returns an immutable `RetryDecision` containing `status` and `next_attempt_at`:

| Outcome / attempt state | Decision status | `next_attempt_at` |
|---|---|---|
| `succeeded` | `succeeded` | `null` |
| `failed` and `attempt_number < max_attempts` | `pending` | `decision_at` normalized to UTC plus the retry delay |
| `failed` and `attempt_number >= max_attempts` | `dead_letter` | `null` |

`max_attempts` is the total allowed number of attempts, including the first. Treating attempt
numbers above the limit as `dead_letter` also handles configurations lowered after earlier attempts
were recorded. `processing` remains a possible `WebhookDeliveryJob` state, but it is not a retry
decision status.

The exponential-backoff delay is:

```text
delay = min(
    base_delay_seconds * 2 ** (attempt_number - 1),
    max_delay_seconds
)
```

With the defaults, attempts 1 through 4 produce delays of 5, 10, 20, and 40 seconds. Later values
grow up to the 300-second cap, and very large attempt numbers are safely capped. There is no jitter,
randomness, sleep, or implicit current-time lookup. Passing `decision_at` explicitly makes the
result deterministic, and a pending `next_attempt_at` is normalized to UTC.

## Delivery job claiming

`claim_due_webhook_delivery_jobs` is a synchronous internal application service separate from
`execute_webhook_delivery`. Claiming does not perform HTTP, while `execute_webhook_delivery`
continues to perform exactly one manual request. The retry policy remains pure decision logic.
These components are not connected by a worker.

The normal `POST /webhook-events` path supplies the initial `pending` jobs. Their
`next_attempt_at` represents the same instant as `event.created_at`, so they are immediately due
for this claim service. Nothing invokes the claim service automatically.

The claim flow is:

1. the caller passes a SQLAlchemy `Session`, a timezone-aware `claimed_at`, and a positive integer
   batch `limit`;
2. the service selects due `pending` jobs in deterministic `next_attempt_at`, `created_at`, and
   `id` order;
3. PostgreSQL locks the selected rows through `FOR UPDATE SKIP LOCKED`;
4. the service changes each selected job to `processing` and explicitly sets `updated_at` to the
   normalized UTC claim time;
5. the service flushes the changes;
6. the caller commits or rolls back the caller-owned transaction.

The service does not create or close a session, read the system time, commit, or roll back. The
row-level locks remain active until the caller ends the transaction. A commit persists the claim;
a rollback restores the previous committed `pending` state.

With concurrent claimers, session A can lock the first due job while session B skips it and claims
later unlocked due jobs. Real PostgreSQL integration tests verify that the two returned ID sets do
not overlap. This is internal infrastructure, not a public endpoint.

A future worker should commit a claim before invoking `execute_webhook_delivery`. It should not
hold the claim transaction or row-level locks while waiting for an external HTTP request. No worker
currently performs this sequence. Claiming alone does not execute HTTP, create an attempt, or
perform a completion transition.

## Error handling

- Preparation errors occur before the request and do not create an attempt.
- Expected delivery failures, including non-2xx responses and transport errors, create a completed
  `failed` attempt.
- An invalid timeout is not caught and does not create an attempt.
- An invalid naive attempt timestamp prevents the request and does not create an attempt.
- A flush error propagates from the execution service before commit.
- A route commit error propagates, and refresh is not performed.
- A route refresh error propagates after a successful commit.
- The execution service and manual route do not translate database errors into the documented
  domain `HTTPException` responses.
- The manual route does not perform rollback.

## Invocation

Application code can call `execute_webhook_delivery` directly. The public manual API invokes the
same service through:

```text
POST /webhook-events/{event_id}/delivery-attempts
```

The route accepts a UUID path parameter and no request body. It synchronously performs exactly one
outgoing request through the execution service, using the timeout from `Settings`. The execution
service adds and flushes the returned attempt; the manual route commits it, refreshes it, and
returns HTTP 201. Both `succeeded` and expected `failed` delivery outcomes return HTTP 201 because
the completed attempt was committed. Non-2xx responses, timeouts, and other transport errors are
delivery results rather than API errors.

Preparation errors occur before an outgoing request and do not create an attempt:

| Application error | HTTP status | API detail |
|---|---|---|
| `WebhookEventNotFoundError` | 404 | `Webhook event not found` |
| `WebhookEndpointNotFoundError` | 409 | `Webhook endpoint not found` |
| `InactiveWebhookEndpointError` | 409 | `Webhook endpoint is inactive` |

`POST /webhook-events` atomically stores an event and its initial `pending` delivery job. It does
not call the delivery service or invoke the manual delivery endpoint automatically.

## Current limitations

- No automatic delivery trigger
- The delivery job claiming service exists, but no worker or polling loop invokes it
- No worker consumes newly created pending jobs
- No automatic claim invocation
- No automatic HTTP execution for newly created jobs
- Claiming does not execute HTTP or perform a completion transition
- Retry policy and backoff calculation exist, but they are not connected to claiming and no
  automatic retry execution invokes them
- No attempt-plus-job completion transaction
- No delivery job success transition after a succeeded attempt
- No retry rescheduling or dead-letter transition performed by a worker
- No automatic retry policy invocation
- No exactly-once delivery; caller-owned transaction alone cannot prevent resending after a crash
  between the external HTTP request and the database commit
- No stale `processing` recovery, lease, or heartbeat
- No background worker
- No replay
- No idempotency
- No concurrent attempt-number allocation
- No request signing
- No custom headers
- No response body persistence

## Navigation

- [Project README](../README.md)
- [Documentation index](index.md)
- [Database and migrations](database.md)
- [API documentation](api/index.md)
- [Webhook event API](api/webhook-events.md)
- [Webhook delivery attempt API](api/webhook-delivery-attempts.md)
