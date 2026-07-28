# Documentation

This documentation covers local development, PostgreSQL persistence, atomic event and initial job
creation, delivery job claiming, manual webhook execution, internal atomic attempt-plus-job
completion, the explicitly started
[long-running worker process](delivery-execution.md#long-running-worker-process), its
framework-independent worker loop and one-shot
[bounded worker iteration](delivery-execution.md#bounded-worker-iteration), partial-progress
semantics, retry scheduling and terminal transitions, stale processing job recovery, the
duplicate remote delivery risk, and the currently available HTTP API for Reliable Webhook
Delivery Service.

## Start here

Read the documentation in this order:

1. [Development setup](development.md)
2. [Database and migrations](database.md)
3. [Webhook delivery execution](delivery-execution.md)
4. [API documentation](api/index.md)

## Documentation map

- [Development setup](development.md) — install, configure, run, and validate the project locally.
- [Database and migrations](database.md) — PostgreSQL connection configuration, Alembic
  migrations, atomic event and job transactions, the current schema, and delivery job claiming
  and recovery transaction semantics with `SKIP LOCKED`, including the separate recovery, claim,
  and per-job completion boundaries used by a bounded worker iteration.
- [Webhook delivery execution](delivery-execution.md) — the explicitly started long-running worker
  process, environment-driven worker configuration, polling, graceful shutdown, manual execution,
  `execute_webhook_delivery_job`, the
  [bounded processing cycle](delivery-execution.md#bounded-delivery-processing-cycle), separate
  claim and per-job completion transactions, partial progress, `succeeded`, `pending`, and
  `dead_letter` transitions, due retry execution in later polls, and
  [stale processing job recovery](delivery-execution.md#stale-processing-job-recovery). The guide
  distinguishes the worker process and worker loop from the
  [bounded worker iteration](delivery-execution.md#bounded-worker-iteration), which performs
  recovery-before-processing with independent limits and separate transactions. Partial progress
  and the duplicate remote delivery risk after an uncertain earlier HTTP result remain.
- [API documentation](api/index.md) — health check, webhook endpoint, webhook event, and delivery
  attempt APIs.
- [Webhook endpoint API](api/webhook-endpoints.md) — endpoint creation, request validation, and
  listing behavior.
- [Webhook event API](api/webhook-events.md) — event validation, atomic event and pending job
  persistence, the event-only response, and error behavior.
- [Webhook delivery attempt API](api/webhook-delivery-attempts.md) — manual execution with `POST`,
  including the manual route commit and unchanged completed-attempt response, plus read-only
  listing with `GET`, outcomes, ordering, and error responses.

## Common tasks

- [Set up the development environment](development.md#create-a-virtual-environment)
- [Configure local environment variables](development.md#configure-the-local-environment)
- [Start PostgreSQL](development.md#start-postgresql)
- [Apply database migrations](development.md#apply-database-migrations)
- [Run the application](development.md#run-the-application)
- [Run the long-running worker](development.md#run-the-worker)
- [Run quality checks](development.md#quality-checks)
- [Stop PostgreSQL](development.md#stop-postgresql)
- [Review database connection configuration](database.md#connection-configuration)
- [Apply or inspect migrations](database.md#alembic-migrations)
- [Review the current database schema](database.md#database-schema)
- [Review atomic event and delivery job creation](database.md#atomic-event-and-delivery-job-creation)
- [Review delivery job claiming](database.md#delivery-job-claiming)
- [Review claim transaction ownership](database.md#claim-transaction-ownership)
- [Review SKIP LOCKED concurrency semantics](database.md#postgresql-locking)
- [Review delivery execution flow](delivery-execution.md#current-execution-model)
- [Review the long-running worker process](delivery-execution.md#long-running-worker-process)
- [Review the bounded processing cycle](delivery-execution.md#bounded-delivery-processing-cycle)
- [Review the bounded worker iteration](delivery-execution.md#bounded-worker-iteration)
- [Review stale processing job recovery](delivery-execution.md#stale-processing-job-recovery)
- [Review partial-progress semantics](delivery-execution.md#partial-progress)
- [Review delivery transaction ownership](delivery-execution.md#delivery-transaction-ownership)
- [Review atomic delivery job completion](delivery-execution.md#delivery-job-completion)
- [Review claim and completion transaction boundaries](delivery-execution.md#delivery-job-claiming)
- [Review delivery result classification](delivery-execution.md#result-classification)
- [Review attempt numbering](delivery-execution.md#attempt-numbering)
- [Review delivery limitations](delivery-execution.md#current-limitations)
- [Review available API endpoints](api/index.md#available-api-areas)
- [Create a webhook endpoint](api/webhook-endpoints.md#create-a-webhook-endpoint)
- [List webhook endpoints](api/webhook-endpoints.md#list-webhook-endpoints)
- [Review request validation](api/webhook-endpoints.md#request-validation)
- [Create a webhook event](api/webhook-events.md#endpoint)
- [Review webhook event persistence behavior](api/webhook-events.md#persistence-behavior)
- [Manually execute one webhook delivery](api/webhook-delivery-attempts.md#manual-delivery-endpoint)
- [List delivery attempts](api/webhook-delivery-attempts.md#listing-endpoint)

## Navigation

- [Project README](../README.md)
