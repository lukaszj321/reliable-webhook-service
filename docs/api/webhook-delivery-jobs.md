# Webhook Delivery Job API

The delivery job API provides read-only operational inspection of current worker state without
claiming, replaying, or otherwise modifying a job.

## Contents

- [Event-scoped inspection](#event-scoped-inspection)
  - [Event-scoped response](#event-scoped-response)
  - [Event-scoped errors](#event-scoped-errors)
- [Collection inspection](#collection-inspection)
  - [Query parameters](#query-parameters)
  - [Collection response](#collection-response)
- [Status meanings](#status-meanings)
- [Ordering and pagination](#ordering-and-pagination)
  - [Cursor semantics](#cursor-semantics)
- [Read-only and race semantics](#read-only-and-race-semantics)
- [Safe response fields](#safe-response-fields)
- [Security](#security)
- [Navigation](#navigation)

## Event-scoped inspection

```text
GET /webhook-events/{event_id}/delivery-job
```

The endpoint returns the single delivery job associated with an existing event. It accepts no
request body or query parameters.

```powershell
curl.exe http://127.0.0.1:8000/webhook-events/00000000-0000-0000-0000-000000000001/delivery-job
```

### Event-scoped response

Successful requests return HTTP 200:

```json
{
  "id": "00000000-0000-0000-0000-000000000002",
  "event_id": "00000000-0000-0000-0000-000000000001",
  "status": "dead_letter",
  "attempt_count": 5,
  "next_attempt_at": null,
  "created_at": "2026-08-01T11:00:00Z",
  "updated_at": "2026-08-01T12:00:00Z"
}
```

### Event-scoped errors

| Condition | HTTP status |
|---|---:|
| Event does not exist | 404 |
| Event exists but its delivery job is missing | 409 |
| `event_id` is not a UUID | 422 |

[Back to contents](#contents)

## Collection inspection

```text
GET /webhook-delivery-jobs
```

The collection endpoint returns operational snapshots across events.

```powershell
curl.exe "http://127.0.0.1:8000/webhook-delivery-jobs?status=dead_letter&limit=25"
```

### Query parameters

| Parameter | Required | Default | Contract |
|---|---:|---:|---|
| `status` | No | None | `pending`, `processing`, `succeeded`, or `dead_letter` |
| `limit` | No | `50` | Integer from `1` through `100` |
| `cursor` | No | None | Opaque continuation cursor returned by the preceding page |

Invalid status, limit, malformed cursor, or cursor/filter mismatch returns HTTP 422.

### Collection response

```json
{
  "items": [
    {
      "id": "00000000-0000-0000-0000-000000000002",
      "event_id": "00000000-0000-0000-0000-000000000001",
      "status": "dead_letter",
      "attempt_count": 5,
      "next_attempt_at": null,
      "created_at": "2026-08-01T11:00:00Z",
      "updated_at": "2026-08-01T12:00:00Z"
    }
  ],
  "next_cursor": "eyJpZCI6Ii4uLiJ9"
}
```

`items` is empty when no rows match. `next_cursor` is `null` on the final page. The API does not
return a total count.

[Back to contents](#contents)

## Status meanings

| Status | Meaning |
|---|---|
| `pending` | Waiting until `next_attempt_at` makes the job eligible for claiming |
| `processing` | Claimed by a worker completion path |
| `succeeded` | Terminal state after successful delivery |
| `dead_letter` | Terminal state after the current automatic retry budget is exhausted |

Active states have a non-null `next_attempt_at`; terminal states return `null`.

[Back to contents](#contents)

## Ordering and pagination

Jobs are ordered exactly by:

```text
updated_at DESC, id DESC
```

`updated_at` places the most recently changed operational rows first. UUID descending is the
deterministic tie-breaker for equal timestamps. Pagination uses a keyset boundary:

```text
updated_at < cursor.updated_at
OR (updated_at = cursor.updated_at AND id < cursor.id)
```

There is no `OFFSET`, sorting parameter, or count query.

### Cursor semantics

The cursor is a versioned, deterministic JSON boundary encoded as URL-safe base64. It contains the
boundary timestamp, job UUID, and the status filter used to create it. A filtered cursor can only
continue the same filter; filtered and unfiltered cursors are not interchangeable.

The cursor is opaque as an API contract, but it is neither encrypted nor signed and must not be
treated as a security control. A syntactically valid modified cursor can only choose another
position in data already available through the endpoint.

[Back to contents](#contents)

## Read-only and race semantics

Both GET endpoints execute read-only queries. They do not commit, flush, refresh, claim, recover,
replay, perform downstream HTTP, or use `SELECT FOR UPDATE`.

A response is a point-in-time snapshot. For example:

1. a client reads a terminal `dead_letter` job;
2. another caller successfully invokes
   [`POST /webhook-events/{event_id}/replay`](webhook-events.md#manual-replay);
3. replay changes the same job to `pending`;
4. the earlier GET response remains historical, while a later GET observes `pending`;
5. another replay request can receive HTTP 409 because the job is no longer terminal.

GET does not hold a row lock and does not prevent this transition.

[Back to contents](#contents)

## Safe response fields

Responses contain only job ID, event ID, status, cycle attempt count, next-attempt timestamp, and
creation/update timestamps. They do not expose event payloads, event type, endpoint configuration,
target URLs, idempotency keys, delivery attempts, stored errors, authorization data, cursor
internals, or secrets.

[Back to contents](#contents)

## Security

Authentication and authorization are outside the current project scope. Production deployments
should restrict operational inspection according to their access-control requirements.

[Back to contents](#contents)

## Navigation

- [API documentation index](index.md)
- [Webhook event API](webhook-events.md)
- [Webhook delivery attempt API](webhook-delivery-attempts.md)
- [Webhook delivery execution](../delivery-execution.md)
- [Database and migrations](../database.md)
- [Main documentation index](../index.md)
- [Project README](../../README.md)
