# Webhook SSRF boundary spike (#57)

Status: design decision; no production implementation. The PoC is retained only in
`tests/experimental/test_webhook_ssrf_boundary_spike.py`.

## Problem and reachability

Webhook endpoint URLs are untrusted outbound destinations. A valid public hostname can resolve to
loopback, link-local, private, reserved, multicast, or otherwise forbidden space, and can change
answers between validation and connection. Redirects are already disabled by the delivery adapter
with `follow_redirects=False` and must remain disabled, but that does not close DNS rebinding or
proxy-routing paths. The boundary must bind an approved DNS snapshot to the connection, preserve
the original HTTP and TLS identity, and verify the peer before the first request byte is written.

This spike used no sockets, system DNS, internet, or real connections. Resolver, dial, peer, TLS,
stream, and HTTP-response behavior is deterministic and test-local.

## Existing execution and state map

| Path | Current flow | Required policy-rejection behavior |
| --- | --- | --- |
| Manual | The API transaction performs HTTP, records a completed attempt, and commits | Validate before HTTP. Return a tagged rejection outcome, persist and commit it in the route-owned transaction, then return a safe `422`; create no attempt and do not mutate a job. |
| Worker | Current code bulk-claims due jobs as `processing`, sets `updated_at`, commits those claim locks, passes only job IDs, then completes jobs serially through an unlocked reload. It has no durable claim identity. | Follow-up 1 replaces bulk claim with a one-job just-in-time claim, increments a durable `claim_generation`, propagates an immutable claim handle, and requires pre-request and locked completion validation. |
| Recovery | A separate transaction finds stale `processing` rows by `updated_at`, changes them to `pending`, and sets `next_attempt_at=recovered_at` | Follow-up 1 uses a dedicated `processing_started_at` age timestamp. Recovery invalidates the old handle by changing status, retains its generation, and the next claim increments the generation. |
| Replay | A terminal job is locked and reset to `pending`, `attempt_count=0`, and a new due time | Enqueue under the existing row lock without DNS. Worker completion performs the authoritative current-policy check and may terminalize another rejection. Preserve all rejection history. |

The important recovery sequence stays `processing -> stale recovery -> pending -> processing`.
Validation and terminalization belong to completion, not claim: a crash before completion commit is
recoverable. Earlier per-job commits remain committed. A permanent rejection is a normal per-job
outcome and must not stop later eligible jobs.

The selected follow-up contract removes up-front batch claim structurally. An iteration may process
at most the existing `WEBHOOK_WORKER_PROCESSING_LIMIT=100`, but claims at most one due job
immediately before executing it. Other jobs remain `pending` while waiting. Only after the current
job's completion transaction commits or rolls back may the iteration claim the next job. Recovery
therefore sees at most the actively owned `processing` job for that serial iteration, rather than
jobs aging in a processing queue.

## Invariants

- Resolve once for a connection and validate every returned address, fail closed.
- Apply one monotonic delivery deadline to resolution, snapshot validation, every connection
  attempt, TLS setup, request write, and response read. Each operation receives only the remaining
  budget, and no operation or dial starts when that budget is exhausted.
- Bound the single claimed job from claim through completion. Pending jobs do not consume a stale
  lease while waiting their turn.
- Fence every worker-owned persistence path with an immutable claim handle containing `job_id`,
  `delivery_cycle`, and a monotonically increasing `claim_generation`. A timestamp determines claim
  age; it must never substitute for claim identity.
- Bound raw resolver iteration before normalization, then normalize, validate, and deduplicate
  before enforcing the independent unique-address limit. Never silently truncate an oversized
  answer set. Interleave sorted IPv4 and IPv6 buckets before applying the independent connection-
  attempt limit, so neither family is starved.
- Dial only numeric literals from the immutable approved snapshot. Fallback stays within that
  snapshot and performs no second lookup.
- Preserve the original hostname for HTTP `Host`, TLS SNI, hostname verification, certificate
  identity, and origin pooling. Never replace request authority with the numeric address.
- Use an `SSLContext` with certificate verification required and hostname checking enabled.
- Inspect the connected peer before an HTTP write; it must remain allowed and be in the snapshot.
- Reject unsupported schemes, forbidden ports, URL credentials, empty answers, malformed
  addresses, and any mixed allowed/denied answer set before dialing.
- Normalize IPv4, IPv6, and IPv4-mapped IPv6 before policy evaluation. Explicitly classify
  loopback, private, link-local, unspecified, multicast, reserved/special-use, and configured
  denied ranges. Record a stable policy version and reason.
- Disable environment proxies with `trust_env=False`. No proxy is safe unless a separate design
  proves equivalent connection-bound enforcement.
- Preserve `follow_redirects=False` at the existing adapter call site.
- A pre-HTTP rejection is not a delivery attempt: create no `WebhookDeliveryAttempt`, do not change
  `attempt_count`, and set `next_attempt_at=None` when terminalizing a worker job.
- Destination-policy rejection is a typed neutral internal outcome, not a timeout or transport
  failure. It must bypass generic `httpx2.RequestError`/transport-error normalization and carry
  only stable safe metadata needed by its caller's transaction.
- Preserve current public APIs and schemas until an approved follow-up adds its migration or error
  contract.

### Selected timeout and fallback contract

The production follow-up must preserve `WEBHOOK_DELIVERY_TIMEOUT_SECONDS`, default `10.0`, but
redefine it as the one finite, positive total monotonic deadline described above rather than a
fresh timeout for each operation. It adds these bounded settings:

| Setting | Meaning | Default | Valid values and invariant |
| --- | --- | --- | --- |
| `WEBHOOK_DELIVERY_MAX_RESOLVED_ADDRESSES` | Maximum unique normalized addresses in one authoritative DNS snapshot | `8` | Integer `1..32`; deduplicate before checking; overflow rejects the whole snapshot |
| `WEBHOOK_DELIVERY_MAX_CONNECT_ATTEMPTS` | Maximum numeric dials within the approved snapshot | `4` | Integer `1..8` and no greater than the resolved-address limit |
| `WEBHOOK_WORKER_STALE_SAFETY_MARGIN_SECONDS` | Budget for post-claim preparation, DB/pool waits, completion persistence, and scheduling overhead outside the nested delivery deadline | `30.0` | Finite positive number |
| `WEBHOOK_WORKER_STALE_PROCESSING_TIMEOUT_SECONDS` | Age after just-in-time claim before the one `processing` job may be recovered | Existing `300.0` | Must be at least the derived claim-to-completion budget, currently `40.0` |

The transport also has the non-negotiable implementation limits
`MAX_RAW_RESOLVER_RECORDS = 32`, `MAX_NORMALIZED_ADDRESSES = 8`, and
`MAX_CONNECT_ATTEMPTS = 4`. The raw cap counts every yielded resolver record before
deduplication, including a duplicate and a value that will later fail normalization. On record 33
the boundary rejects the entire answer immediately, stops iteration, returns no partial snapshot,
dials nothing, and performs no HTTP write. For at most 32 records it then normalizes and validates
every value, deduplicates, rejects more than 8 unique addresses, interleaves the approved families,
and applies the cap of 4 connection attempts to that interleaved result. The raw cap bounds CPU and
memory work on duplicate-heavy or malformed answers; the unique cap bounds the immutable approved
snapshot; the attempt cap bounds connection amplification. All three limits are independent.

Approved-address ordering is exact: normalize and deduplicate; split into IPv4 and IPv6 buckets;
sort each bucket by packed bytes; then take one item from each available bucket in fixed family
order IPv4, IPv6, repeating until both are empty. Apply the connection-attempt cap only afterward.
Thus IPv4 `192.0.2.10`, `192.0.2.20`, `192.0.2.30` and IPv6 `2001:db8::10`,
`2001:db8::20` produce `192.0.2.10`, `2001:db8::10`, `192.0.2.20`,
`2001:db8::20`, `192.0.2.30`. Resolver answer order cannot affect this order. When both families
exist, the first two attempts cover both; four unavailable addresses of either family cannot push
the other family outside the first four attempts. Interleaving never changes an allow/deny
decision, and every dial still consumes only the immutable approved numeric snapshot.

Each just-in-time claim starts one monotonic claim-to-completion budget derived as
`WEBHOOK_DELIVERY_TIMEOUT_SECONDS + WEBHOOK_WORKER_STALE_SAFETY_MARGIN_SECONDS`, currently
`10.0 + 30.0 = 40.0` seconds. It covers every post-claim preparation step, database/connection-pool
wait, the nested delivery deadline, and completion persistence. Every DB, pool, statement, and
transport operation receives only the remaining claim budget and may not begin when it is
exhausted; the nested delivery budget is the lesser of its 10-second cap and the remaining claim
budget. Startup enforces `WEBHOOK_WORKER_STALE_PROCESSING_TIMEOUT_SECONDS >= 40.0` and rejects any
invalid combination. The current `300.0`-second stale default remains compatible.

This removes the earlier `100 * 10` queue-age problem by leaving waiting jobs `pending`, not by
adding a fixed margin to bulk-owned processing rows. In supported bounded execution it prevents
premature recovery of the one active job. It does not provide exactly-once delivery if a process
dies after the HTTP side effect but before durable completion; that existing at-least-once window
remains. Future parallelism or bulk claim requires a new ownership/lease derivation and controlled
tests. Deadline exhaustion maps to a safe timeout, address-count overflow maps to a safe
destination-policy rejection, and connect-attempt exhaustion maps to a safe transport failure.
None may expose resolved addresses or resolver details.

### Selected durable claim-identity contract

Follow-up 1 adds `WebhookDeliveryJob.claim_generation BIGINT NOT NULL DEFAULT 0`, backfills every
existing job to `0`, and enforces a nonnegative value. It is monotonic within one job and is never
reset by automatic retry, stale recovery, replay, or terminalization. Every successful
`pending -> processing` claim increments it exactly once. PostgreSQL's signed `BIGINT` maximum is
`9223372036854775807`; the claim operation checks for that maximum while holding the job row lock
and returns a fail-closed internal claim-overflow outcome before mutation. It must not rely on a
database overflow error that aborts the transaction.

It also adds two nullable completion-marker columns:

```sql
last_completed_delivery_cycle BIGINT NULL,
last_completed_claim_generation BIGINT NULL,
CONSTRAINT ck_webhook_delivery_job_last_completed_pair
CHECK (
    (last_completed_delivery_cycle IS NULL) =
    (last_completed_claim_generation IS NULL)
)
```

Both values are therefore always simultaneously `NULL` or simultaneously non-`NULL`. Existing
rows are safely backfilled as `(NULL, NULL)`; migration must not infer an earlier accepted
completion. Such an unmarked legacy row cannot support duplicate readback and fails closed as
`stale-claim`. For every accepted worker completion—success, retryable failure transitioning to
`pending`, retry-exhausted failure, and worker destination-policy rejection—the completion
transaction sets the pair to its incoming `(delivery_cycle, claim_generation)` atomically under
the same job row lock that accepts completion. It does so before or atomically with the attempt or
rejection insert, status transition, `processing_started_at` clear, `next_attempt_at` update, and
terminal projection. Stale recovery, claim acquisition, retry scheduling outside an accepted
completion, manual delivery, manual policy rejection, and advisory preflight never set or change
the pair. Replay increments `delivery_cycle` and may retain the historical pair; the cycle mismatch
fences it. The next successful claim increments `claim_generation`, so an older handle is stale
even if it equals the retained historical pair. The marker identifies only the last accepted
worker completion handle. It stores no HTTP result and does not add outcome identity to
`WebhookDeliveryAttempt`.

Follow-up 1 also adds nullable `processing_started_at`. Its migration sets it to `updated_at` for
rows that are `processing` at migration time and to `NULL` for every other row. A database
constraint enforces `processing_started_at IS NOT NULL` exactly when `status='processing'`.
Operational recovery indexes, filters, and ordering move from `updated_at` to
`processing_started_at`. `updated_at` remains ordinary row-change metadata. The timestamp answers
how old the current processing state is; `claim_generation` answers which claim owns it.

The atomic claim transaction selects one claimable pending job under the existing appropriate row
lock, revalidates that it is still claimable, checks generation overflow, increments
`claim_generation` once, sets `status='processing'`, sets `processing_started_at` to the normalized
claim time, and flushes. Before the ORM object is detached or the claim session closes, the service
returns an immutable scalar `ClaimHandle(job_id, delivery_cycle, claim_generation)`. No supported
path may set a job to `processing` without performing that increment in the same transaction.
Worker iteration propagates the complete handle into execution and completion rather than carrying
only a job ID. The normative worker claim identity is therefore
`job_id + delivery_cycle + claim_generation`.

Immediately before DNS resolution or HTTP, an application/persistence service performs a
pre-request validation of the handle against current `status='processing'`, `job_id`,
`delivery_cycle`, and `claim_generation`. It releases the session and any database resources before
network work; no row lock is held across DNS or HTTP. A mismatch returns an internal stale-claim
outcome, performs no DNS or HTTP, creates no attempt or rejection, mutates no job, and lets the
worker continue its batch. This check narrows but cannot eliminate the race in which recovery
occurs after validation and while the request is already in flight.

Before any persistent completion change, one shared boundary selects the job with
`SELECT ... FOR UPDATE` and classifies the incoming
`ClaimHandle(job_id, delivery_cycle, claim_generation)` in this exact order:

1. **Current-handle comparison.** If incoming `delivery_cycle` or `claim_generation` differs from
   the job's current values, return `stale-claim`. Perform no mutation and do not read an outcome
   belonging to a newer claim.
2. **Active processing claim.** If the incoming pair matches, `status == processing`, and
   `processing_started_at IS NOT NULL`, the claim is active and completion may be accepted.
3. **Accepted duplicate.** If the incoming pair matches, the job is no longer `processing`, and
   both last-completed fields match the incoming pair, the completion was already accepted. For a
   worker policy rejection, return exact idempotent rejection readback only when the worker record
   and job pointer match that same pair. For success, retryable failure, or retry-exhausted failure,
   return `already-completed`. Perform no mutation and create no attempt or rejection.
4. **Recovered same-generation claim.** If the incoming pair matches, the job is no longer
   `processing`, and the last-completed pair does not match, return `stale-claim` with no mutation.
   This includes recovery changing `processing` to `pending` without accepting completion.

Every worker persistence path passes through this ordered boundary. Neither non-mutating result
automatically retries mutation; the worker continues its batch.

Completion outcomes have two distinct post-completion contracts. Same-handle idempotent readback
is required only for a worker policy rejection because that path has a durable rejection record
containing `job_id`, `delivery_cycle`, and `claim_generation`. When the existing worker rejection
matches the complete handle, `source=worker`, policy identifier/version, and safe reason code,
persistence may return that existing rejection outcome without creating another rejection,
attempt, or job mutation. Metadata mismatch is fail-closed.

Successful HTTP delivery, failed HTTP delivery, retry scheduling, retry-exhaustion terminalization,
and ordinary `WebhookDeliveryAttempt` persistence have no equivalent durable completion-outcome
identity in this design. After the first completion has committed, a second completion carrying
the same full handle observes that the job is no longer the matching active `processing` claim and
returns an internal `already-completed` outcome without another attempt, status change,
`attempt_count` change, `next_attempt_at` change, or other durable mutation. It does not promise to
reconstruct the original HTTP response, transport failure, retry decision, or attempt outcome.
If the status, cycle, or generation instead shows that recovery, replay, retry, or another claim
has invalidated the handle, the result is `stale-claim`, not `already-completed`, and no outcome
belonging to another claim may be read back.

The spike deliberately does not add `claim_generation` or a separate completion identity to
`WebhookDeliveryAttempt`. An attempt remains the record created by one accepted completion
transaction. Duplicate prevention comes from the locked full-handle check before insertion; a
later completion creates no second attempt. Exact readback of an ordinary attempt or public
completion idempotency is outside scope, and no new attempt uniqueness constraint is selected
without evidence that one is required.

Stale recovery selects and locks processing rows whose `processing_started_at` exceeds the stale
threshold, sets them to `pending`, clears `processing_started_at`, and preserves
`delivery_cycle`, `claim_generation`, and `attempt_count`; it creates no attempt or rejection.
It does not write the last-completed marker, which makes a late same-generation completion
classifiable as stale. The next claim increments the retained generation. Accepted retry
scheduling likewise changes `processing -> pending`, clears
`processing_started_at`, and retains the current generation until the next claim. Valid completion
to `succeeded` or `dead_letter` also clears `processing_started_at` and retains the last generation.

Replay keeps its existing row lock and terminal-only precondition. It increments `delivery_cycle`
exactly once, sets the job to `pending`, clears terminal projection, and neither resets nor
increments `claim_generation`; only the future worker claim increments it. Claim identities are
therefore never reused, including after replay. The required stale sequence is concrete:

- worker A claims `(delivery_cycle=0, claim_generation=1)`;
- recovery sets `pending` and retains generation `1`;
- worker B claims `(delivery_cycle=0, claim_generation=2)`;
- worker A's completion expects generation `1`, observes `2`, and is rejected as stale;
- worker B's completion expects and observes generation `2` and may continue.

The marker makes the formerly ambiguous cycle `0`, generation `7` cases durable:

- **Accepted retryable completion:** the worker finishes its request; the locked completion check
  accepts `(0, 7)`; one failed attempt is inserted; the last-completed pair is set to `(0, 7)`; and
  the job becomes `pending`. A duplicate completion with `(0, 7)` returns `already-completed`,
  creates no additional attempt, and performs no additional mutation.
- **Recovery before completion:** recovery locks and changes the processing job to `pending`, but
  does not set the pair to `(0, 7)`. The late completion with `(0, 7)` returns `stale-claim`, creates
  no attempt, and performs no additional mutation.

### Selected URL and special-use address policy

Only `http` and `https` targets are supported. URL userinfo is forbidden: any username or password
causes rejection. Ports `1..65535`, including non-default ports, are allowed; port `0` and values
outside that range are rejected. A non-default port does not change address classification and is
subject to the same deadline, connection, proxy, and peer rules.

Follow-up 2 must implement and test this normative policy rather than rely on one broad
`is_global` predicate:

| Address class | IPv4 examples/ranges | IPv6 examples/ranges | Decision |
| --- | --- | --- | --- |
| Public globally routable unicast | Public unicast not covered below | Public global unicast not covered below | Allow |
| Loopback | `127.0.0.0/8` | `::1/128` | Deny |
| Private / unique-local | RFC 1918 | `fc00::/7` | Deny |
| Link-local | `169.254.0.0/16` | `fe80::/10` | Deny |
| Unspecified | `0.0.0.0/8` | `::/128` | Deny |
| Multicast | `224.0.0.0/4` | `ff00::/8` | Deny |
| Carrier-grade NAT/shared | `100.64.0.0/10` | Not applicable | Deny |
| Documentation and benchmarking | RFC 5737 and benchmark ranges | RFC 3849 and benchmark ranges | Deny |
| Reserved, protocol-assignment, and other special-use | IANA special-purpose ranges | IANA special-purpose ranges | Deny |
| Cloud metadata | `169.254.169.254` and configured provider ranges | Configured provider ranges | Deny |
| IPv4-mapped IPv6 | Embedded IPv4 in `::ffff:0:0/96` | Same address representation | Classify and enforce both the IPv6 wrapper and embedded IPv4; deny if either is denied |

Literal IP hosts pass through the same normalization and table. For DNS names, every answer must
be allowed; a mixed answer fails closed before dial.

URL host parsing and numeric normalization also fail closed before resolution or dialing. IPv6
zone identifiers are unsupported and rejected, including bracketed IPv6 with `%zone` and
percent-encoded scope syntax such as `%25`. Alternate IPv4 textual forms are rejected: integer,
octal, hexadecimal, mixed-base components, and shortened dotted forms. Tests also cover
IPv4-mapped IPv6, uppercase and other non-canonical IPv6 spellings, and hosts for which the URL
parser's normalization is ambiguous. A numeric host is accepted only when it has one unambiguous
canonical representation. The URL parser, policy classifier, and numeric dialer must never
interpret the same host differently. Any rejection happens before resolution or dial and therefore
before an HTTP write. This spike specifies those production tests; it does not implement a parser.

## Dependency evidence: httpx2/httpcore2 2.9.1

Commands:

```powershell
& ".\.venv\Scripts\python.exe" -c "import httpx2,httpcore2; print(httpx2.__version__, httpx2.__file__); print(httpcore2.__version__, httpcore2.__file__)"
& ".\.venv\Scripts\python.exe" -c "import inspect,httpx2,httpcore2; print(inspect.signature(httpx2.BaseTransport.handle_request)); print(inspect.signature(httpcore2.ConnectionPool)); print(httpcore2.NetworkBackend); print(httpcore2.NetworkStream)"
rg -n "class NetworkBackend|class NetworkStream|def handle_request|def start_tls|connect_tcp" .venv/Lib/site-packages/httpcore2 .venv/Lib/site-packages/httpx2/_transports
```

Observed output:

```text
httpx2 2.9.1 C:\Users\user\Documents\Projekty\reliable webhook service\.venv\Lib\site-packages\httpx2\__init__.py
httpcore2 2.9.1 C:\Users\user\Documents\Projekty\reliable webhook service\.venv\Lib\site-packages\httpcore2\__init__.py
BaseTransport (self, request: 'Request') -> 'Response'
ConnectionPool (ssl_context: 'ssl.SSLContext | None' = None, proxy: 'Proxy | None' = None, max_connections: 'int | None' = 10, max_keepalive_connections: 'int | None' = None, keepalive_expiry: 'float | None' = None, http1: 'bool' = True, http2: 'bool' = False, retries: 'int' = 0, local_address: 'str | None' = None, uds: 'str | None' = None, network_backend: 'NetworkBackend | None' = None, socket_options: 'typing.Iterable[SOCKET_OPTION] | None' = None) -> 'None'
NetworkBackend <class 'httpcore2.NetworkBackend'>
NetworkStream <class 'httpcore2.NetworkStream'>
```

Exact package metadata command:

```powershell
& ".\.venv\Scripts\python.exe" -m pip show httpx2 httpcore2
```

Exact output:

```text
Name: httpx2
Version: 2.9.1
Summary: The next generation HTTP client.
Home-page: https://github.com/pydantic/httpx2
Author:
Author-email: Tom Christie <tom@tomchristie.com>
License-Expression: BSD-3-Clause
Location: C:\Users\user\Documents\Projekty\reliable webhook service\.venv\Lib\site-packages
Requires: anyio, httpcore2, idna, truststore, typing-extensions
Required-by: reliable-webhook-service
---
Name: httpcore2
Version: 2.9.1
Summary: A minimal low-level HTTP client.
Home-page: https://github.com/pydantic/httpx2
Author:
Author-email: Tom Christie <tom@tomchristie.com>
License-Expression: BSD-3-Clause
Location: C:\Users\user\Documents\Projekty\reliable webhook service\.venv\Lib\site-packages
Requires: h11, truststore
Required-by: httpx2
```

Installed-source symbols and conclusions:

- `httpx2.BaseTransport.handle_request(request)` is the public synchronous transport seam.
- `httpcore2.ConnectionPool(..., ssl_context=..., network_backend=...,
  max_keepalive_connections=...)` accepts a public injected backend.
- `httpcore2.NetworkBackend.connect_tcp(host, port, ...)` owns TCP establishment.
- `httpcore2.NetworkStream.start_tls(ssl_context, server_hostname, ...)` receives SNI;
  `get_extra_info(...)` is the peer/SSL inspection seam.
- Pooling is keyed by original origin, retaining hostname authority while the backend dials a
  numeric address.

Signatures prove only that a callable/parameter exists in this installed version. Source
inspection proves only the inspected branch/path. Neither is end-to-end evidence; behavioral
claims below require the deterministic PoC or a future real-backend compatibility test.

| Claim | Exact command | Inspected path and symbol | Observed result | Conclusion | Evidence status | PoC required? |
| --- | --- | --- | --- | --- | --- | --- |
| `HTTPTransport` initialization and dispatch path | `& ".\.venv\Scripts\python.exe" -c "import inspect,httpx2,httpcore2; print('HTTPTransport.__init__', inspect.signature(httpx2.HTTPTransport)); print('BaseTransport.handle_request', inspect.signature(httpx2.BaseTransport.handle_request)); print('ConnectionPool.__init__', inspect.signature(httpcore2.ConnectionPool)); print('NetworkBackend.connect_tcp', inspect.signature(httpcore2.NetworkBackend.connect_tcp)); print('NetworkStream.start_tls', inspect.signature(httpcore2.NetworkStream.start_tls))"`; `rg -n -F -e "class HTTPTransport" -e "def __init__" -e "ConnectionPool(" -e "HTTPProxy(" -e "SOCKSProxy(" -e "def handle_request" .venv/Lib/site-packages/httpx2/_transports/default.py` | `.venv/Lib/site-packages/httpx2/_transports/default.py`; `HTTPTransport.__init__`, `handle_request` | Signature exposes verify/trust/proxy/limits; source selects pool/proxy then adapts requests to httpcore2 | Documents the ordinary path and why private `_pool` mutation is rejected; does not prove guarded delivery | Signature + source confirmed | Yes |
| `ConnectionPool` initialization/path | `& ".\.venv\Scripts\python.exe" -c "import inspect,httpx2,httpcore2; print('HTTPTransport.__init__', inspect.signature(httpx2.HTTPTransport)); print('BaseTransport.handle_request', inspect.signature(httpx2.BaseTransport.handle_request)); print('ConnectionPool.__init__', inspect.signature(httpcore2.ConnectionPool)); print('NetworkBackend.connect_tcp', inspect.signature(httpcore2.NetworkBackend.connect_tcp)); print('NetworkStream.start_tls', inspect.signature(httpcore2.NetworkStream.start_tls))"`; `rg -n -F -e "class ConnectionPool" -e "def __init__" -e "def connect_tcp" -e "server_hostname" -e "server_addr" -e "max_keepalive_connections" .venv/Lib/site-packages/httpcore2/_sync/connection_pool.py .venv/Lib/site-packages/httpcore2/_sync/connection.py .venv/Lib/site-packages/httpcore2/_backends/base.py .venv/Lib/site-packages/httpcore2/_backends/sync.py` | `.venv/Lib/site-packages/httpcore2/_sync/connection_pool.py`; `ConnectionPool.__init__`, `handle_request` | Constructor accepts `ssl_context`, `network_backend`, and keepalive controls; source assigns requests by origin | Public injection seam exists in 2.9.1; signature alone proves no security property | Signature + source confirmed; offline use confirmed | Yes |
| `NetworkBackend.connect_tcp` seam | `& ".\.venv\Scripts\python.exe" -c "import inspect,httpx2,httpcore2; print('HTTPTransport.__init__', inspect.signature(httpx2.HTTPTransport)); print('BaseTransport.handle_request', inspect.signature(httpx2.BaseTransport.handle_request)); print('ConnectionPool.__init__', inspect.signature(httpcore2.ConnectionPool)); print('NetworkBackend.connect_tcp', inspect.signature(httpcore2.NetworkBackend.connect_tcp)); print('NetworkStream.start_tls', inspect.signature(httpcore2.NetworkStream.start_tls))"`; `rg -n -F -e "class ConnectionPool" -e "def __init__" -e "def connect_tcp" -e "server_hostname" -e "server_addr" -e "max_keepalive_connections" .venv/Lib/site-packages/httpcore2/_sync/connection_pool.py .venv/Lib/site-packages/httpcore2/_sync/connection.py .venv/Lib/site-packages/httpcore2/_backends/base.py .venv/Lib/site-packages/httpcore2/_backends/sync.py` | `.venv/Lib/site-packages/httpcore2/_backends/base.py`; `NetworkBackend.connect_tcp` | Method receives host, port, timeout, local address, and socket options and returns `NetworkStream` | A custom backend can own connection establishment; correct binding remains implementation work | Signature + source confirmed; fake backend exercised | Yes |
| Address passed to connection/dial | `& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py`; `rg -n -F -e "class ConnectionPool" -e "def __init__" -e "def connect_tcp" -e "server_hostname" -e "server_addr" -e "max_keepalive_connections" .venv/Lib/site-packages/httpcore2/_sync/connection_pool.py .venv/Lib/site-packages/httpcore2/_sync/connection.py .venv/Lib/site-packages/httpcore2/_backends/base.py .venv/Lib/site-packages/httpcore2/_backends/sync.py` | `.venv/Lib/site-packages/httpcore2/_sync/connection.py`; `HTTPConnection._connect`; experimental `_SnapshotNetworkBackend.connect_tcp` and `_OfflineNumericDialer.connect` | httpcore2 passes the original origin host to the injected backend; the PoC backend consumes its bound snapshot and passes numeric literals only to the fake dialer | Numeric dialing is PoC-confirmed only inside the injected boundary, not by the signature or a real socket | Offline PoC confirmed; real backend unconfirmed | Yes |
| TLS `server_hostname` | `& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py`; `rg -n -F -e "class ConnectionPool" -e "def __init__" -e "def connect_tcp" -e "server_hostname" -e "server_addr" -e "max_keepalive_connections" .venv/Lib/site-packages/httpcore2/_sync/connection_pool.py .venv/Lib/site-packages/httpcore2/_sync/connection.py .venv/Lib/site-packages/httpcore2/_backends/base.py .venv/Lib/site-packages/httpcore2/_backends/sync.py` | `.venv/Lib/site-packages/httpcore2/_sync/connection.py`; `HTTPConnection._connect`; `.venv/Lib/site-packages/httpcore2/_backends/sync.py`; `SyncStream.start_tls` | Source chooses request SNI override or original origin host; PoC records original `hooks.example.test` while fake dialing numeric | Original SNI preservation is offline-confirmed; real certificate/hostname verification is not | Source + offline PoC confirmed; real TLS unconfirmed | Yes |
| Real connected-peer metadata | `rg -n -F -e "def get_extra_info" -e "server_addr" .venv/Lib/site-packages/httpcore2/_backends/sync.py`; `& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py` | `.venv/Lib/site-packages/httpcore2/_backends/sync.py`; `SyncStream.get_extra_info`, `TLSinTLSStream.get_extra_info`; experimental `_OfflineStream.get_extra_info` | Installed sync streams expose peer via `server_addr`; PoC fake uses `peername` | Production must prove/normalize real metadata and fail closed; fake peer assertion is not real-backend evidence | Source confirmed; production behavior unconfirmed | Yes |
| `trust_env=False` | `rg -n -F -e "def _get_proxy_map" -e "get_environment_proxies" -e "allow_env_proxies" -e "trust_env" .venv/Lib/site-packages/httpx2/_client.py`; `& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py` | `.venv/Lib/site-packages/httpx2/_client.py`; `Client.__init__`, `_get_proxy_map` | Source gates environment proxy discovery; monkeypatched PoC proves discovery is not called with its custom transport and `trust_env=False` | Required construction prevents environment proxy discovery in the characterized path | Source + offline PoC confirmed | Yes |
| HTTP/HTTPS/SOCKS proxy handling | `rg -n -F -e "class HTTPTransport" -e "def __init__" -e "ConnectionPool(" -e "HTTPProxy(" -e "SOCKSProxy(" -e "def handle_request" .venv/Lib/site-packages/httpx2/_transports/default.py` | `.venv/Lib/site-packages/httpx2/_transports/default.py`; `HTTPTransport.__init__` | Source selects `HTTPProxy` for `http`/`https`, `SOCKSProxy` for `socks5`/`socks5h`, otherwise direct `ConnectionPool` | This proves routing branches exist, not that proxy routes preserve destination policy; production design forbids them | Source confirmed; safe proxy behavior unconfirmed | Yes |
| Pooling and keepalive | `& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py`; `rg -n -F -e "class ConnectionPool" -e "def __init__" -e "def connect_tcp" -e "server_hostname" -e "server_addr" -e "max_keepalive_connections" .venv/Lib/site-packages/httpcore2/_sync/connection_pool.py .venv/Lib/site-packages/httpcore2/_sync/connection.py .venv/Lib/site-packages/httpcore2/_backends/base.py .venv/Lib/site-packages/httpcore2/_backends/sync.py` | `.venv/Lib/site-packages/httpcore2/_sync/connection_pool.py`; `ConnectionPool.__init__`, `_assign_requests_to_connections`; experimental `_SpikeTransport` | PoC observes one lookup/dial for reused fake connection and fresh guard with `max_keepalive_connections=0`; hostname cache is not physical-connection ownership | Keepalive characterization is test-only; reconnect/expiry must bind a fresh snapshot or reuse stays disabled | Source + offline PoC confirmed; reconnect production behavior unconfirmed | Yes |

## Evidence strength matrix

| Subject | Confirmed by source inspection | Confirmed by deterministic PoC | Not confirmed with real transport | Design recommendation | Production-proven |
| --- | --- | --- | --- | --- | --- |
| Installed versions | `httpx2 2.9.1` and `httpcore2 2.9.1` | PoC ran with those installed versions | Compatibility with other versions | Re-check and compatibility-test on upgrade | No |
| Public extension point | `BaseTransport` and `ConnectionPool(network_backend=...)` are public seams | PoC composes both seams | Production adapter integration | Use these seams; do not mutate private `_pool` | No |
| DNS snapshot validation | Not established by library source | Fake boundary counts raw records, rejects record 33 without a partial snapshot, then normalizes, validates, and deduplicates before dial | Real resolver integration and answer normalization | Enforce independent raw-32 and unique-8 caps and validate every retained answer fail closed | No |
| Numeric IP dial | Injected backend owns `connect_tcp` | Fake dialer accepts only parsed numeric literals | Real numeric socket connection | Dial only an approved numeric snapshot address | No |
| No second DNS lookup | Custom backend can own connection establishment | Fake resolver is called once for the characterized connection | System resolver and reconnect behavior | Bind resolution and dial in one connection boundary | No |
| Host header | Request authority remains the original origin | Wire bytes contain the original `Host` | Real server observation | Preserve original authority | No |
| TLS SNI | Inspected path passes the original hostname as `server_hostname` | Fake stream records the original SNI | Real TLS handshake | Preserve original hostname for SNI | No |
| Certificate hostname verification | Default context enables hostname checking | PoC observes `CERT_REQUIRED` and `check_hostname=True` | Real certificate-chain and hostname validation | Verify certificates against the original hostname | No |
| Peer metadata | Real synchronous backend exposes `server_addr` | Fake exposes `peername` | Real `server_addr` shape and availability | Normalize real metadata and fail closed when unavailable | No |
| Peer validation before request bytes | Backend returns the stream before httpcore writes | Fake peer is checked before the first recorded write | Real stream and socket ordering | Validate the real peer before returning a writable stream | No |
| Fallback | Connection code permits backend-owned connection attempts | Shuffled duplicate IPv4/IPv6 answers interleave deterministically and fake fallback stays inside the approved snapshot | Real address-family and socket fallback | Round-robin sorted family buckets before the attempt cap; restrict fallback to the snapshot | No |
| Pooling and keep-alive | Pool assigns and reuses connections by origin | Fake connection reuse and zero-keepalive behavior are observed | Real expiry, remote close, and pool lifecycle | Bind snapshot ownership to each physical connection | No |
| Reconnect | Reconnection paths exist in the pool/connection code | Not confirmed | Real reconnect after close or expiry | Re-resolve for each replacement connection or disable reuse | No |
| Concurrency | Public APIs permit shared transport use | Not confirmed | Thread safety, races, and snapshot ownership | Define connection-scoped concurrent ownership | No |
| HTTP/2 | Pool exposes an HTTP/2 option | Not confirmed | ALPN and HTTP/2 connection behavior | Keep disabled until the guarded path is verified | No |
| Custom transport | Client accepts a custom `BaseTransport` | PoC executes through the custom transport | Production client lifecycle and error mapping | Use one shared guarded transport for manual and worker paths | No |
| `trust_env` | Client source gates environment proxy discovery | Discovery is not called in the tested custom-transport configuration | Independent causal effect of `trust_env=False` | Set `trust_env=False` as explicit defense in depth | No |
| HTTP proxy | Standard transport contains an HTTP proxy branch | Not confirmed | Real HTTP proxy routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |
| HTTPS proxy | Standard transport contains an HTTPS proxy branch | Not confirmed | Real HTTPS proxy routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |
| SOCKS proxy | Standard transport contains a SOCKS proxy branch | Not confirmed | Real SOCKS routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |
| Shared deadline model | Current production supplies one timeout value, not an end-to-end budget | Fake clock demonstrates resolution and fallback consuming one decreasing budget | Real resolver, socket, TLS, response, and scheduler timing | Treat `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` as one monotonic end-to-end deadline | No |
| Raw, unique, and attempt limits | No production limits exist | PoC accepts 32 duplicate-heavy records, rejects record 33, caps 8 unique addresses and 4 interleaved dials | Real transport enforcement | Keep raw 32, unique 8, and attempt 4 caps independent | No |
| Fake clock | Not applicable | Deterministically advances without sleep or system time | Timing fidelity of production operations | Use injectable monotonic time in focused tests | No |
| Just-in-time single-job claim | Current code bulk-claims up to 100 jobs; it does not implement this contract | Not confirmed | Production job ownership and transaction integration | Claim one due job immediately before execution; leave later jobs pending | No |
| `claim_generation` schema | Current jobs have no claim-generation field | Not confirmed | Migration, database constraint, overflow handling, and deployed worker compatibility | Add nonnegative monotonic `BIGINT NOT NULL DEFAULT 0`; never reset it | No |
| Last-completed handle marker | Current jobs have no completion marker | Pure model enforces pair-nullability and distinguishes marked accepted retry from unmarked recovery | Migration, atomic writes, row locks, and rolling deployment | Add the nullable pair; set it only in every accepted worker completion transaction | No |
| Increment-on-claim | Current claim changes status and `updated_at` only | Not confirmed | Atomic PostgreSQL increment under concurrent claimers | Increment exactly once in the locked `pending -> processing` transaction | No |
| Immutable claim handle | Current processing carries only job IDs after claim | Not confirmed | Application-service propagation through worker execution | Carry scalar `(job_id, delivery_cycle, claim_generation)` | No |
| Pre-request claim validation | Current execution begins from a job-ID reload and status check | Not confirmed | Real session lifecycle and recovery race | Validate the full handle immediately before DNS/HTTP without holding a network-time lock | No |
| Completion claim revalidation | Current completion performs an unlocked load and checks only `status` | Not confirmed | Real PostgreSQL completion/recovery/replay serialization | Re-lock and validate status plus the full handle before any persistence | No |
| Stale-recovery invalidation | Current recovery uses `updated_at` and has no claim identity | Not confirmed | `processing_started_at` migration, query plan, and recovery race | Clear processing timestamp, retain generation, and reject the old handle | No |
| Late worker after reclaim | Current code cannot distinguish two claims in one cycle | Not confirmed | Worker A/recovery/worker B integration with independent sessions | Reject A by generation mismatch and allow B | No |
| PostgreSQL claim concurrency | Current `SKIP LOCKED` claim serializes row selection but has no generation | Not confirmed | Two claimers plus recovery/completion row-lock tests | Require distinct generations and one consistent locked winner | No |
| Stale-completion observability | Current code raises on non-processing state and has no safe stale outcome | Not confirmed | Metrics/log integration and redaction | Emit only a redacted stale-claim log or metric and continue the batch | No |
| Residual at-least-once window | Current design already allows an HTTP effect before durable completion | Not confirmed | Recovery during a real in-flight request | Fence persistence, not remote effects; retain at-least-once semantics | No |
| Same-handle worker-rejection readback | Current runtime has no worker policy-rejection record | Not confirmed | Two completion transactions and conflict-safe PostgreSQL readback | Permit exact readback only for a matching worker rejection record | No |
| Duplicate success completion | Current runtime has no `already-completed` outcome | Not confirmed | Concurrent same-handle success completion | Create at most one attempt; return `already-completed` without replaying the HTTP outcome | No |
| Duplicate failure completion | Current runtime has no `already-completed` outcome | Not confirmed | Retryable and retry-exhausted concurrent completion | Apply one attempt/state transition; return `already-completed` without mutation | No |
| `already-completed` outcome | Current runtime raises or follows existing state paths rather than exposing this internal result | Pure model classifies a matching marker on non-processing state as `already-completed` | Locked same-handle PostgreSQL classification | Distinguish accepted prior completion from stale ownership without promising ordinary outcome readback | No |
| `stale-claim` outcome | Current runtime has no generation fence or stale outcome | Pure model classifies unmarked recovery and older generation as stale | Recovery/reclaim with real row locks | Reject invalidated handles without mutation or cross-claim readback | No |
| Delivery-attempt identity | Attempts have no claim-generation or completion identity | Not confirmed | Duplicate completion integration tests | Keep attempts unchanged; rely on the pre-insert full-handle fence | No |
| Claim-to-completion budget | Current production has separate delivery and stale timeout values | Not confirmed | DB/pool/statement timeout enforcement and scheduling overhead | One 40-second monotonic budget: 10-second delivery cap plus 30-second margin | No |
| Two-worker stale recovery | Recovery and claim are separate current transactions | Not confirmed | One long active delivery and a later eligible job remaining pending | Verify bounded ownership without claiming exactly-once guarantees | No |
| `dead_letter` terminal reason | Current status is terminal but exposes no reason field | Not confirmed | Schema, migration, serialization, and client compatibility | Add `terminal_reason` and nullable `policy_rejection_id` | No |
| Manual commit before `422` | Current route commits only after a successful service return | Not confirmed | Tagged outcome and route-owned completion commit | Commit the rejection before returning a safe `422`; propagate commit failure | No |
| `delivery_cycle` identity | Replay reuses the job and resets its attempt count | Not confirmed | Migration, row locking, idempotency, and concurrency | Add a monotonic cycle and a worker-only uniqueness boundary per job/cycle | No |
| Worker rejection identity | Current persistence has no policy-rejection record | Not confirmed | Schema, partial index, completion idempotency, and terminal projection | Use `source=worker`, record the accepted claim generation, and allow at most one rejection per `(job_id, delivery_cycle)` | No |
| Manual rejection identity | Current manual path has no durable policy-rejection identity | Not confirmed | Route-generated identity, conflict-safe persistence, metadata readback, and transaction integration | Generate one opaque ID per route invocation and accept readback only when all identity metadata matches | No |
| Source-specific partial uniqueness | Current schema has no corresponding constraints | Not confirmed | Migration and database constraint behavior | Use separate partial unique indexes for worker cycle and manual request identity | No |
| Manual conflict-safe persistence | Current schema and manual path have no corresponding insert/readback flow | Not confirmed | Real PostgreSQL conflict handling and outer-transaction usability | Use predicate-targeted `ON CONFLICT DO NOTHING RETURNING`; validate full metadata on readback and fail closed on mismatch | No |
| Manual/worker coexistence | Current runtime implements neither rejection path | Not confirmed | Concurrent persistence and transaction ordering | Permit both sources for the same job/cycle without sharing an idempotency key | No |
| Worker completion locking | Claim uses a row lock only until the claim transaction commits; current completion reloads without `FOR UPDATE` | Not confirmed | Real PostgreSQL completion/replay serialization | Re-acquire and revalidate the job row in the policy-rejection completion transaction | No |
| Manual cycle snapshot locking | Current manual path has no policy-rejection snapshot | Not confirmed | Real PostgreSQL manual/replay/worker interleavings | Resolve before locking, then lock the job and snapshot its cycle only for rejection persistence | No |
| Rejection concurrency | Current replay locks its job, but completion is unlocked and rejection concurrency is not implemented | Not confirmed | Concurrent manual requests, worker completion, and replay | Use one job-first lock order for rejection persistence and isolate source identities | No |
| `job.policy_rejection_id` integrity | Current job has no terminal-rejection pointer | Not confirmed | Composite foreign key, deferred integrity enforcement, and migration | Permit only a same-job, same-cycle, same-generation `source=worker` record as terminal pointer | No |

## Scope of proof

The PoC confirms the concept and data flow through deterministic test doubles.

The PoC does not confirm integration with the real `SyncStream` or the real
`get_extra_info("server_addr")` contract.

The PoC does not confirm real sockets, system DNS, real TLS handshakes, real certificate hostname
validation, reconnect behavior, concurrency, HTTP/2, or real proxy implementations.

The fake uses peer metadata named `peername`, while the real synchronous backend uses
`server_addr`. This confirms only the intended control flow, not production peer validation.

Source inspection confirms that TLS receives the original hostname as `server_hostname` in the
inspected path. It does not prove a successful real TLS handshake or certificate validation.

The tested custom transport prevented construction of the default environment-aware transport.
Therefore the experiment did not independently prove that `trust_env=False` caused proxy
isolation.

The recommended production boundary remains a design recommendation until the follow-up
implementation verifies the exact real integration.

The fake clock confirms only the intended shared-budget data flow, decreasing remaining budget,
and refusal to start another fake dial after exhaustion. It does not confirm timing behavior of a
real resolver, sockets, TLS, response streaming, schedulers, cancellation, or stale recovery.
Just-in-time single-job claim and the 40-second claim-to-completion budget are design
recommendations, not repository behavior or PoC/production proof. They require controlled
two-worker integration tests, including remaining-budget enforcement for delayed DB/pool work.
Future parallel or bulk ownership invalidates this derivation until it is re-evaluated.

The current repository has no `claim_generation`, no dedicated `processing_started_at`, and no
immutable claim handle. Current claim/iteration code passes only job IDs, current recovery derives
age from `updated_at`, and current completion checks status after an unlocked load. The transport
PoC does not test worker claim ownership. No PostgreSQL integration test was performed for a
claim/recovery/completion race. The selected claim-generation contract remains a design
recommendation for follow-up 1 and is not production-proven; real confirmation requires PostgreSQL
row locks and at least two independent sessions.

The current runtime also implements neither `already-completed` nor `stale-claim` as internal
completion outcomes. The offline PoC's immutable, database-free model confirms only the ordered
classification semantics: an accepted retry in `pending` with a matching marker is
`already-completed`; recovered `pending` without that marker is `stale-claim`; and a newer
generation fences an old handle. It also demonstrates that the marker contains no HTTP outcome.
No PostgreSQL integration test exercised two completion transactions carrying the same or
different handles, so locking, atomicity, worker-rejection readback, and production behavior remain
follow-up 1 recommendations. Exact readback of an ordinary HTTP or `WebhookDeliveryAttempt`
outcome is not a requirement of this spike.

The PoC fake reports its peer through `get_extra_info("peername")`. The installed synchronous
`httpcore2` `SyncStream` instead exposes the socket peer through
`get_extra_info("server_addr")`. Therefore the PoC does not prove the production metadata key or
shape. Follow-up 2 must exercise every supported real backend, normalize `server_addr`, and fail
closed before write when peer metadata is missing, malformed, or inconsistent.

Status: sufficient for a deterministic PoC and boundary recommendation, but version-specific.
PoC and source checks are required on dependency upgrade. Production still needs proof for real
backend peer metadata on supported platforms, timeouts and exception mapping, concurrent snapshot
ownership, TLS failure behavior, IPv6 zones, cancellation, and lifecycle under load.
It also needs production evidence that claim/transaction/scheduling overhead fits the selected
margin, only one job is claimed per serial iteration step, and later eligible jobs remain pending
until their turn.

## Evaluated transport approaches

| Approach | Result | Reason |
| --- | --- | --- |
| Resolve/validate, then ordinary transport | Rejected | The connection can perform a second lookup; validation and dial are unbound. |
| Rewrite request URL to a numeric IP | Rejected | Risks changing Host, SNI, certificate checks, origin identity, and diagnostics. |
| Mutate `httpx2.HTTPTransport._pool` | Rejected | `_pool` is private, not a compatibility contract. |
| Implement HTTP/TLS directly | Rejected | Duplicates protocol, timeout, pooling, and error behavior. |
| `httpx2.BaseTransport` plus `httpcore2.ConnectionPool(network_backend=...)` | Recommended | Uses public seams, retains origin identity, and moves numeric dial/peer enforcement to the connection boundary. |

Production should have a policy service create an immutable, normalized, all-address-approved
snapshot. A connection-safe transport/backend consumes exactly that snapshot, tries only approved
numeric addresses, checks the peer, and then performs TLS using the original hostname as SNI with
CA and hostname verification enabled. Only then may HTTP write the original `Host`. The PoC's
single-threaded hostname cache is test-only and is not bound to a physical connection. After a
disconnect, expiry, or reconnect it can reuse a stale snapshot while reporting
`snapshot_reused`. Production must bind one snapshot to one physical connection and resolve again
for every replacement connection, or disable keepalive/reconnection until that ownership is
proven. A hostname-level cache is not an acceptable production shortcut.

## PoC findings

Exact command:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py
```

The original spike result was `6 passed in 0.51s` (exit code 0). The corrective PoC now contains
15 offline deterministic tests; its current result is recorded by the validation report rather
than asserted here.

Confirmed through deterministic event ordering:

- mixed allowed/denied answers fail before any dial;
- resolution happens once; numeric fallback uses only the approved snapshot;
- peer approval precedes the first write;
- original Host and TLS SNI stay `hooks.example.test` while dialing a numeric address;
- the SSL context has `verify_mode=ssl.CERT_REQUIRED` and `check_hostname=True`;
- default keepalive reuses the guarded connection and skips second-request resolution;
- `max_keepalive_connections=0` causes a fresh resolve, dial, and peer guard per request;
- monkeypatched environment proxy discovery is not called with `trust_env=False`.
- a fake resolver and every fake fallback dial consume one monotonic budget, later operations see
  only the remainder, and no third dial starts after exhaustion;
- the default four-attempt cap permits exactly four failed approved dials and prevents a fifth;
- 32 duplicate-heavy raw records stay within the raw cap and deduplicate to eight unique values;
- record 33 stops resolver iteration and rejects duplicate-heavy input without validation, a
  partial snapshot, dial, or write;
- nine unique addresses exceed the default eight-address limit and fail closed before every dial;
- shuffled, duplicate mixed-family answers produce exact IPv4/IPv6 round-robin ordering before the
  four-attempt cap, resolve once, and never dial outside the approved snapshot;
- the pure completion model distinguishes accepted retry scheduling from stale recovery, fences an
  old handle after a newer claim, enforces pair nullability, and stores no HTTP outcome;
- all deadline/limit cases keep one resolver call, use only the approved snapshot, and use no
  sleep, system clock, DNS, sockets, database, ORM, real TLS, proxy, or HTTP/2.

Unconfirmed by this offline PoC:

- real DNS, sockets, OS routing, proxy implementations, and peer metadata;
- real TLS CA/hostname failures, ALPN/HTTP2, and client certificates;
- async behavior, concurrency, cancellation races, saturation, expiry, retries, and shutdown;
- PostgreSQL row locks, transactional completion atomicity, and concurrent completion/recovery;
- the production keepalive/reconnect policy. The PoC only proves reuse of its test stream; its
  hostname cache could reuse stale addresses after reconnect. Disabling keepalive gives the
  clearest per-request guard at a performance cost until per-connection ownership is proven.

The PoC disposition is retained experimental test code only, never a production import or public
service contract.

## Internal rejection signal and ownership

Introduce a neutral internal `WebhookDestinationPolicyRejected` signal, separate from
`WebhookTransportError`, `WebhookTimeoutError`, and dependency exceptions. It should be a typed,
immutable value/exception with stable safe fields such as `reason_code`, `policy_version`,
normalized scheme/hostname/port, and already-redacted address evidence. It must contain no raw
resolver exception, certificate text, proxy environment, credentials, or unrestricted internal
address data.

The destination policy/connection boundary produces the signal. The HTTP adapter must let it pass
unchanged before generic timeout/request-error normalization; it must never relabel policy denial
as a retryable transport failure. Worker and manual application services catch the typed signal
and persist it through the shared rejection service inside their own caller-owned transaction.
Persistence must not commit independently.

Worker completion converts the signal to the selected terminal job/rejection outcome. Manual
delivery uses a different, concrete transaction contract: the service returns a tagged HTTP
delivery outcome or destination-policy rejection outcome and never raises after persisting the
rejection. At the beginning of each manual route invocation, before calling the service, the route
generates one opaque `manual_delivery_request_id` and passes that same value through the service and
persistence operation. Destination-policy resolution and evaluation finish before acquiring a job
row lock, so no database lock is held across DNS or transport work. Only on a policy-rejection
completion path does the service acquire that job with `SELECT ... FOR UPDATE`, re-read its
`job_id` and `delivery_cycle` as the authoritative audit snapshot, insert and flush the manual
rejection, and retain the lock until the route-owned transaction commits. The rejection variant
contains both its `rejection_id` and `manual_delivery_request_id`. If the job or cycle cannot be
loaded consistently, the transaction fails without persisting a rejection or returning `422`.
Only after a successful commit does the route return deterministic
`422 Unprocessable Entity` with a safe body containing the opaque IDs and no target, address, or
resolver details. Commit failure rolls back or propagates as an internal failure and must never
return a false `422` that implies durable audit. The application must not automatically retry the
mutation after an ambiguous commit result. Reusing the same `manual_delivery_request_id` within the
same request-processing attempt returns the existing rejection or otherwise preserves an
idempotent result only after verifying the existing record's complete identity metadata; a new
independent HTTP request receives a new ID. This is not public
cross-request idempotency. No delivery attempt is created and no job field changes. Batch workers
do not use this route-owned commit path. Existing successful manual response status and schema
remain unchanged until follow-up 1 adds the rejection response contract.

## Durable policy-rejection decision

`dead_letter` is defined as a terminal job for which no further automatic retry is scheduled.
Retry-budget exhaustion and destination-policy rejection are distinct terminal reasons, not
synonyms. Add a nullable public `terminal_reason` enum with `retry_exhausted` and
`destination_policy_rejected`, plus nullable UUID `policy_rejection_id`, set only for the latter.
Both fields are null for non-terminal jobs. Existing dead-letter rows are backfilled and projected
as `retry_exhausted`; clients never infer the reason from `attempt_count`.

For permanent pre-HTTP worker rejection, atomically set the existing job to `dead_letter`, set
`terminal_reason=destination_policy_rejected` and its `policy_rejection_id`, and insert a separate
durable policy-rejection record in the completion transaction. Set `next_attempt_at=None`, leave
`attempt_count` unchanged, create no `WebhookDeliveryAttempt`, and continue the batch. Recovery
ignores it because it selects `processing` only.

Add nonnegative monotonic `WebhookDeliveryJob.delivery_cycle`, initially and historically
backfilled to `0`. Manual replay locks the job and increments `delivery_cycle` exactly once in the
same transaction that changes it to `pending`; automatic retries do not change it. Every durable
rejection has a required `source` enum with exactly `worker` and `manual`, a required `job_id`, a
required `delivery_cycle`, a policy identifier or policy version, a safe reason code, and
`created_at`. The cycle stored on a manual record is an audit snapshot only: it neither
terminalizes that cycle nor participates in manual uniqueness.

Add the distinct durable ownership fence `WebhookDeliveryJob.claim_generation BIGINT NOT NULL
DEFAULT 0`, backfill it to `0`, and enforce nonnegative values. It increases exactly once in every
atomic claim and is never reset by recovery, retry, replay, or terminalization. Add nullable
`processing_started_at`, backfilled from `updated_at` only for currently processing rows and null
otherwise, with a database invariant that it is non-null exactly for `status='processing'`.
Recovery queries use this dedicated timestamp for age. `updated_at` and `processing_started_at`
cannot identify ownership; only the immutable `(job_id, delivery_cycle, claim_generation)` handle
does so. A coordinated deployment is mandatory: the migration and handle-aware claim, execution,
recovery, and completion code must deploy without old workers that can set `processing` without an
increment or complete work without the full handle.

A worker rejection is the terminal result for its job cycle. The repository does not currently
hold a job lock throughout completion: the claim transaction's row lock ends at claim commit, and
the current completion path performs an unlocked load. Follow-up 1 must therefore re-acquire the
job with `SELECT ... FOR UPDATE` inside the worker policy-rejection completion transaction. After
locking and before any rejection insert or job update, it re-reads and validates `status`,
`delivery_cycle`, `claim_generation`, `terminal_reason`, and `policy_rejection_id` against the
immutable `ClaimHandle(job_id, delivery_cycle, claim_generation)` captured at claim. Replay,
worker completion, recovery, and manual rejection persistence use the same deterministic
job-first lock order.

The worker record contains `rejection_id`,
`source=worker`, `job_id`, `delivery_cycle`, required `claim_generation`, policy
identifier/version, safe reason code, and `created_at`; `manual_delivery_request_id` is null. The
generation is durable provenance for the accepted claim, not the worker uniqueness key. The
record atomically drives the job to `dead_letter`, clears `processing_started_at`, retains that
generation on the job, sets `terminal_reason=destination_policy_rejected`, and sets
`job.policy_rejection_id` to that worker record. The normative idempotency boundary remains a
PostgreSQL partial unique index, not a table `UNIQUE` constraint:

```sql
CREATE UNIQUE INDEX ... ON webhook_destination_policy_rejections (job_id, delivery_cycle)
WHERE source = 'worker';
```

SQLAlchemy must declare the equivalent `Index(..., unique=True, postgresql_where=...)`. After the
locked re-read, completion may act only when status and the complete claim handle match. A retry
of the same already accepted completion may return the existing same-cycle worker rejection only
when its `job_id`, `delivery_cycle`, and recorded `claim_generation` match the handle and the job's
valid pointer. It creates no second rejection, creates no `WebhookDeliveryAttempt`, and does not
increase `attempt_count`. A different or stale generation must not be treated as that idempotent
completion. Otherwise the new path atomically inserts the worker rejection and updates the
terminal job fields in the same transaction. The partial index remains a database backstop for a
residual insert race. Implement that race path with
`INSERT ... ON CONFLICT DO NOTHING RETURNING` against the partial-index predicate, followed by a
read and full handle validation of the existing worker rejection, or use a savepoint-equivalent
that leaves the outer transaction usable. Do not catch a raw `IntegrityError` after it has aborted
the transaction. A replay can create a new worker rejection only after it atomically advances
`delivery_cycle`.

A manual rejection is non-terminal. Its record contains `rejection_id`, `source=manual`, required
`manual_delivery_request_id`, `job_id`, the current `delivery_cycle` as an audit snapshot, policy
identifier/version, safe reason code, and `created_at`. It does not change job status,
`delivery_cycle`, `attempt_count`, or `job.policy_rejection_id`, and it cannot block a later worker
rejection for the same job and cycle. The normative manual idempotency boundary is likewise a
PostgreSQL partial unique index, not a table constraint:

```sql
CREATE UNIQUE INDEX ... ON webhook_destination_policy_rejections (manual_delivery_request_id)
WHERE source = 'manual';
```

SQLAlchemy must declare the equivalent `Index(..., unique=True, postgresql_where=...)`. One
accepted manual request can therefore create at most one rejection, while two independent
requests have distinct IDs and may create two records in the same cycle. There is no deduplication
promise between independent requests without a separate future public idempotency contract.

Manual persistence must implement re-entry and residual races without aborting the route-owned
outer transaction. Use PostgreSQL
`INSERT ... ON CONFLICT (manual_delivery_request_id) WHERE source='manual' DO NOTHING RETURNING`
targeted to the manual partial-index predicate, followed by readback by
`manual_delivery_request_id` when the insert returns no row. A savepoint-equivalent is acceptable
only if it likewise leaves the outer transaction usable. Do not catch and continue from a raw
`IntegrityError` after PostgreSQL has aborted the transaction.

Readback is an idempotent success only when the existing row has `source=manual`, exactly the same
`manual_delivery_request_id`, the same `job_id`, the same locked `delivery_cycle` audit snapshot,
the same policy identifier/version, and identical safe reason semantics (`reason_code` and every
other normalized non-sensitive reason field). Any mismatch is a
fail-closed contract violation: roll back or propagate, return no `422`, and do not retry the
mutation. The job row remains locked until the route-owned commit, and the existing rule against
automatic retry after an ambiguous commit result remains unchanged.

The migration must enforce source-specific row integrity with `NOT NULL` declarations and `CHECK`
constraints equivalent to these predicates:

- for `source=worker`, `job_id` and `delivery_cycle` are present and
  `claim_generation` is present, nonnegative, and `manual_delivery_request_id IS NULL`;
- for `source=manual`, `job_id`, the audit-snapshot `delivery_cycle`, and
  `manual_delivery_request_id` are present and `claim_generation IS NULL`;
- no other `source` value is valid.

Cross-table terminal-pointer integrity cannot be expressed by a plain row `CHECK`. The database
must give the rejection table a candidate key covering
`(rejection_id, job_id, delivery_cycle, claim_generation)` and use a composite foreign key, or an
equivalent relational constraint, from the job's
`(policy_rejection_id, job_id, delivery_cycle, claim_generation)` values. A deferrable constraint
trigger, or an equivalent database-enforced mechanism, must additionally verify at transaction
end that:

- `job.policy_rejection_id IS NULL` unless
  `terminal_reason=destination_policy_rejected`;
- a non-null pointer resolves to `source=worker`, never `source=manual`;
- the pointed record belongs to the same `job_id`, `delivery_cycle`, and `claim_generation`;
- `terminal_reason=destination_policy_rejected` is valid only with `status=dead_letter` and a
  non-null valid pointer, while all other terminal reasons and all non-terminal jobs have a null
  pointer.

Replay, recovery, and corrected worker completion serialize by acquiring the same job row first;
this is a follow-up requirement, not current completion behavior. A job already `processing` is
not replayable. The locked worker re-read either observes the exact current handle and completes
it or returns a stale-claim outcome; replay either wins before claim or waits for and observes
terminal completion. Replay after a worker rejection advances the cycle exactly once for the
future worker uniqueness boundary and leaves `claim_generation` unchanged until the next claim.

Manual policy evaluation happens without a job lock, but manual rejection persistence acquires
the same job row first and retains it through the route-owned commit. This serializes its audit
snapshot with worker completion and replay without holding a lock across DNS. If manual
persistence gets the lock first, it records the old cycle and commits before replay can increment
it. If replay gets the lock and commits first, the later manual persistence records the new cycle.
The snapshot is therefore defined by lock order, not merely by unconstrained commit timing. A
manual and worker rejection may coexist, and two distinct manual requests may both persist; manual
insertion never mutates the locked job. Replay after prior manual records preserves them, while
replay after terminal worker rejection preserves both sources' history. Attempt numbering
and `attempt_count` remain independent. The public job projection exposes `terminal_reason` and a
worker-only `policy_rejection_id`; a manual safe `422` may expose the opaque `rejection_id` and
`manual_delivery_request_id`, but never IPs, resolver messages, or target details. Manual rejection
does not change the public job status.

### Claim fencing, recovery, and concurrency

All transitions out of a valid current `processing` claim clear `processing_started_at` and retain
the last `claim_generation`: retry scheduling changes the job to `pending`, and successful or
terminal completion changes it to `succeeded` or `dead_letter`. A retained `claim_generation` is
not an indication that a job is currently processing. Current ownership always requires all of
`status='processing'`, matching `delivery_cycle`, and matching `claim_generation`.

The required concurrency outcomes are:

1. If worker A returns after recovery but before another claim, the row is `pending`; A's
   handle still matches current cycle/generation, but recovery did not write the last-completed
   marker. Step 4 classifies A as stale and rejects it without mutation.
2. If worker A returns after worker B has claimed the job, status is `processing` and the cycle may
   still match, but the generation differs; A's completion is stale and is rejected without an
   attempt or rejection.
3. Worker B may complete only when its job ID, cycle, and generation all match the locked row.
4. Recovery racing completion A serializes on the same job row lock. If completion obtains the lock
   while A's handle is current, it completes and recovery later observes a non-processing row. If
   recovery first confirms staleness and changes the row to `pending`, completion later rejects A.
   The two transactions cannot persist contradictory terminalizations.
5. Replay after a terminal worker rejection increments `delivery_cycle`, retains
   `claim_generation`, and the future claim increments the generation again.
6. Automatic retry scheduling sets `pending`, clears `processing_started_at`, keeps the cycle and
   generation, and the next claim increments `claim_generation` exactly once.
7. Two completion calls carrying the same full handle serialize on the job row lock. The first
   validates the active processing claim, writes its single attempt or policy outcome, and changes
   job state while atomically writing the marker. The second matches that marker and performs no
   mutation. For a matching worker policy rejection it may read
   back the same rejection outcome; for success, ordinary failure, retry scheduling, or retry
   exhaustion it returns `already-completed` without reconstructing the first HTTP or attempt
   outcome. A completion carrying an older or different generation returns `stale-claim` and may
   not read back the newer claim's outcome.

Pre-request validation and the completion fence prevent a stale owner from starting network work
when it is already stale and from mutating persistence after recovery. They cannot undo an HTTP
effect that occurred before recovery. If recovery wins while an already validated request is in
flight, the remote effect may exist even though the later completion is rejected as stale. The
single deadline and stale-threshold margin reduce this window but do not eliminate it. The system
retains at-least-once semantics and does not claim exactly-once delivery. A stale-claim outcome
emits only a redacted internal log or metric: no target details, resolver details, addresses, or
payload.

| Worker-state option | Audit/state effect | Decision |
| --- | --- | --- |
| Existing `dead_letter` only | Loses why no HTTP attempt exists | Reject: insufficient audit. |
| New `policy_rejected` status | Broad schema/query/API/replay/recovery/metrics change | Reject: disproportionate. |
| Fake failed attempt | Falsely claims HTTP and corrupts attempt semantics | Reject: false semantics. |
| `dead_letter` plus separate record | Durable reason while reusing terminal state | Selected. |

A migration is required.

### Selected operational stale projection

Recovery and internal stale classification use `processing_started_at`, never arbitrary
`updated_at`. Follow-up 1 adds public `oldest_processing_started_at`. For at least one release it
also preserves `oldest_processing_updated_at` as a deprecated compatibility alias. Both fields are
populated from the exact same `MIN(processing_started_at)` query result and are both `NULL` when no
processing jobs exist. Public documentation must state that the deprecated alias no longer means
the oldest arbitrary job `updated_at`. The service query, schema, and API mapping cannot calculate
the two fields independently, and their stale threshold must match recovery.

## Follow-up draft 1

### Follow-up 1 title

Persist rejection/state contracts and fence webhook worker completion

### Follow-up 1 context

Issue #57 selected a separate durable rejection record, an explicit terminal reason, and a stable
delivery-cycle identity. A pre-HTTP rejection is not a delivery attempt. Worker terminalization and
audit insertion commit atomically in worker completion; manual delivery instead returns a tagged
result with a route-generated request identity so its route can commit durable evidence before
returning `422`. Worker and manual records have distinct database-enforced identity boundaries and
may coexist for the same job and cycle. A separate monotonic claim generation fences each
individual worker ownership period, including two claims in the same delivery cycle.

### Follow-up 1 scope

- Add migration and ORM schema for durable destination-policy rejections, nonnegative
  `WebhookDeliveryJob.delivery_cycle`, nullable `terminal_reason`, and nullable
  `policy_rejection_id` with foreign-key and consistency constraints.
- Add `WebhookDeliveryJob.claim_generation BIGINT NOT NULL DEFAULT 0`, backfill existing rows to
  `0`, enforce nonnegative values, and reject a claim at `9223372036854775807` before mutation with
  an internal overflow outcome. Never reset generation during retry, recovery, replay, or
  terminalization.
- Add nullable `last_completed_delivery_cycle BIGINT` and
  `last_completed_claim_generation BIGINT`, with a check constraint requiring both to be null or
  both non-null. Backfill both to null without guessing historical completion. Legacy unmarked
  duplicates fail closed as `stale-claim`. Atomically set both values under the completion row lock
  for every accepted worker success, retryable-to-pending failure, retry-exhausted failure, and
  worker policy rejection. Never set or change them from recovery, claim acquisition, scheduling
  outside accepted completion, manual delivery/rejection, or advisory preflight.
- Add nullable `processing_started_at`; backfill current processing rows from `updated_at`, leave
  all other rows null, and enforce non-null exactly for `status='processing'`. Move stale-recovery
  filters, ordering, and supporting indexes from `updated_at` to this field. Every transition out
  of processing clears it.
- Backfill `delivery_cycle=0` and map existing `dead_letter` rows to
  `terminal_reason=retry_exhausted`; non-terminal rows keep terminal fields null.
- Store required `source` enum (`worker`/`manual`), required `job_id`, required
  `delivery_cycle`, reason, policy version, safe target snapshot/evidence, event/endpoint
  references, and `created_at`. Add nullable `manual_delivery_request_id` and nullable
  `claim_generation`: worker rows require the accepted generation; manual rows require it to be
  null.
- Add PostgreSQL partial unique indexes, not table unique constraints: worker
  `(job_id, delivery_cycle) WHERE source='worker'` and manual
  `(manual_delivery_request_id) WHERE source='manual'`. Declare both through SQLAlchemy
  `Index(..., unique=True, postgresql_where=...)` and verify the generated migration DDL.
- Add database row checks requiring a null manual ID for worker records and a non-null manual ID
  for manual records; both sources require job and cycle. Back these with source-specific tests.
- Enforce the job pointer relationally: a composite foreign key or equivalent covers
  `policy_rejection_id`, the same `job_id`, the same `delivery_cycle`, and the same
  `claim_generation`; a deferred constraint trigger or equivalent requires the target to have
  `source=worker` and requires terminal reason, pointer, status, and nullability to remain
  consistent. A manual record can never be the terminal pointer.
- Preserve all worker and manual rejection history across replay. Migration/backfill must leave
  legacy terminal rows as `retry_exhausted` with a null policy pointer.
- Add one service-owned persistence operation used inside the caller transaction.
- Add the neutral typed `WebhookDestinationPolicyRejected` signal and safe metadata contract.
- Change the internal execution/completion result to a tagged outcome: an HTTP-attempt outcome has
  its existing required attempt/attempt ID, while a policy-rejected outcome has
  `attempt_id=None` and a required `rejection_id`. Update the processing projection accordingly;
  do not synthesize an attempt.
- Replace up-front bulk claim with a just-in-time one-job claim. An iteration may complete at most
  `WEBHOOK_WORKER_PROCESSING_LIMIT` jobs, but each next due job stays `pending` until the prior
  completion commits or rolls back and it is immediately ready to execute.
- In the same locked claim transaction, revalidate claimability, check overflow, increment
  `claim_generation` exactly once, set `processing`, set `processing_started_at`, and return an
  immutable scalar `ClaimHandle(job_id, delivery_cycle, claim_generation)` before ORM detachment.
  No supported path may set processing without the increment. Worker iteration carries this full
  handle through execution and completion.
- Immediately before DNS or HTTP, have the persistence/application boundary validate
  `status='processing'` and every handle field, then release database resources before transport.
  A mismatch returns a stale-claim outcome with no DNS, HTTP, attempt, rejection, or job mutation;
  the batch continues. Do not put claim validation ownership in the transport adapter.
- Start one monotonic 40-second claim-to-completion budget at claim. Carry its remaining budget
  through preparation and completion persistence; DB, pool, and statement waits may not start
  after exhaustion. Validate stale timeout against this derived budget while retaining the current
  compatible 300-second default. Expose the remaining budget boundary for follow-up 2's nested
  transport deadline.
- Worker policy-rejection completion re-acquires the job with `SELECT ... FOR UPDATE`; the claim
  lock no longer exists after claim commit. Using the deterministic job-first lock order shared
  with replay and manual persistence, re-read `status`, `delivery_cycle`, `terminal_reason`,
  `claim_generation`, `policy_rejection_id`, and the exact
  `ClaimHandle(job_id, delivery_cycle, claim_generation)` before every insert or update. Apply the
  same fence to attempt success/failure, retry, succeeded/dead-letter, and policy-rejection paths.
  An already terminal same-cycle, same-generation worker rejection may return the existing tagged
  outcome after full source, handle, policy-version, and safe-reason validation. This exact
  same-handle readback applies only to worker policy rejection. For success, failed delivery,
  retry scheduling, retry exhaustion, and ordinary attempts, a second same-handle completion
  returns `already-completed` without another mutation and without reconstructing the original
  HTTP or attempt outcome. A different generation returns `stale-claim` and cannot read another
  claim's outcome. Otherwise,
  insert the rejection and change the locked `processing` job to `dead_letter` with
  `terminal_reason=destination_policy_rejected`, its `policy_rejection_id`,
  `next_attempt_at=None`, cleared `processing_started_at`, retained claim generation, and unchanged
  `attempt_count` in the same transaction. Any handle mismatch returns a stale-claim outcome and
  changes none of those fields or related records; it is not automatically retried and the batch
  continues.
- Implement the exact four-step locked classification specified above: current cycle/generation
  mismatch is stale before any newer-outcome read; a matching active processing handle may
  complete; a matching non-processing handle plus matching completion marker is an accepted
  duplicate; and a matching non-processing handle without that marker is recovered and stale.
  Update the marker before or atomically with the completion's attempt/rejection, state transition,
  processing timestamp clear, scheduling values, and terminal projection.
- Do not add `claim_generation`, a completion key, or a new uniqueness constraint to
  `WebhookDeliveryAttempt` in this issue. The locked full-handle validation must occur before its
  single insert. Public or exact ordinary-attempt outcome readback remains a non-goal.
- Treat the partial worker index as a residual-race backstop. Use
  `INSERT ... ON CONFLICT DO NOTHING RETURNING` followed by reading the existing rejection, or a
  savepoint-equivalent that preserves outer-transaction usability; never recover by catching a raw
  `IntegrityError` in an aborted transaction.
- Preserve the existing fatal path: non-policy errors roll back the current completion transaction
  and stop the batch rather than being converted to rejection outcomes.
- At the start of manual delivery, the route generates exactly one opaque
  `manual_delivery_request_id` and passes it to the service and persistence operation. The service
  performs destination-policy evaluation without holding a job lock. Only for a rejection does it
  acquire the job with `SELECT ... FOR UPDATE`, using the shared job-first lock order, and re-read
  the authoritative `job_id` and `delivery_cycle` snapshot. It then inserts and flushes a
  `source=manual` rejection without job mutation and returns a tagged policy outcome containing
  both `rejection_id` and the request ID; the lock remains until route-owned commit. Missing or
  inconsistent job/cycle state fails without persistence or `422`. Reuse of that ID within the
  same request-processing attempt is idempotent, while each independent request receives a new
  ID. The route returns the safe deterministic `422` only after commit. Commit failure
  rolls back/propagates and cannot return `422`; do not automatically retry after an ambiguous
  commit. This does not introduce a public cross-request idempotency contract. Batch workers do
  not use this transaction contract.
- Implement manual insert/re-entry with PostgreSQL
  `INSERT ... ON CONFLICT (manual_delivery_request_id) WHERE source='manual' DO NOTHING RETURNING`
  and read back by the request ID when no row is returned, or use a savepoint-equivalent that
  preserves the route-owned outer transaction. Never continue after a raw `IntegrityError` has
  aborted that transaction. Treat readback as idempotent only after verifying `source=manual`, the
  exact request ID, the locked `job_id`/`delivery_cycle` snapshot, policy identifier/version, and
  exact safe reason semantics. Any mismatch rolls back or propagates without `422` or mutation
  retry; retain the job lock through route commit.
- Replay locks the job, increments `delivery_cycle` exactly once in the same transaction that sets
  `pending`, resets the retry budget, clears terminal projection, preserves rejection history, and
  performs no DNS. It neither resets nor increments `claim_generation`; the next claim does.
  Automatic retry does not increment the cycle or generation while scheduling, but its next claim
  increments generation. Stale recovery locks by `processing_started_at`, changes processing to
  pending, clears that timestamp, and retains cycle, generation, and count. Define serialization for
  concurrent replay, worker completion, and manual rejection persistence under their shared
  job-first row-lock order. Historical records from both sources remain immutable; replay advances
  only the future worker uniqueness boundary. Manual-first records the old cycle before replay;
  replay-first causes manual persistence to record the new cycle. Manual policy evaluation itself
  remains outside the lock.
- Extend public response models so clients distinguish `retry_exhausted` from
  `destination_policy_rejected` directly, with `policy_rejection_id` only for the latter; do not
  require inference from `attempt_count`. The pointer may identify only the current-cycle worker
  record. A manual `422` may return opaque `rejection_id` and `manual_delivery_request_id`, while
  the public job status and terminal projection remain unchanged and no address, resolver message,
  or target detail is exposed.
- Update `src/reliable_webhook_service/operations_service.py`,
  `src/reliable_webhook_service/schemas.py`, and
  `src/reliable_webhook_service/operations_api.py`, their focused tests, and public API
  documentation. Recovery and internal stale classification use `processing_started_at`. Add
  public `oldest_processing_started_at`; keep `oldest_processing_updated_at` as a deprecated
  compatibility alias for at least one release. During that period both fields contain the same
  minimum value computed exclusively from `processing_started_at`, including both being `NULL`
  when no processing job exists. Document that the deprecated name no longer means arbitrary job
  `updated_at`.
- Treat the migration and handle-aware worker code as one coordinated deployment boundary. The
  rollout sequence is normative: stop and drain every old worker; verify that no old completion is
  in flight; apply the migration, backfill, indexes, and constraints during a maintenance window or
  use the explicitly specified staged-constraint procedure; atomically deploy the supported
  handle-aware claim, recovery, completion, worker-iteration, and operations code; only then resume
  workers. Old workers that pass only job IDs, set processing without generation increments, or
  complete without the full handle must never run with the new enforced invariants. The public API
  and manual-delivery route may remain available only if the deployment can guarantee they do not
  start, depend on, or race an old worker completion; otherwise they are paused for the same
  maintenance window. This availability rule does not change manual outcome or transaction
  semantics.
- Rolling compatibility requires a worker drain before activating the completion-marker contract:
  no job-ID-only completion may remain in flight. Schema-first deployment may expose nullable
  columns while old workers are stopped; only code that performs handle-aware claim, locked
  classification, marker write, and marker-free recovery may resume. Old rows retain null markers
  and unrecognized duplicates remain stale rather than guessed. Verify rollout with two-session
  PostgreSQL row-lock tests and an executable drain/deployment check.
- Emit a redacted log or metric for stale-claim and claim-overflow outcomes without target,
  resolver, address, or payload data.

### Follow-up 1 acceptance criteria

- Worker terminalization and exactly one rejection commit together; rollback leaves neither.
- Worker completion explicitly re-acquires the job with `SELECT ... FOR UPDATE` after the claim
  lock has ended. Before inserting or updating, it revalidates status, cycle, terminal fields, and
  the exact `job_id + delivery_cycle + claim_generation` handle under that lock.
- Initial and backfilled `claim_generation` is `0`. The first successful claim changes it to `1`,
  every later successful claim increments it exactly once, and two concurrent claimers cannot
  receive the same generation. Overflow at signed-BIGINT maximum returns a fail-closed outcome
  before mutation and does not abort the transaction. No supported service, recovery, retry, or
  replay path can set `status='processing'` without the same-transaction increment.
- Initial `processing_started_at` is backfilled from `updated_at` only for processing rows and null
  otherwise. Claim sets it; recovery, retry scheduling, success, and dead-letter completion clear
  it. Database constraints enforce its relationship to processing status, and recovery queries no
  longer use `updated_at` as claim age.
- Claim returns and worker iteration propagates immutable
  `ClaimHandle(job_id, delivery_cycle, claim_generation)`. Immediately before DNS/HTTP, persistence
  code validates all fields and status without holding a lock across network work. A stale handle
  starts no DNS or HTTP and the batch continues.
- Every completion path locks and validates the exact handle before mutation. A stale worker after
  recovery or after a new claim creates no attempt or policy rejection, changes no job/count/time
  or terminal field, is not automatically retried, and does not stop later batch jobs. The current
  worker with the matching handle can complete.
- Completion markers satisfy pair nullability. Every accepted worker completion writes its exact
  cycle/generation pair in the same locked transaction as all related persistence; recovery and
  every non-worker-completion path leave it unchanged. A marked duplicate retry completion returns
  `already-completed`, while an unmarked same-generation job recovered to pending returns
  `stale-claim`. A newer generation and a replayed cycle both fence a historical marker. The pair
  contains no HTTP outcome and ordinary attempts gain no outcome identity.
- Initial and backfilled `delivery_cycle` is `0`; replay increments it once per successful
  terminal-to-pending transition, automatic retry never increments it, and row locking prevents a
  double increment under concurrent replay.
- A PostgreSQL partial unique index on `(job_id, delivery_cycle) WHERE source='worker'` permits at
  most one terminal worker rejection per cycle. Retrying the same completion returns the
  existing/idempotent result without a duplicate only when its recorded generation matches the
  accepted handle; a second replay permits one worker rejection for the new cycle and preserves
  all earlier history.
- A PostgreSQL partial unique index on `(manual_delivery_request_id) WHERE source='manual'` permits
  one rejection for one manual request identity. Reusing the identity inside the same request
  processing does not duplicate it only when the read-back source, request ID, locked job/cycle,
  policy identifier/version, and safe reason semantics all match. A mismatch fails closed without
  `422`; two independent manual requests in one cycle receive different IDs and create two
  records.
- A manual rejection and worker rejection for the same `job_id` and `delivery_cycle` coexist.
  The manual record does not block worker terminalization and does not set
  `job.policy_rejection_id`.
- Database constraints reject invalid source-specific nullability. `job.policy_rejection_id`
  resolves only to a `source=worker` record with the same job, cycle, and claim generation, is
  required exactly for `destination_policy_rejected` on `dead_letter`, and is null otherwise.
  Worker records require generation and manual records require it to be null. These guarantees
  are database-enforced rather than application-only.
- No attempt is created and `attempt_count` is unchanged.
- Tagged execution and processing results expose `rejection_id` without an `attempt_id`; existing
  HTTP-attempt outcomes retain their current attempt contracts.
- Rejected jobs are not recovered and later eligible jobs continue; fatal non-policy errors still
  roll back and stop processing.
- At most one job is claimed `processing` immediately before execution; later jobs remain
  `pending`. The next claim starts only after the current completion transaction ends.
- The claim-to-completion budget is 40 seconds for current defaults. Preparation, DB/pool waits,
  statements, delivery, and completion use remaining budget only; operations do not begin after
  exhaustion, and startup rejects stale timeout below the derived budget.
- In controlled two-worker cases, an active bounded job is not stale-recovered and a later/last
  eligible job remains pending until its turn. Neither case performs parallel HTTP or duplicate
  delivery, without claiming exactly-once behavior after an HTTP-side-effect/process-crash window.
- `dead_letter` means no further automatic retry. Public projections distinguish
  `retry_exhausted` and `destination_policy_rejected`; policy rejection has its rejection ID and
  does not look like exhausted retry budget. Existing dead-letter rows serialize as
  `retry_exhausted`, and non-terminal rows expose neither terminal field.
- Manual service returns a tagged rejection result containing the persisted rejection and manual
  request IDs; rejection is flushed and committed by the route before the stable redacted `422`.
  Commit failure rolls back/propagates without `422`, and an ambiguous commit is not automatically
  retried. No attempt, job mutation, terminal pointer, or raw resolver/address/target detail occurs.
- Manual policy evaluation holds no job lock. On rejection, persistence locks and re-reads the job
  before snapshotting its cycle, keeps the lock through route commit, and fails without persistence
  or `422` if consistent state cannot be loaded. In controlled interleavings, manual-first records
  the old cycle before replay increments it, while replay-first makes manual record the new cycle.
- Concurrent or re-entered persistence with the same manual identity produces exactly one row and
  consistent idempotent outcomes only when all identity metadata matches. The conflict-safe path
  leaves both route-owned outer transactions usable. Metadata mismatch rolls back/propagates with
  no `422`, and does not retry the mutation.
- Replay performs no DNS while holding the row lock, retains history, clears terminal projection,
  retains `claim_generation`, and leaves authoritative revalidation to worker completion.
  Concurrent replay and worker
  completion serialize on the job row after worker completion re-acquires it; a processing job
  cannot be replayed. Replay after terminal worker rejection advances the next cycle, while replay
  after manual records preserves them and does not reinterpret their cycle snapshots.
- Stale recovery locks a processing row, confirms `processing_started_at` crosses the threshold,
  sets pending, clears the timestamp, and retains cycle, generation, and attempt count. A worker A
  completion after recovery is rejected because the row is non-processing without A's marker;
  after worker B's new claim it is rejected first by generation. Recovery racing A's completion
  serializes on the row lock and cannot create two contradictory terminal results.
- Worker rejection records persist the accepted generation. The terminal pointer resolves to the
  same worker record/job/cycle/generation, while manual records have null generation. Automatic
  retry and replay do not change generation before a claim; every future claim increments it.
- Stale-claim and overflow outcomes emit only redacted observability. Claim fencing prevents stale
  persistence but does not undo an HTTP side effect that occurred before recovery; the documented
  deadline/stale margin mitigates this residual at-least-once window without promising exactly
  once.
- Migration and worker deployment are coordinated so an old job-ID-only worker cannot coexist with
  the new handle-aware claim and completion paths.
- Operational stale projection is computed from `processing_started_at` only. The new
  `oldest_processing_started_at` and deprecated `oldest_processing_updated_at` alias are equal for
  at least one compatibility release, including equal `NULL` behavior, and use the same threshold
  semantics as recovery.
- Two concurrent worker policy-rejection completions carrying the same immutable
  `ClaimHandle(job_id, delivery_cycle, claim_generation)` create exactly one worker row and both
  return the same rejection ID and idempotent rejection outcome. Both outer transactions remain usable; neither creates an
  attempt nor changes `attempt_count`. A completion with the same job/cycle but a different
  `claim_generation` is stale: it must not reuse or read the current generation's worker rejection
  as its own successful idempotent outcome, and it returns the stale-claim outcome with no attempt,
  rejection, or job mutation. The residual same-handle conflict path uses conflict-safe insert/read
  or a savepoint, never a raw aborted-transaction `IntegrityError` path.
- Two same-handle success completions create at most one attempt; the second returns
  `already-completed` without mutation and without reproducing the original HTTP outcome. The same
  rule applies separately to retryable transport failure and retry-exhausted failure: at most one
  attempt and one retry or terminal transition are persisted, and the second completion returns
  `already-completed`. Any completion from an older or different generation returns `stale-claim`
  and cannot read back an outcome belonging to the newer claim.

### Follow-up 1 tests

The following are mandatory real PostgreSQL tests using two independent sessions where locking or
concurrency is involved:

1. **Accepted retry scheduling:** claim cycle `0`, generation `7`; accept retryable completion;
   assert `pending`, marker `(0, 7)`, and exactly one attempt. Duplicate the same handle and assert
   `already-completed`, the same job values, one attempt, and no additional mutation.
2. **Recovery before completion:** claim cycle `0`, generation `7`; recovery sets `pending` without
   a matching marker. Late completion returns `stale-claim`, creates zero attempts, and performs no
   additional mutation.
3. **Newer-claim fencing:** after accepted completion `(0, 7)`, the next claim increments generation
   to `8`. Completion `(0, 7)` is stale, performs no mutation, and must not read back generation
   `8`'s outcome.
4. **Replay fencing:** retain a marker from cycle `0`, replay to cycle `1`, and assert old-cycle
   completion is stale with no mutation.
5. **Worker policy rejection:** after accepted rejection, the marker matches the handle; an exact
   same-handle duplicate returns the same rejection record and creates no new rejection row.

Add operations tests for the service query, public schema, and API mapping; assert both stale fields
are identical, both are `NULL` without processing jobs, and their `processing_started_at` threshold
matches recovery. Migration tests cover both nullable columns, pair-nullability DDL, null backfill,
and fail-closed behavior for legacy unmarked rows. Two-session tests cover completion/recovery lock
ordering and atomic marker visibility. Rollout tests cover worker drain and schema/code rolling
compatibility.

Migration/backfill and database-enforced source/nullability, PostgreSQL partial-unique-index DDL,
composite-reference, and terminal-pointer constraint tests; initial cycle and exactly-once replay
increment; initial/backfilled `claim_generation=0`; first claim producing generation `1`; every
later claim incrementing exactly once; two PostgreSQL claimers receiving distinct generations;
atomic increment under real PostgreSQL concurrency; signed-BIGINT overflow failing before mutation
without aborting the transaction; `processing_started_at` backfill, status constraint, index, and
recovery-query migration tests; automatic retry changing neither cycle nor generation before its
next claim; stale recovery retaining generation and clearing the timestamp; replay retaining
generation; worker completion lock/exact-handle validation; at most one worker rejection per cycle;
worker rejection storing the current generation; idempotent same-handle worker-rejection retry; second replay
allowing a new worker rejection and the next claim receiving a new generation; history preservation
for both sources; two concurrent worker-policy-rejection completion calls carrying the same
immutable handle producing one rejection row and the same rejection ID while both outer
transactions remain usable, with no attempt/count change; duplicate same-handle success producing
one attempt and `already-completed` on the second call; duplicate same-handle retryable failure
producing one attempt, one `attempt_count` increment and one retry transition, with
`already-completed` on the second call; duplicate same-handle retry-exhausted failure producing one
attempt and one terminalization, with `already-completed` on the second call; a generation-`1`
completion arriving after generation `2` exists for the same job/cycle
returning stale with no mutation and without reusing or returning generation `2`'s rejection; real
PostgreSQL worker completion/replay/recovery row-lock serialization;
controlled manual-first/replay-first cycle snapshot interleavings; rejection failure on
missing/inconsistent locked job state; a concurrent manual and worker rejection coexisting for the
same cycle; two manual requests creating two records; reuse of the same manual identity creating no
duplicate and returning the same consistent outcome when metadata matches; concurrent persistence
with the same manual identity producing one row while both outer transactions remain usable;
mismatched job, cycle, policy, or safe-reason readback failing closed without `422`; manual
rejection not setting `job.policy_rejection_id`; the job pointer resolving to its current-cycle
worker record; tagged execution/processing projections;
worker PostgreSQL commit/rollback/continuation and fatal-error tests; both public terminal reasons
and legacy dead-letter serialization; manual route identity generation, tagged result,
persistence/flush, commit-before-`422`, rollback and no-`422` on commit failure, no attempt,
unchanged job/status/cycle/count, and raw-resolver/address/target-detail redaction. Add
fake-monotonic-budget tests for delayed DB/pool/statement work consuming the remaining 40-second
claim budget, refusal to start work after exhaustion, one-at-a-time claim ordering, and two-worker
controlled regressions for one active long delivery and a later/last eligible job remaining pending
without parallel HTTP or duplicates. Add the exact worker A generation `1` -> recovery -> worker B
generation `2` -> late A completion scenario: A cannot mutate the job, create an attempt, or create
a rejection; B can complete; the batch continues after A's stale outcome. Add the variant where A
returns after recovery but before B's claim, and a recovery-versus-completion race using real
PostgreSQL row locks. Verify all attempt success/failure, retry, terminal, and policy-rejection
paths share the same fence; `job.policy_rejection_id` resolves to the worker record with the same
job, cycle, and generation; stale outcomes are not automatically retried and observability is
redacted. Verify coordinated-deployment guards reject or prevent legacy job-ID-only worker
operation. Add a rollout/operations test or executable deployment check proving workers are drained
before constraint enforcement and handle-aware workers are the only workers resumed; document the
expected API/manual-delivery maintenance behavior for the chosen migration procedure.

### Follow-up 1 documentation

Update database, delivery execution, manual API, operations public API/schema compatibility,
architecture, and changelog documentation.

### Follow-up 1 non-goals

No resolver/transport, new job status, fake attempt, endpoint preflight, proxy support, parallel
worker execution, or exactly-once guarantee.

### Follow-up 1 validation commands

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/test_migrations.py tests/test_delivery_job_execution_service.py tests/test_delivery_processing_service.py tests/test_worker_iteration_service.py tests/test_worker_iteration_service_integration.py tests/test_delivery_service_transaction_integration.py tests/test_manual_delivery_api.py tests/test_delivery_job_recovery_service.py tests/test_replay_service.py tests/test_operations_service.py tests/test_operations_service_integration.py tests/test_operations_api.py tests/test_operations_api_integration.py
& ".\.venv\Scripts\python.exe" -m ruff check migrations src tests
& ".\.venv\Scripts\python.exe" -m mypy src
```

### Follow-up 1 dependencies

Depends on issue #57's decision. Blocks follow-up 2: the transport work consumes this issue's
just-in-time ownership and remaining claim-budget boundary.

## Follow-up draft 2

### Follow-up 2 title

Enforce webhook destination policy at the DNS-to-connection boundary

### Follow-up 2 context

Issue #57 proved validation must bind an all-address-approved resolver snapshot to numeric dialing
and peer inspection while retaining Host, SNI, and certificate verification.

### Follow-up 2 scope

- Implement shared URL/address policy and stable rejection reasons.
- Enforce the normative special-use table above for IPv4, IPv6, literal hosts, every DNS answer,
  and embedded IPv4 in IPv4-mapped IPv6. Do not substitute an untested `is_global` check.
- Reject URL userinfo and port `0`; allow ports `1..65535`, including non-default ports, under the
  same destination, deadline, proxy, and connection rules.
- Reject before resolution or dial every IPv6 zone identifier (including bracketed `%zone` and
  percent-encoded scope syntax such as `%25`), alternate integer/octal/hexadecimal/mixed-base or
  shortened-dotted IPv4 form, and URL host with ambiguous parser normalization. Require one
  canonical numeric representation, including explicit handling of IPv4-mapped, uppercase, and
  non-canonical IPv6, so URL parser, policy classifier, and numeric dialer cannot disagree.
- Implement `httpx2.BaseTransport` backed by
  `httpcore2.ConnectionPool(network_backend=...)`; never use `HTTPTransport._pool`.
- Resolve once per new connection, validate all normalized answers, dial snapshot addresses only,
  restrict fallback, and verify peer before write.
- Count raw resolver records before normalization and reject the whole result on record 33,
  including duplicate or invalid records, without continued iteration, partial snapshot, dial, or
  write. For at most 32 raw records, normalize and validate, deduplicate, cap the approved snapshot
  at 8 unique addresses, split and packed-byte-sort IPv4/IPv6 buckets, interleave one each in fixed
  IPv4-then-IPv6 rounds, and only then cap connection attempts at 4. Keep the raw, unique, and
  attempt caps independent.
- Treat the existing 10-second `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` default as one monotonic budget
  across resolution, validation, every dial, TLS, request, and response. Pass only remaining time
  and start no operation at zero.
- Consume follow-up 1's just-in-time one-job ownership and remaining claim-to-completion budget.
  Execution receives the approved immutable
  `ClaimHandle(job_id, delivery_cycle, claim_generation)`. The persistence/application service,
  not the transport adapter, validates that handle immediately before DNS/HTTP and releases its
  database resources before network work. The transport receives claim context only for bounded
  execution/observability; it does not own job-state validation or persistence. It gets the lesser
  of its 10-second deadline and the remaining 40-second claim budget and starts no resolution or
  connection work after exhaustion. Follow-up 2 does not restore bulk claim or own job-state
  transactions.
- Bind the snapshot to the physical connection, never a hostname cache. Until reconnect/expiry
  ownership is proven, configure `max_keepalive_connections=0` and do not retry transparently.
- Read and normalize the real synchronous backend's `server_addr`; fail closed before write if
  peer metadata is absent, malformed, denied, or outside the snapshot.
- Preserve authority, SNI, CA/hostname verification, timeouts, normalized errors, and
  `follow_redirects=False`.
- Emit `WebhookDestinationPolicyRejected` with safe stable metadata and ensure the HTTP adapter
  bypasses generic transport normalization for that signal.
- Construct client/transport with `trust_env=False` and no implicit proxy route.
- Integrate follow-up 1's rejection transaction behavior.
- Document and test an explicit keepalive/snapshot-lifetime policy.
- Because production will import `httpcore2` directly, select and lock one exact compatible
  `httpx2`/`httpcore2` pair, initially `2.9.1`/`2.9.1`, and test that exact pair. Do not claim
  compatibility from SemVer ranges alone. Private APIs remain forbidden; upgrading either package
  independently requires the real transport integration suite against the resulting exact pair.

### Follow-up 2 acceptance criteria

- Denied/mixed answers reject durably before connection; no second lookup occurs.
- Raw record 33 rejects even when all records duplicate one allowed address. At most 32 records are
  deduplicated before the unique limit; overflow rejects without dial. Connect attempts never
  exceed four and use deterministic family interleaving, so both families occupy the first two
  positions when both exist and neither family can starve the other.
- Resolution, validation, all fallback, TLS, request, and response share one monotonic deadline;
  remaining budget decreases, and no dial starts after exhaustion. Errors are stable and redact
  resolved IPs and resolver detail.
- With two workers, a long-running delivery that remains within the 40-second claim budget is not
  recovered while active. A later/last eligible job remains `pending` until its turn, then enters
  HTTP under a fresh claim budget. These controlled cases produce neither parallel HTTP nor a
  duplicate delivery; they do not remove the existing at-least-once crash window.
- With worker A at generation `1`, recovery, and worker B at generation `2`, late A cannot mutate
  the job or terminalize it. If A is already stale before pre-request validation, it performs no
  DNS or HTTP. If A's HTTP occurred before recovery, the remote effect remains possible but stale
  completion is still rejected; this is explicitly at-least-once behavior.
- Every dial is numeric and in snapshot; fallback cannot escape it.
- Denied/mismatched peer fails before write.
- Reconnect or expiry cannot reuse a hostname-level snapshot; each physical connection has one
  freshly resolved snapshot, or connection reuse is disabled.
- Host, SNI, certificate verification, timeout behavior, and redirect prohibition remain.
- Environment proxies cannot bypass enforcement.
- Policy signals retain their type and safe metadata through the adapter, are never normalized as
  transport errors, and contain no sensitive exception/address detail.
- Manual/worker paths share policy but retain distinct state mutation.
- The exact compatible `httpx2`/`httpcore2` pair is directly declared and tested. Any one-package
  upgrade reruns public-seam, real connection, TLS, proxy, and `server_addr` integration tests;
  production uses no private API.

### Follow-up 2 tests

Add table-driven fail-closed URL-host and numeric-normalization tests for every normative IPv4/IPv6
class, IPv6 zone identifiers, bracketed IPv6 with `%zone`, percent-encoded scope syntax including
`%25`, IPv4 integer/octal/hexadecimal forms, mixed-base IPv4 components, shortened dotted IPv4,
IPv4-mapped IPv6, uppercase and non-canonical IPv6, ambiguous URL-parser normalization, literal
hosts, userinfo, port `0`, and non-default ports. Assert rejection before resolution/dial/write and
that parser, classifier, and dialer cannot disagree. Add deterministic fake-clock tests for one
deadline across resolution and every fallback, exact 32-record duplicate-heavy acceptance,
duplicate-heavy record-33 rejection with zero dial/write and no partial snapshot, the 8-unique and
4-attempt caps, mixed-family shuffled/duplicate exact ordering and starvation prevention, no dial
after exhaustion, deterministic snapshot-only fallback, peer mismatch, TLS identity,
per-connection snapshot ownership, timeout/error mapping, typed-signal bypass, and proxy isolation.
Add controlled real-transport tests for `server_addr`, peer-before-first-write, real TLS hostname
verification, reconnect/expiry, HTTP/2 disabled behavior, HTTP/HTTPS/SOCKS rejection, and
`trust_env=False`; use no public internet. Verify delayed preparation leaves the transport only the
remaining claim budget. Add two controlled two-worker PostgreSQL regressions: one long-running
active delivery within 40 seconds, and one later/last eligible job that remains pending behind
prior deliveries before receiving a fresh just-in-time claim and entering HTTP. Prove the active
job is not prematurely recovered and neither controlled case runs HTTP concurrently or delivers
twice, without asserting exactly-once across process failure. Test the exact dependency pair.
Add the claim-fencing transport regression with worker A generation `1`, recovery, worker B
generation `2`, and late A: persistence-owned pre-request validation prevents stale A from
starting DNS/HTTP when observed in time; completion validation prevents every late A job mutation,
attempt, rejection, or terminalization; B remains able to complete. Include the explicit variant
where A's HTTP happened before recovery and assert only stale completion rejection, not reversal of
the remote effect or exactly-once behavior.

### Follow-up 2 documentation

Update architecture, security limitations, delivery execution, dependency evidence, and operations;
record keepalive and proxy decisions.

### Follow-up 2 non-goals

No endpoint preflight, redirects, arbitrary proxies, protocol rewrite, or private internals.

### Follow-up 2 validation commands

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py tests/test_delivery_http.py tests/test_delivery_processing_service.py
& ".\.venv\Scripts\python.exe" -c "import httpx2,httpcore2; assert (httpx2.__version__,httpcore2.__version__)==('2.9.1','2.9.1')"
& ".\.venv\Scripts\python.exe" -m ruff check src tests
& ".\.venv\Scripts\python.exe" -m mypy src
```

### Follow-up 2 dependencies

Depends on follow-up 1's tagged outcomes, rejection persistence, immutable claim handle,
persistence-owned pre-request/completion validation, just-in-time one-job claim, and remaining
claim-budget boundary. It must declare and verify an exact compatible pair for both packages and
lock public-seam and real-transport evidence into tests. The transport adapter must not own or
mutate `claim_generation`. Blocks follow-up 3.

Follow-up 1 must be deployed before follow-up 2: the transport worker needs executable locked
completion fencing, including the durable marker distinction between accepted completion and stale
recovery, before it begins a real outbound request.

## Follow-up draft 3

### Follow-up 3 title

Add webhook endpoint destination-policy preflight with safe API errors

### Follow-up 3 context

Connection-time enforcement stays authoritative, but endpoint creation should reject obviously
invalid/currently forbidden destinations early without exposing network detail.

### Follow-up 3 scope

- Reuse follow-up 2's exact parser/address policy during endpoint creation.
- Perform advisory current DNS preflight without dialing. Delivery-time enforcement remains the
  only authoritative decision and always re-resolves through the guarded boundary.
- Return stable safe errors without internal IPs, resolver detail, or exception text.
- Keep connection-time re-resolution/enforcement; successful preflight is not authorization cache.
- A rejected creation persists no endpoint and no destination-policy rejection record because no
  endpoint, event, or job exists. Do not create an artificial durable domain entity solely for
  audit. Optional redacted log/metric telemetry is not durable domain audit history.
- Define resolver unavailability as a safe, retryable creation failure with deterministic status
  and response; do not treat unavailable DNS as authorization or silently accept it.

### Follow-up 3 acceptance criteria

- Invalid scheme, credentials, port, empty/malformed answers, and any denied answer reject.
- Errors are stable and redact address/resolver data.
- Passing preflight never bypasses connection-bound policy or pins stale DNS.
- Rejected preflight creates no endpoint or durable rejection record. Optional telemetry contains
  no target IP, credentials, resolver exception, or internal details.
- Existing successful endpoint response remains compatible.
- Resolver failure has safe deterministic classification and explicit retry guidance; delivery-time
  enforcement remains authoritative after every successful creation.

### Follow-up 3 tests

Offline resolver test doubles for endpoint service/API allowed, denied, mixed, malformed, empty,
and unavailable answers; userinfo/port cases; redaction and no-persistence assertions; regression
proving delivery still performs authoritative enforcement. No real network is used.

### Follow-up 3 documentation

Update endpoint API, architecture, operations, and changelog with the preflight/authoritative-check
distinction and safe error contract.

### Follow-up 3 non-goals

No replacement for connection enforcement, creation-time HTTP/reachability probe, public DNS
guarantee, redirect, or proxy support.

### Follow-up 3 validation commands

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/test_webhook_endpoint.py tests/test_webhook_endpoint_api.py tests/test_delivery_http.py
& ".\.venv\Scripts\python.exe" -m ruff check src tests
& ".\.venv\Scripts\python.exe" -m mypy src
```

### Follow-up 3 dependencies

Depends on follow-up 2's shared policy/taxonomy and guarded delivery boundary. It does not depend
on follow-up 1 persistence because creation rejections are deliberately not durable domain records.
