# Documentation

This documentation covers local development, PostgreSQL persistence, atomic event and initial
delivery job creation, delivery job claiming, webhook delivery execution, and the currently
available HTTP API for Reliable Webhook Delivery Service.

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
- [Webhook delivery execution](delivery-execution.md) — public manual execution, retry decisions,
  delivery job claiming infrastructure, attempt persistence, and the separation between job
  creation, claiming, and delivery execution.
- [API documentation](api/index.md) — health check, webhook endpoint, webhook event, and delivery
  attempt APIs.
- [Webhook endpoint API](api/webhook-endpoints.md) — endpoint creation, request validation, and
  listing behavior.
- [Webhook event API](api/webhook-events.md) — event validation, atomic event and pending job
  persistence, the event-only response, and error behavior.
- [Webhook delivery attempt API](api/webhook-delivery-attempts.md) — manual execution with `POST`
  and read-only listing with `GET`, including outcomes, ordering, and error responses.

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
