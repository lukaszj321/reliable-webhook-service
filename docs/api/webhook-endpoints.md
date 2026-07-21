# Webhook endpoint API

This API manages configurations for webhook destination addresses.

## Contents

- [Data model](#data-model)
- [Create a webhook endpoint](#create-a-webhook-endpoint)
- [Request validation](#request-validation)
- [List webhook endpoints](#list-webhook-endpoints)
- [Status codes](#status-codes)
- [Current limitations](#current-limitations)
- [Navigation](#navigation)

## Data model

Webhook endpoint responses contain exactly six fields:

- `id` — UUID primary key generated for the configuration.
- `name` — human-readable endpoint name.
- `target_url` — HTTP or HTTPS destination URL.
- `is_active` — whether the endpoint configuration is active.
- `created_at` — timezone-aware creation timestamp.
- `updated_at` — timezone-aware last modification timestamp.

See the [`webhook_endpoints` database schema](../database.md#webhook_endpoints) for persistence
details.

## Create a webhook endpoint

```text
POST /webhook-endpoints
```

Creates a webhook destination configuration, stores it in PostgreSQL, and returns HTTP 201.
`id`, `is_active`, `created_at`, and `updated_at` are set by the application or database.

Example request:

```json
{
  "name": "Primary webhook endpoint",
  "target_url": "https://example.com/webhooks"
}
```

Example response:

```json
{
  "id": "5dce6a1d-f4c7-4c16-b709-2b0d08683ed2",
  "name": "Primary webhook endpoint",
  "target_url": "https://example.com/webhooks",
  "is_active": true,
  "created_at": "2026-07-20T20:00:00Z",
  "updated_at": "2026-07-20T20:00:00Z"
}
```

## Request validation

The `name` field:

- has leading and trailing whitespace removed;
- must contain between 1 and 255 characters after trimming;
- rejects an empty string;
- rejects a value containing only whitespace.

The `target_url` field:

- must be a valid URL;
- accepts only HTTP and HTTPS;
- has a maximum length of 2048 characters;
- rejects unsupported schemes such as FTP;
- may be normalized by Pydantic before it is stored.

Invalid request data returns HTTP 422. Neither `name` nor `target_url` is required to be unique.
The database schema does not define a unique constraint for either field.

## List webhook endpoints

```text
GET /webhook-endpoints
```

Returns HTTP 200 with a JSON array containing all stored endpoint configurations. When no records
exist, the response is an empty array (`[]`). Results are sorted by `created_at` ascending, with
`id` ascending as the second sort key. Pagination is not currently supported.

Example response:

```json
[
  {
    "id": "5dce6a1d-f4c7-4c16-b709-2b0d08683ed2",
    "name": "Primary webhook endpoint",
    "target_url": "https://example.com/webhooks",
    "is_active": true,
    "created_at": "2026-07-20T20:00:00Z",
    "updated_at": "2026-07-20T20:00:00Z"
  }
]
```

## Status codes

| Method | Path | Status | Meaning |
|---|---|---:|---|
| POST | `/webhook-endpoints` | 201 | Endpoint configuration created |
| POST | `/webhook-endpoints` | 422 | Request validation failed |
| GET | `/webhook-endpoints` | 200 | Endpoint configurations returned |

## Current limitations

- Pagination is not supported.
- Filtering is not supported.
- A detail endpoint is not available.
- Update operations are not available.
- Delete operations are not available.
- Authentication is not implemented.

## Navigation

- [API documentation index](index.md)
- [Database and migrations](../database.md)
- [Main documentation index](../index.md)
- [Project README](../../README.md)
