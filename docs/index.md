# Documentation

This documentation covers local development, the PostgreSQL database, webhook delivery execution,
and the currently available HTTP API for Reliable Webhook Delivery Service.

## Start here

Read the documentation in this order:

1. [Development setup](development.md)
2. [Database and migrations](database.md)
3. [Webhook delivery execution](delivery-execution.md)
4. [API documentation](api/index.md)

## Documentation map

- [Development setup](development.md) — install, configure, run, and validate the project locally.
- [Database and migrations](database.md) — PostgreSQL connection configuration, Alembic
  migrations, and the current database schema.
- [Webhook delivery execution](delivery-execution.md) — synchronous request execution, result
  classification, attempt persistence, and current limitations.
- [API documentation](api/index.md) — health check, webhook endpoint, webhook event, and delivery
  attempt APIs.
- [Webhook endpoint API](api/webhook-endpoints.md) — endpoint creation, request validation, and
  listing behavior.
- [Webhook event API](api/webhook-events.md) — event creation, validation, persistence, and error
  responses.
- [Webhook delivery attempt API](api/webhook-delivery-attempts.md) — read-only listing of stored
  attempts, ordering, and error responses.

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
- [Review delivery execution flow](delivery-execution.md#current-execution-model)
- [Review delivery result classification](delivery-execution.md#result-classification)
- [Review attempt numbering](delivery-execution.md#attempt-numbering)
- [Review delivery limitations](delivery-execution.md#current-limitations)
- [Review available API endpoints](api/index.md#available-api-areas)
- [Create a webhook endpoint](api/webhook-endpoints.md#create-a-webhook-endpoint)
- [List webhook endpoints](api/webhook-endpoints.md#list-webhook-endpoints)
- [Review request validation](api/webhook-endpoints.md#request-validation)
- [Create a webhook event](api/webhook-events.md#endpoint)
- [List delivery attempts](api/webhook-delivery-attempts.md#endpoint)

## Navigation

- [Project README](../README.md)
