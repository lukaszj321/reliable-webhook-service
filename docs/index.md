# Documentation

This documentation covers local development, PostgreSQL persistence, atomic event and initial job
creation, delivery job claiming, manual webhook execution, internal atomic attempt-plus-job
completion, the explicitly invoked bounded processing cycle, partial-progress semantics, retry
scheduling and terminal transitions, and the currently available HTTP API for Reliable Webhook
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
  with `SKIP LOCKED`.
- [Webhook delivery execution](delivery-execution.md) — manual execution,
  `execute_webhook_delivery_job`, the
  [bounded processing cycle](delivery-execution.md#bounded-delivery-processing-cycle), separate
  claim and per-job completion transactions, partial progress, `succeeded`, `pending`, and
  `dead_letter` transitions, and the absence of a long-running worker.
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
- [Run quality checks](development.md#quality-checks)
- [Stop PostgreSQL](development.md#stop-postgresql)
- [Review database connection configuration](database.md#connection-configuration)
- [Apply or inspect migrations](database.md#alembic-migrations)
- [Review the current database schema](database.md#database-schema)
- [Review atomic event and delivery job creation](database.md#atomic-event-and-delivery-job-creation)
- [Review delivery job claiming](database.md#delivery-job-claiming)
- [Review transaction ownership](database.md#transaction-ownership)
- [Review SKIP LOCKED concurrency semantics](database.md#postgresql-locking)
- [Review delivery execution flow](delivery-execution.md#current-execution-model)
- [Review the bounded processing cycle](delivery-execution.md#bounded-delivery-processing-cycle)
- [Review partial-progress semantics](delivery-execution.md#partial-progress)
- [Review delivery transaction ownership](delivery-execution.md#transaction-ownership)
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
