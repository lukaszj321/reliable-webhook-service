# Webhook Delivery Execution

Delivery execution has three related entry points. `execute_webhook_delivery` performs one HTTP
attempt and flushes one completed `WebhookDeliveryAttempt` in a caller-owned transaction. The
internal `execute_webhook_delivery_job` service accepts a previously committed `processing` job,
uses that execution service, applies one retry decision, and flushes the job transition in the same
caller-owned transaction. The public manual endpoint uses only `execute_webhook_delivery`, commits
the attempt, and does not update a delivery job.

## Contents

- [Current execution model](#current-execution-model)
- [Preparation and validation](#preparation-and-validation)
- [HTTP request behavior](#http-request-behavior)
- [Result classification](#result-classification)
- [Attempt persistence](#attempt-persistence)
- [Transaction ownership](#transaction-ownership)
- [Attempt numbering](#attempt-numbering)
- [Retry decision policy](#retry-decision-policy)
- [Delivery job completion](#delivery-job-completion)
- [Delivery job claiming](#delivery-job-claiming)
- [Error handling](#error-handling)
- [Invocation](#invocation)
- [Current limitations](#current-limitations)
- [Navigation](#navigation)

## Current execution model

Delivery execution is synchronous. `execute_webhook_delivery` performs at most one external HTTP
request and flushes one completed attempt. The public manual POST route calls it with a database
session, HTTP client, and timeout, then commits and refreshes the attempt without changing the
delivery job.

The framework-independent `execute_webhook_delivery_job` service is an internal completion path,
not an endpoint, background service, or worker. Its caller supplies a previously committed
`processing` job ID, session, HTTP client, timeout, retry settings, and clocks. The service calls
`execute_webhook_delivery` once, applies one retry decision, and flushes the job transition. The
attempt and transition share one caller-owned PostgreSQL transaction.

The completion service does not select or claim work. Creating an event stores an immediately due
`pending` job, and the separate claim service can change due jobs to `processing`, but no worker or
polling loop connects event creation, claim, completion, or scheduled retry execution.

The target can receive the request before the completion transaction is committed. PostgreSQL can
atomically commit or roll back the attempt and job transition, but it cannot roll back the external
HTTP request and does not provide exactly-once delivery.

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

### `execute_webhook_delivery`

`execute_webhook_delivery` receives the caller's session. After completing the HTTP request and
classifying its result, the execution service performs `add`, `flush`, and `return`. It does not
commit, roll back, refresh, close the session, or create another session.

### Manual route

The public manual route calls the execution service, performs exactly one `commit` after receiving
the attempt, refreshes that attempt after the commit, and returns the response. The route does not
perform its own rollback or update `WebhookDeliveryJob`.

A flush error propagates to the caller before commit. A commit error also propagates and prevents
refresh. A refresh error propagates after the commit has succeeded; no rollback can undo that
completed commit. Management of a failed or unfinished transaction remains with the caller and the
existing session dependency.

### `execute_webhook_delivery_job`

The internal completion service receives the caller's session, loads an existing `processing` job,
calls `execute_webhook_delivery`, obtains and applies a retry decision, flushes the job transition,
and returns the same job and attempt in `WebhookDeliveryJobExecutionResult`. It does not commit,
roll back, refresh, close the session, or create another session.

After both flushes, the caller session sees the completed attempt and updated job. Before caller
commit, an independent session does not see the attempt and still sees the previously committed
`processing` state, schedule, and update timestamp. Caller commit makes the attempt and job
transition visible together.

Caller rollback removes the uncommitted attempt and restores the job's previously committed
`processing` state. The endpoint and event were committed before completion and remain. The
external HTTP request may already have reached the target; PostgreSQL rollback cannot undo it.
Consequently, the transaction protects database consistency but does not guarantee exactly-once
delivery.

## Attempt numbering

The first attempt for an event has number 1. Each later attempt uses the maximum existing number
for that event plus 1; attempts for other events do not affect it. A database unique constraint
protects the pair of `event_id` and `attempt_number`.

Concurrent attempt-number allocation remains outside the current scope.

## Retry decision policy

The retry decision policy remains pure and separate from `execute_webhook_delivery`, which
continues to execute exactly one HTTP request. After an attempt is complete, the policy accepts its
`outcome`, `attempt_number`, an explicit timezone-aware `decision_at`, and the retry settings. It
does not perform HTTP, read the system time, write to PostgreSQL, or update
`WebhookDeliveryJob` itself.

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

`execute_webhook_delivery_job` invokes the policy once after a completed attempt and applies its
returned values to the `processing` job without recalculating backoff. The public manual endpoint
does not invoke the policy. No worker automatically invokes completion or executes a scheduled
retry.

## Delivery job completion

### Entry conditions

`execute_webhook_delivery_job` requires an existing, previously committed
`WebhookDeliveryJob` whose status is exactly `processing`. It does not claim jobs.

- A missing job raises `WebhookDeliveryJobNotFoundError` with
  `Webhook delivery job not found`.
- A job in any status other than `processing` raises
  `WebhookDeliveryJobNotProcessingError` with
  `Webhook delivery job is not processing`.

Both validations happen before HTTP, attempt creation, or a job transition.

### Execution flow

The internal service performs this sequence:

1. load `WebhookDeliveryJob` with `session.get`;
2. validate that its status is `processing`;
3. call `execute_webhook_delivery` exactly once;
4. let that service add and flush one completed attempt;
5. obtain the decision timestamp;
6. call `decide_webhook_retry` exactly once;
7. assign `job.status` from the decision;
8. assign `job.next_attempt_at` from the decision;
9. set `job.updated_at` to the decision instant normalized to UTC;
10. flush the job transition;
11. return `WebhookDeliveryJobExecutionResult`;
12. leave commit or rollback to the caller.

The result contains exactly the existing `job` and the new `attempt`.

### State transitions

| Attempt result | Retry condition | Job status | `next_attempt_at` |
|---|---|---|---|
| `succeeded` | Any valid attempt number | `succeeded` | `null` |
| `failed` | `attempt_number < max_attempts` | `pending` | Exact policy timestamp |
| `failed` | `attempt_number >= max_attempts` | `dead_letter` | `null` |

`max_attempts` includes the first attempt. Backoff comes only from the existing retry policy; the
completion service neither duplicates nor adjusts it. A `pending` schedule is the exact timestamp
returned by the policy, while `updated_at` is the decision timestamp normalized to UTC.

### Transaction behavior

Real PostgreSQL integration tests verify pre-commit invisibility, joint post-commit visibility, and
caller rollback of both the attempt and job transition. They cover committed `succeeded`,
retryable `pending`, and final `dead_letter` transitions. After rollback, the endpoint and event
remain and the job returns to its previously committed `processing` state. Every accepted
completion performs exactly one external HTTP request.

### Failure behavior

Delivery preparation errors propagate without wrapping. An inactive endpoint creates no attempt
and leaves the job unchanged. Attempt flush errors, retry policy validation errors, and job flush
errors also propagate without changing their type. The caller is responsible for rolling back a
failed transaction; the service does not catch broad database exceptions or reset the job
automatically.

## Delivery job claiming

`claim_due_webhook_delivery_jobs` is a synchronous internal application service separate from
completion. Claiming does not perform HTTP, and `execute_webhook_delivery_job` does not invoke the
claim service. The retry policy remains pure decision logic even though completion applies its
result to a job. No worker connects these components.

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

A future orchestrator would:

1. claim due jobs in a claim transaction;
2. commit that claim transaction;
3. release its row locks;
4. start a separate completion transaction for a selected `processing` job;
5. call `execute_webhook_delivery_job` to perform HTTP and prepare the attempt plus transition;
6. commit or roll back the completion transaction.

This sequence is not automatic. There is no worker loop, the claim service does not invoke
completion, and completion does not invoke claim. The claim transaction therefore does not retain
row locks during the external HTTP request.

## Error handling

### Manual route errors

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

### Internal completion errors

- A missing job raises `WebhookDeliveryJobNotFoundError`.
- A non-`processing` job raises `WebhookDeliveryJobNotProcessingError`.
- Delivery preparation errors propagate before an attempt or transition.
- Retry policy validation errors propagate after the completed attempt is flushed but before the
  job is changed.
- Attempt flush and job flush errors propagate to the caller.
- These internal application and database errors have no assigned HTTP status because completion
  is not a public endpoint.
- The completion service does not use broad exception handling or perform rollback; transaction
  recovery belongs to the caller.

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

Internal application code can invoke `execute_webhook_delivery_job` without a public route. The
caller supplies its `Session`, a `job_id`, HTTP client, timeout, retry settings, and clocks. The
service does not import `Settings`; timeout and retry configuration are explicit caller
arguments.

## Current limitations

- No worker or polling loop
- No automatic claim invocation
- No automatic completion invocation
- No automatic execution of a scheduled `pending` retry
- No stale `processing` recovery, lease, or heartbeat
- No exactly-once delivery
- A crash after external HTTP but before commit can cause a later resend
- No concurrent attempt-number allocation protection beyond the database unique constraint
- No idempotency
- No replay
- No request signing
- No custom headers
- No response body persistence
- No public delivery-job API

Retry scheduling exists when internal completion is invoked explicitly: a retryable failed attempt
changes its job from `processing` to `pending` with the policy's `next_attempt_at`. Nothing
automatically invokes or executes that scheduled retry.

## Navigation

- [Project README](../README.md)
- [Documentation index](index.md)
- [Database and migrations](database.md)
- [API documentation](api/index.md)
- [Webhook event API](api/webhook-events.md)
- [Webhook delivery attempt API](api/webhook-delivery-attempts.md)
