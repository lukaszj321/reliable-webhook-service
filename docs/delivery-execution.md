# Webhook Delivery Execution

Delivery execution and recovery have five related entry points. `execute_webhook_delivery` performs one HTTP
attempt and flushes one completed `WebhookDeliveryAttempt` in a caller-owned transaction. The
internal `execute_webhook_delivery_job` service accepts a previously committed `processing` job,
uses that execution service, applies one retry decision, and flushes the job transition in the same
caller-owned transaction. `run_webhook_delivery_processing_cycle` explicitly connects claim and
completion for one bounded batch. `recover_stale_webhook_delivery_jobs` explicitly returns one
bounded batch of stale `processing` jobs to `pending` without HTTP or attempt creation. The public
manual endpoint uses only `execute_webhook_delivery`, commits the attempt, and does not update a
delivery job.

## Contents

- [Current execution model](#current-execution-model)
- [Bounded delivery processing cycle](#bounded-delivery-processing-cycle)
- [Stale processing job recovery](#stale-processing-job-recovery)
- [Preparation and validation](#preparation-and-validation)
- [HTTP request behavior](#http-request-behavior)
- [Result classification](#result-classification)
- [Attempt persistence](#attempt-persistence)
- [Delivery transaction ownership](#delivery-transaction-ownership)
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
`pending` job, and the separate claim service can change due jobs to `processing`. When explicitly
invoked, the bounded processing cycle connects claim and completion for at most the requested
limit. Event creation and the public API do not invoke that cycle, and no worker or polling loop
runs it continuously or executes scheduled retries automatically.

The target can receive the request before the completion transaction is committed. PostgreSQL can
atomically commit or roll back the attempt and job transition, but it cannot roll back the external
HTTP request and does not provide exactly-once delivery.

## Bounded delivery processing cycle

### Purpose

`run_webhook_delivery_processing_cycle` is a synchronous, framework-independent orchestration
service. One explicit internal invocation connects one call to
`claim_due_webhook_delivery_jobs` with zero or more ordered calls to
`execute_webhook_delivery_job`. One invocation performs one bounded batch and then returns; it is
not a long-running worker or polling loop.

The cycle does not import FastAPI or `Settings`, does not start automatically, and does not sleep
or invoke another cycle. Application code must provide all dependencies and call it explicitly.

### Inputs

The caller supplies:

- a session factory used to create the dedicated claim session and fresh completion sessions;
- a `WebhookHttpClient`;
- a timezone-aware `claimed_at`;
- a batch `limit`;
- the HTTP timeout;
- maximum attempts and base and maximum retry delays;
- explicit attempt, decision, and monotonic clocks when deterministic timing is required.

### Validation

Before opening the first session, the cycle validates:

- `limit` is an integer greater than or equal to 1 and is not a Boolean;
- `claimed_at` is timezone-aware;
- the timeout is finite and greater than zero.

Invalid input therefore creates no claim or completion session and performs no HTTP.

### Claim phase

The cycle:

1. creates one dedicated claim session;
2. calls `claim_due_webhook_delivery_jobs` exactly once with `claimed_at` and `limit`;
3. receives jobs in deterministic `next_attempt_at`, `created_at`, and `id` order;
4. snapshots their UUIDs before ending the claim session;
5. commits the `pending` to `processing` changes;
6. rolls back instead if claim execution or claim commit fails;
7. always closes the claim session before completion and external HTTP begin.

The snapshot separates later processing from claim-session ORM objects. The cycle never returns
those objects.

### Empty cycle

When no due job is claimed, the cycle still commits and closes the claim transaction. It creates no
completion session, performs no HTTP, and returns an immutable result with empty
`claimed_job_ids` and `completed_jobs`.

### Completion phase

The cycle iterates over the snapshotted UUIDs in claim order. For each started job it:

1. creates a fresh completion session;
2. calls `execute_webhook_delivery_job` exactly once;
3. lets that service perform one attempt and apply one retry decision in the same caller-owned
   completion transaction;
4. snapshots the resulting job ID, attempt ID, status, and `next_attempt_at`;
5. commits the current attempt and job transition before starting another job;
6. appends the primitive snapshot only after the commit succeeds;
7. rolls back only the current completion transaction on execution or commit failure;
8. always closes the current session.

A failure is propagated immediately, so no later claimed UUID is started.

### Result

`WebhookDeliveryProcessingCycleResult` is an immutable snapshot containing:

- `claimed_job_ids`: every UUID committed as `processing` by the claim transaction, in claim order;
- `completed_jobs`: immutable primitive summaries for successfully committed completions, in the
  same order;
- `claimed_count`: the number of claimed UUIDs;
- `completed_count`: the number of committed completion summaries.

Each `WebhookDeliveryProcessingJobResult` contains `job_id`, `attempt_id`, `status`, and
`next_attempt_at`. Neither result contains a SQLAlchemy session or ORM object.

### Partial progress

Consider three jobs claimed together:

1. job A completes successfully and its completion transaction commits;
2. job B raises an error and its current completion transaction rolls back;
3. job C is not started because processing stops at B.

Job A remains completed because its commit preceded B. Jobs B and C remain `processing` because
the earlier claim transaction committed all three jobs before completion began. There is no shared
batch transaction or batch rollback, and the cycle does not automatically restore either job to
`pending`.

The explicit stale processing job recovery service can later restore eligible claimed but not
completed jobs to `pending`. The bounded cycle does not invoke recovery, and no automatic recovery
mechanism exists.

### Failure boundaries

| Failure | Transaction outcome | Session outcome | Further processing |
|---|---|---|---|
| Claim execution failure | Claim transaction rolls back | Claim session closes | No completion starts |
| Claim commit failure | Claim transaction rolls back | Claim session closes | No completion starts |
| Completion execution failure | Current completion rolls back | Current session closes | Later claimed jobs are not started |
| Completion commit failure | Current completion rolls back | Current session closes | Later claimed jobs are not started |

Previously committed per-job completions remain committed in every later completion-failure case.
The cycle performs no batch-level compensating rollback.

### HTTP limitation

External HTTP is not part of the PostgreSQL transaction. An HTTP request can reach the target
before the current completion transaction is rolled back, and PostgreSQL rollback cannot undo that
external side effect. The bounded cycle therefore does not guarantee exactly-once delivery.

### Still not implemented

- long-running worker loop;
- polling or sleep;
- scheduler or automatic application-startup invocation;
- continuous or parallel completion;
- automatic execution of a scheduled `pending` retry;
- automatic stale processing job recovery invocation;
- leases, lease owners, or heartbeat;
- exactly-once delivery;
- idempotency;
- replay.

## Stale processing job recovery

### Cycle purpose

A job can remain `processing` when its claim transaction commits but the process stops before its
per-job completion commits. A later completion failure can also leave later jobs from the same
claimed batch unstarted in `processing`.

`recover_stale_webhook_delivery_jobs` is a synchronous, framework-independent service for one
explicit bounded recovery batch. It does not run automatically and is not a worker, polling loop,
scheduler, background task, or application startup hook.

### Cycle inputs

The caller supplies:

- a caller-owned SQLAlchemy `Session`;
- `stale_before`, the recovery cutoff;
- `recovered_at`, the timestamp assigned to recovered state;
- `limit`, the maximum number of jobs in this bounded recovery batch.

### Cycle validation

Before executing SQL, the service requires:

- a positive integer `limit` that is not a Boolean;
- a timezone-aware `stale_before`;
- a timezone-aware `recovered_at`;
- `recovered_at >= stale_before` after both timestamps are normalized to UTC.

Invalid input raises `ValueError` before a query or flush.

### Eligibility

A job is eligible only when:

- `status` is `processing`;
- `updated_at <= stale_before`.

The cutoff is inclusive. Fresh `processing` jobs and every `pending`, `succeeded`, or
`dead_letter` job are skipped. `updated_at` is currently the persisted age marker; there is no
lease timestamp or heartbeat.

### Selection and locking

One invocation executes one query in deterministic recovery order:

1. `updated_at`;
2. `created_at`;
3. `id`.

The caller's `limit` bounds the result. PostgreSQL uses `SELECT FOR UPDATE SKIP LOCKED`, so
concurrent recovery sessions can select disjoint batches. A row locked by another transaction is
skipped without waiting for that row; locks acquired by recovery remain active until its
caller-owned transaction ends.

### State transition

For every selected job the service:

1. changes `processing` to `pending`;
2. sets `next_attempt_at = recovered_at`;
3. sets `updated_at = recovered_at`;
4. performs one flush after all mutations.

Both assigned timestamps use normalized UTC. Recovery creates no `WebhookDeliveryAttempt`,
performs no HTTP, and does not invoke the retry policy or processing cycle.

### Transaction ownership

The entire bounded recovery batch is one caller-owned transaction. The service does not commit,
roll back, or close the session:

- caller commit persists every transition in the selected batch;
- caller rollback restores the previously committed `processing` state;
- row locks remain active until caller commit or rollback;
- selection and flush failures propagate to the caller, which owns rollback and close.

### Empty recovery

When no job is eligible, the service returns an empty immutable result. It performs no flush,
mutation, commit, rollback, or close.

### Cycle result

`WebhookDeliveryJobRecoveryResult` contains:

- `recovered_job_ids`, an ordered `tuple[UUID, ...]`;
- `recovered_count`, a read-only property derived from that tuple.

The result is an immutable UUID snapshot and contains no ORM object or session.

### Duplicate-delivery limitation

A concrete failure sequence is:

1. a job is claimed as `processing`;
2. its HTTP request reaches the endpoint;
3. the process stops before the completion transaction commits;
4. the job remains `processing` without a committed completion;
5. explicit recovery returns it to `pending`;
6. a later, separately invoked processing cycle sends the webhook again.

PostgreSQL does not know whether the remote target received the earlier request, and rollback
cannot undo HTTP. Recovery performs no remote verification, creates no missing attempt, and
performs no compensating action. The project has no idempotency key and provides no exactly-once
guarantee.

### Cycle capabilities still not implemented

- automatic recovery invocation;
- worker loop, polling, or sleep;
- scheduler or application startup hook;
- leases, lease owners, or heartbeat;
- stale timeout configuration in `Settings`;
- remote delivery verification;
- automatic retry execution;
- idempotency;
- exactly-once delivery;
- replay.

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

## Delivery transaction ownership

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

`claim_due_webhook_delivery_jobs` is a synchronous internal application service with its own
transaction responsibility. Claiming does not perform HTTP, and
`execute_webhook_delivery_job` does not invoke the claim service. The explicitly invoked bounded
processing cycle connects these services while preserving the separate claim and per-job
completion transaction boundaries. The retry policy remains pure decision logic even though
completion applies its result to a job.

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

The existing bounded processing cycle:

1. claim due jobs in a claim transaction;
2. snapshot the claimed UUIDs in deterministic order;
3. commit that claim transaction;
4. close the claim session and release its row locks;
5. start a fresh completion transaction for each selected `processing` job;
6. call `execute_webhook_delivery_job` to perform HTTP and prepare the attempt plus transition;
7. commit or roll back the current completion transaction before another job starts.

This sequence is not automatic. There is no worker loop, the claim service does not invoke
completion, and completion does not invoke claim; the bounded cycle must be called explicitly. The
claim transaction therefore does not retain row locks during the external HTTP request.

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

Internal application code can also invoke `run_webhook_delivery_processing_cycle`. The caller
supplies a session factory, HTTP client, claim time, bounded limit, timeout, retry settings, and
clocks. One call performs one bounded cycle and returns; no public API route invokes it.

Internal application code can separately invoke `recover_stale_webhook_delivery_jobs` with a
caller-owned session, recovery cutoff, recovered timestamp, and limit. No API route, event
creation path, manual delivery route, or processing cycle invokes recovery. A recovered `pending`
job requires a later separate explicit processing-cycle invocation for delivery.

## Current limitations

- No worker or polling loop
- No automatic bounded-cycle invocation
- No automatic stale processing job recovery invocation
- No automatic execution of a scheduled `pending` retry
- No lease, lease owner, heartbeat, or configured stale timeout
- No exactly-once delivery
- Recovery after a crash following external HTTP but before commit can cause duplicate delivery
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
