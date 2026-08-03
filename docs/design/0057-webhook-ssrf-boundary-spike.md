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
| Manual | The API transaction performs HTTP, records a completed attempt, and commits | Validate before HTTP. Persist a durable rejection in the manual transaction, create no attempt, and do not mutate a job. |
| Worker | Claim transaction changes `pending -> processing`; one completion transaction per job performs HTTP, creates an attempt, and chooses `succeeded`, `pending`, or `dead_letter` | Validate in completion before HTTP. Atomically insert a rejection and set the existing job to `dead_letter`; continue the batch. |
| Recovery | A separate transaction changes stale `processing -> pending` and sets `next_attempt_at=recovered_at` | Ignore terminal rejected jobs. An interrupted pre-commit validation remains `processing` and follows normal recovery. |
| Replay | A terminal job is locked and reset to `pending`, `attempt_count=0`, and a new due time | Enqueue under the existing row lock without DNS. Worker completion performs the authoritative current-policy check and may terminalize another rejection. Preserve all rejection history. |

The important recovery sequence stays `processing -> stale recovery -> pending -> processing`.
Validation and terminalization belong to completion, not claim: a crash before completion commit is
recoverable. Earlier per-job commits remain committed. A permanent rejection is a normal per-job
outcome and must not stop later claimed jobs.

## Invariants

- Resolve once for a connection and validate every returned address, fail closed.
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
| DNS snapshot validation | Not established by library source | Entire fake answer set is validated before dial | Real resolver integration and answer normalization | Validate every answer and fail closed | No |
| Numeric IP dial | Injected backend owns `connect_tcp` | Fake dialer accepts only parsed numeric literals | Real numeric socket connection | Dial only an approved numeric snapshot address | No |
| No second DNS lookup | Custom backend can own connection establishment | Fake resolver is called once for the characterized connection | System resolver and reconnect behavior | Bind resolution and dial in one connection boundary | No |
| Host header | Request authority remains the original origin | Wire bytes contain the original `Host` | Real server observation | Preserve original authority | No |
| TLS SNI | Inspected path passes the original hostname as `server_hostname` | Fake stream records the original SNI | Real TLS handshake | Preserve original hostname for SNI | No |
| Certificate hostname verification | Default context enables hostname checking | PoC observes `CERT_REQUIRED` and `check_hostname=True` | Real certificate-chain and hostname validation | Verify certificates against the original hostname | No |
| Peer metadata | Real synchronous backend exposes `server_addr` | Fake exposes `peername` | Real `server_addr` shape and availability | Normalize real metadata and fail closed when unavailable | No |
| Peer validation before request bytes | Backend returns the stream before httpcore writes | Fake peer is checked before the first recorded write | Real stream and socket ordering | Validate the real peer before returning a writable stream | No |
| Fallback | Connection code permits backend-owned connection attempts | Fake fallback stays inside the approved snapshot | Real address-family and socket fallback | Restrict fallback to the immutable approved snapshot | No |
| Pooling and keep-alive | Pool assigns and reuses connections by origin | Fake connection reuse and zero-keepalive behavior are observed | Real expiry, remote close, and pool lifecycle | Bind snapshot ownership to each physical connection | No |
| Reconnect | Reconnection paths exist in the pool/connection code | Not confirmed | Real reconnect after close or expiry | Re-resolve for each replacement connection or disable reuse | No |
| Concurrency | Public APIs permit shared transport use | Not confirmed | Thread safety, races, and snapshot ownership | Define connection-scoped concurrent ownership | No |
| HTTP/2 | Pool exposes an HTTP/2 option | Not confirmed | ALPN and HTTP/2 connection behavior | Keep disabled until the guarded path is verified | No |
| Custom transport | Client accepts a custom `BaseTransport` | PoC executes through the custom transport | Production client lifecycle and error mapping | Use one shared guarded transport for manual and worker paths | No |
| `trust_env` | Client source gates environment proxy discovery | Discovery is not called in the tested custom-transport configuration | Independent causal effect of `trust_env=False` | Set `trust_env=False` as explicit defense in depth | No |
| HTTP proxy | Standard transport contains an HTTP proxy branch | Not confirmed | Real HTTP proxy routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |
| HTTPS proxy | Standard transport contains an HTTPS proxy branch | Not confirmed | Real HTTPS proxy routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |
| SOCKS proxy | Standard transport contains a SOCKS proxy branch | Not confirmed | Real SOCKS routing and policy enforcement | Do not support until an equivalent guarded boundary is proven | No |

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

The PoC fake reports its peer through `get_extra_info("peername")`. The installed synchronous
`httpcore2` `SyncStream` instead exposes the socket peer through
`get_extra_info("server_addr")`. Therefore the PoC does not prove the production metadata key or
shape. Follow-up 2 must exercise every supported real backend, normalize `server_addr`, and fail
closed before write when peer metadata is missing, malformed, or inconsistent.

Status: sufficient for a deterministic PoC and boundary recommendation, but version-specific.
PoC and source checks are required on dependency upgrade. Production still needs proof for real
backend peer metadata on supported platforms, timeouts and exception mapping, concurrent snapshot
ownership, TLS failure behavior, IPv6 zones, cancellation, and lifecycle under load.

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

Result on 2026-08-02: `6 passed in 0.51s` (exit code 0).

Confirmed through deterministic event ordering:

- mixed allowed/denied answers fail before any dial;
- resolution happens once; numeric fallback uses only the approved snapshot;
- peer approval precedes the first write;
- original Host and TLS SNI stay `hooks.example.test` while dialing a numeric address;
- the SSL context has `verify_mode=ssl.CERT_REQUIRED` and `check_hostname=True`;
- default keepalive reuses the guarded connection and skips second-request resolution;
- `max_keepalive_connections=0` causes a fresh resolve, dial, and peer guard per request;
- monkeypatched environment proxy discovery is not called with `trust_env=False`.

Unconfirmed by this offline PoC:

- real DNS, sockets, OS routing, proxy implementations, and peer metadata;
- real TLS CA/hostname failures, ALPN/HTTP2, and client certificates;
- async behavior, concurrency, cancellation races, saturation, expiry, retries, and shutdown;
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

Worker completion converts the signal to the selected terminal job/rejection outcome. The manual
service persists a rejection without job mutation, and the manual route returns deterministic
`422 Unprocessable Entity` with the safe body
`{"detail":"Webhook destination is not permitted"}`. Existing successful manual response status
and schema remain unchanged. Tests must cover catch ordering, no generic normalization, atomic
commit/rollback, stable safe response, and absence of raw address/resolver/exception leakage.

## Durable policy-rejection decision

For permanent pre-HTTP worker rejection, atomically set the existing job to `dead_letter` and
insert a separate durable policy-rejection record in the completion transaction. Set
`next_attempt_at=None`, leave `attempt_count` unchanged, create no `WebhookDeliveryAttempt`, and
continue the batch. Recovery ignores it because it selects `processing` only. Replay is allowed
to enqueue without DNS while holding the existing job row lock; it resets the normal retry budget
and preserves all rejection history. Worker completion then performs authoritative current-policy
validation and may atomically dead-letter the job with another durable rejection record.

Manual rejection inserts the durable record without job mutation. The record should distinguish
manual/worker source and hold stable references, target snapshot, policy version, normalized
reason, non-sensitive address evidence, and timestamp. Exact fields, constraints, retention, and
exposure belong to the migration follow-up.

| Worker-state option | Audit/state effect | Decision |
| --- | --- | --- |
| Existing `dead_letter` only | Loses why no HTTP attempt exists | Reject: insufficient audit. |
| New `policy_rejected` status | Broad schema/query/API/replay/recovery/metrics change | Reject: disproportionate. |
| Fake failed attempt | Falsely claims HTTP and corrupts attempt semantics | Reject: false semantics. |
| `dead_letter` plus separate record | Durable reason while reusing terminal state | Selected. |

A migration is required.

## Follow-up draft 1

### Follow-up 1 title

Persist webhook destination-policy rejections and terminalize worker jobs

### Follow-up 1 context

Issue #57 selected a separate durable rejection record. A pre-HTTP rejection is not a delivery
attempt. Worker terminalization and audit insertion must commit atomically; manual rejection must
preserve the current job. Today `WebhookDeliveryJobExecutionResult.attempt` is required and
`WebhookDeliveryProcessingJobResult.attempt_id` is a required UUID, so the internal completion and
processing projections cannot represent a rejection without inventing an attempt.

### Follow-up 1 scope

- Add migration and ORM schema for durable destination-policy rejections.
- Store stable source (`worker`/`manual`), reason, policy version, target snapshot, safe address
  evidence, event/endpoint references, optional job reference, and timestamp with constraints.
- Add one service-owned persistence operation used inside the caller transaction.
- Add the neutral typed `WebhookDestinationPolicyRejected` signal and safe metadata contract.
- Change the internal execution/completion result to a tagged outcome: an HTTP-attempt outcome has
  its existing required attempt/attempt ID, while a policy-rejected outcome has
  `attempt_id=None` and a required `rejection_id`. Update the processing projection accordingly;
  do not synthesize an attempt.
- Worker completion inserts rejection and changes locked `processing` job to `dead_letter` with
  `next_attempt_at=None` and unchanged `attempt_count`, returns the tagged rejection outcome, and
  lets the processing cycle continue the batch.
- Preserve the existing fatal path: non-policy errors roll back the current completion transaction
  and stop the batch rather than being converted to rejection outcomes.
- Manual delivery catches the typed signal, inserts rejection without job mutation, and exposes
  the safe deterministic policy-error response while leaving its success response unchanged.
- Replay enqueues under its existing row lock without DNS, preserves rejection history, and leaves
  authoritative validation to worker completion; recovery still selects only `processing`.

### Follow-up 1 acceptance criteria

- Worker terminalization and exactly one rejection commit together; rollback leaves neither.
- Idempotency is enforced under a documented uniqueness rule.
- No attempt is created and `attempt_count` is unchanged.
- Tagged execution and processing results expose `rejection_id` without an `attempt_id`; existing
  HTTP-attempt outcomes retain their current attempt contracts.
- Rejected jobs are not recovered and later claimed jobs continue; fatal non-policy errors still
  roll back and stop processing.
- Manual rejection records evidence without job mutation and returns the stable redacted `422`
  response; its successful response is unchanged.
- Replay performs no DNS while holding the row lock, retains history, and a repeated worker denial
  produces a new atomic rejection/`dead_letter` completion.

### Follow-up 1 tests

Migration/model constraints; tagged execution/processing projection tests; worker PostgreSQL
commit/rollback/continuation and fatal-error tests; manual record-only transaction and API safe
response/no-leak tests; recovery and replay-without-DNS/repeated-rejection regressions.

### Follow-up 1 documentation

Update database, delivery execution, manual API, architecture, and changelog documentation.

### Follow-up 1 non-goals

No resolver/transport, new job status, fake attempt, endpoint preflight, or proxy support.

### Follow-up 1 validation commands

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/test_migrations.py tests/test_delivery_job_execution_service.py tests/test_delivery_processing_service.py tests/test_delivery_service_transaction_integration.py tests/test_manual_delivery_api.py tests/test_delivery_job_recovery_service.py tests/test_replay_service.py
& ".\.venv\Scripts\python.exe" -m ruff check migrations src tests
& ".\.venv\Scripts\python.exe" -m mypy src
```

### Follow-up 1 dependencies

Depends on issue #57's decision. Blocks follow-up 2.

## Follow-up draft 2

### Follow-up 2 title

Enforce webhook destination policy at the DNS-to-connection boundary

### Follow-up 2 context

Issue #57 proved validation must bind an all-address-approved resolver snapshot to numeric dialing
and peer inspection while retaining Host, SNI, and certificate verification.

### Follow-up 2 scope

- Implement shared URL/address policy and stable rejection reasons.
- Implement `httpx2.BaseTransport` backed by
  `httpcore2.ConnectionPool(network_backend=...)`; never use `HTTPTransport._pool`.
- Resolve once per new connection, validate all normalized answers, dial snapshot addresses only,
  restrict fallback, and verify peer before write.
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
- Because production will import `httpcore2` directly, add it as a direct dependency constrained to
  the verified 2.9 API line (`httpcore2>=2.9.1,<2.10`) and document the deliberate compatibility
  upgrade procedure rather than relying on httpx2's transitive resolution.

### Follow-up 2 acceptance criteria

- Denied/mixed answers reject durably before connection; no second lookup occurs.
- Every dial is numeric and in snapshot; fallback cannot escape it.
- Denied/mismatched peer fails before write.
- Reconnect or expiry cannot reuse a hostname-level snapshot; each physical connection has one
  freshly resolved snapshot, or connection reuse is disabled.
- Host, SNI, certificate verification, timeout behavior, and redirect prohibition remain.
- Environment proxies cannot bypass enforcement.
- Policy signals retain their type and safe metadata through the adapter, are never normalized as
  transport errors, and contain no sensitive exception/address detail.
- Manual/worker paths share policy but retain distinct state mutation.
- `httpcore2` is a direct versioned dependency, and dependency upgrades have a public-seam and
  real-`server_addr` compatibility test.

### Follow-up 2 tests

Add deterministic cases based on the PoC for IPv4/IPv6 normalization, mapped addresses, mixed
answers, fallback, peer mismatch, TLS identity, per-connection snapshot ownership, timeout/error
mapping, typed-signal bypass, and proxy isolation. Add controlled local-server tests for real
`server_addr`, missing/malformed peer fail-closed behavior, reconnect/expiry, and worker/manual
rejection integrations. Use no public internet.

### Follow-up 2 documentation

Update architecture, security limitations, delivery execution, dependency evidence, and operations;
record keepalive and proxy decisions.

### Follow-up 2 non-goals

No endpoint preflight, redirects, arbitrary proxies, protocol rewrite, or private internals.

### Follow-up 2 validation commands

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -W error -p no:cacheprovider tests/experimental/test_webhook_ssrf_boundary_spike.py tests/test_delivery_http.py tests/test_delivery_processing_service.py
& ".\.venv\Scripts\python.exe" -c "import httpcore2; assert httpcore2.__version__.startswith('2.9.')"
& ".\.venv\Scripts\python.exe" -m ruff check src tests
& ".\.venv\Scripts\python.exe" -m mypy src
```

### Follow-up 2 dependencies

Depends on follow-up 1. It must add and verify the direct `httpcore2>=2.9.1,<2.10` dependency
alongside compatible `httpx2`, then lock that public-seam evidence into tests. Blocks follow-up 3.

## Follow-up draft 3

### Follow-up 3 title

Add webhook endpoint destination-policy preflight with safe API errors

### Follow-up 3 context

Connection-time enforcement stays authoritative, but endpoint creation should reject obviously
invalid/currently forbidden destinations early without exposing network detail.

### Follow-up 3 scope

- Reuse follow-up 2's exact parser/address policy during endpoint creation.
- Perform non-authoritative current DNS preflight without dialing.
- Return stable safe errors without internal IPs, resolver detail, or exception text.
- Keep connection-time re-resolution/enforcement; successful preflight is not authorization cache.
- Decide explicitly whether creation rejection also gets follow-up 1's manual audit record.

### Follow-up 3 acceptance criteria

- Invalid scheme, credentials, port, empty/malformed answers, and any denied answer reject.
- Errors are stable and redact address/resolver data.
- Passing preflight never bypasses connection-bound policy or pins stale DNS.
- Existing successful endpoint response remains compatible.
- Resolver failure has safe classification and retry guidance.

### Follow-up 3 tests

Endpoint service/API allowed, denied, mixed, malformed, empty, and resolver-failure tests with
redaction assertions; regression proving delivery still performs authoritative enforcement.

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

Depends on follow-up 2's shared policy/taxonomy and on follow-up 1 if creation rejections are
persisted.
