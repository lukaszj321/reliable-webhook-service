"""Experimental, offline PoC for SSRF snapshots and completion classification.

This is deliberately test-local. It characterizes public httpx2/httpcore2 seams and
pure classification semantics, and must not be imported by production code. It uses
no database, sockets, system DNS, sleep, system clock, real TLS, proxy, or HTTP/2.
"""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from typing import Any

import httpcore2
import httpx2
import pytest

Resolver = Callable[[str], Iterable[str]]
AddressPolicy = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]

MAX_RAW_RESOLVER_RECORDS = 32
MAX_NORMALIZED_ADDRESSES = 8
MAX_CONNECT_ATTEMPTS = 4


class _FakeClock:
    """Deterministic monotonic clock; it never reads or sleeps system time."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(frozen=True)
class _Snapshot:
    hostname: str
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class _ClaimHandle:
    job_id: int
    delivery_cycle: int
    claim_generation: int


@dataclass(frozen=True)
class _CompletionJob:
    """Minimal durable state used only to prove locked classification semantics."""

    job_id: int
    delivery_cycle: int
    claim_generation: int
    status: str
    processing_started: bool
    last_completed_delivery_cycle: int | None = None
    last_completed_claim_generation: int | None = None

    def __post_init__(self) -> None:
        cycle_is_null = self.last_completed_delivery_cycle is None
        generation_is_null = self.last_completed_claim_generation is None
        if cycle_is_null != generation_is_null:
            raise ValueError("completion marker fields must be null or non-null together")


def _classify_completion(job: _CompletionJob, incoming: _ClaimHandle) -> str:
    """Model the four ordered checks performed under SELECT ... FOR UPDATE."""

    if (
        incoming.job_id != job.job_id
        or incoming.delivery_cycle != job.delivery_cycle
        or incoming.claim_generation != job.claim_generation
    ):
        return "stale-claim"
    if job.status == "processing" and job.processing_started:
        return "active-processing-claim"
    if (
        job.status != "processing"
        and job.last_completed_delivery_cycle == incoming.delivery_cycle
        and job.last_completed_claim_generation == incoming.claim_generation
    ):
        return "already-completed"
    return "stale-claim"


def _interleave_addresses(
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> tuple[str, ...]:
    ipv4 = sorted(
        (address for address in addresses if address.version == 4),
        key=lambda address: address.packed,
    )
    ipv6 = sorted(
        (address for address in addresses if address.version == 6),
        key=lambda address: address.packed,
    )
    ordered: list[str] = []
    for offset in range(max(len(ipv4), len(ipv6))):
        if offset < len(ipv4):
            ordered.append(str(ipv4[offset]))
        if offset < len(ipv6):
            ordered.append(str(ipv6[offset]))
    return tuple(ordered)


class _FakeSSLObject:
    def selected_alpn_protocol(self) -> str:
        return "http/1.1"


class _OfflineStream(httpcore2.NetworkStream):
    def __init__(self, *, peer: str, events: list[str], response_count: int = 1) -> None:
        self.peer = peer
        self.events = events
        self._closed = False
        self._responses = [
            b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n" for _ in range(response_count)
        ]

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._closed or not self._responses:
            return b""
        return self._responses.pop(0)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.events.append(f"write:{buffer.decode('ascii', errors='replace')}")

    def close(self) -> None:
        self._closed = True
        self.events.append(f"close:{self.peer}")

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.NetworkStream:
        self.events.append(
            "tls:"
            f"sni={server_hostname}:"
            f"verify_mode={ssl_context.verify_mode}:"
            f"check_hostname={ssl_context.check_hostname}"
        )
        return self

    def get_extra_info(self, info: str) -> Any:
        if info == "peername":
            return (self.peer, 443)
        if info == "ssl_object":
            return _FakeSSLObject()
        if info == "is_readable":
            return False
        return None


class _OfflineNumericDialer:
    """A deterministic fake: it never opens a socket or invokes system DNS."""

    def __init__(
        self,
        *,
        events: list[str],
        failing_addresses: Iterable[str] = (),
        peer_overrides: dict[str, str] | None = None,
        responses_per_connection: int = 2,
        clock: _FakeClock | None = None,
        connect_costs: dict[str, float] | None = None,
    ) -> None:
        self.events = events
        self.failing_addresses = set(failing_addresses)
        self.peer_overrides = peer_overrides or {}
        self.responses_per_connection = responses_per_connection
        self.clock = clock or _FakeClock()
        self.connect_costs = connect_costs or {}

    def connect(self, address: str, port: int, *, timeout: float) -> _OfflineStream:
        # Parsing proves the boundary passes a numeric literal, not a DNS name.
        ipaddress.ip_address(address)
        self.events.append(f"dial_budget:{timeout:.1f}")
        self.events.append(f"dial:{address}:{port}")
        self.clock.advance(self.connect_costs.get(address, 0.0))
        if address in self.failing_addresses:
            self.events.append(f"dial_failed:{address}")
            raise httpcore2.ConnectError(f"offline failure for {address}")
        return _OfflineStream(
            peer=self.peer_overrides.get(address, address),
            events=self.events,
            response_count=self.responses_per_connection,
        )


class _SnapshotNetworkBackend(httpcore2.NetworkBackend):
    def __init__(
        self,
        *,
        dialer: _OfflineNumericDialer,
        policy: AddressPolicy,
        events: list[str],
        clock: _FakeClock,
        max_connect_attempts: int,
    ) -> None:
        self.dialer = dialer
        self.policy = policy
        self.events = events
        self.clock = clock
        self.max_connect_attempts = max_connect_attempts
        self.snapshots: dict[str, tuple[_Snapshot, float]] = {}

    def bind(self, snapshot: _Snapshot, deadline: float) -> None:
        self.snapshots[snapshot.hostname] = (snapshot, deadline)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        snapshot, deadline = self.snapshots[host]
        last_error: httpcore2.ConnectError | None = None
        for address in snapshot.addresses[: self.max_connect_attempts]:
            remaining = deadline - self.clock()
            if remaining <= 0:
                self.events.append("deadline_exhausted_before_dial")
                raise httpcore2.ConnectTimeout("shared delivery deadline exhausted")
            try:
                stream = self.dialer.connect(address, port, timeout=remaining)
            except httpcore2.ConnectError as error:
                last_error = error
                continue

            peer = ipaddress.ip_address(stream.get_extra_info("peername")[0])
            self.events.append(f"peer:{peer}")
            if str(peer) not in snapshot.addresses or not self.policy(peer):
                stream.close()
                raise httpcore2.ConnectError("connected peer was not in the approved snapshot")
            self.events.append(f"peer_approved:{peer}")
            return stream

        raise last_error or httpcore2.ConnectError("approved snapshot had no reachable address")

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        raise AssertionError("Unix sockets are outside the webhook transport boundary")


class _SpikeTransport(httpx2.BaseTransport):
    """Minimal characterization, not a production-ready concurrent transport."""

    def __init__(
        self,
        *,
        resolver: Resolver,
        policy: AddressPolicy,
        dialer: _OfflineNumericDialer,
        events: list[str],
        max_keepalive_connections: int = 10,
        clock: _FakeClock | None = None,
        delivery_deadline_seconds: float = 10.0,
        max_raw_resolver_records: int = MAX_RAW_RESOLVER_RECORDS,
        max_resolved_addresses: int = MAX_NORMALIZED_ADDRESSES,
        max_connect_attempts: int = MAX_CONNECT_ATTEMPTS,
    ) -> None:
        self.resolver = resolver
        self.policy = policy
        self.events = events
        self.keepalive = max_keepalive_connections != 0
        self.clock = clock or dialer.clock
        self.delivery_deadline_seconds = delivery_deadline_seconds
        self.max_raw_resolver_records = max_raw_resolver_records
        self.max_resolved_addresses = max_resolved_addresses
        self.snapshots: dict[str, _Snapshot] = {}
        self.backend = _SnapshotNetworkBackend(
            dialer=dialer,
            policy=policy,
            events=events,
            clock=self.clock,
            max_connect_attempts=max_connect_attempts,
        )
        self.ssl_context = ssl.create_default_context()
        self.pool = httpcore2.ConnectionPool(
            ssl_context=self.ssl_context,
            network_backend=self.backend,
            max_keepalive_connections=max_keepalive_connections,
            http1=True,
            http2=False,
        )

    def _snapshot(self, hostname: str, deadline: float) -> _Snapshot:
        if self.keepalive and hostname in self.snapshots:
            self.events.append(f"snapshot_reused:{hostname}")
            return self.snapshots[hostname]

        self.events.append(f"resolve:{hostname}")
        raw_addresses: list[str] = []
        for record_number, value in enumerate(self.resolver(hostname), start=1):
            if record_number > self.max_raw_resolver_records:
                raise httpx2.ConnectError("resolver answer exceeded the safe raw record limit")
            raw_addresses.append(value)
        if deadline - self.clock() <= 0:
            raise httpx2.ConnectTimeout("shared delivery deadline exhausted")
        if not raw_addresses:
            raise httpx2.ConnectError("resolver returned no addresses")
        normalized = _interleave_addresses({ipaddress.ip_address(value) for value in raw_addresses})
        if len(normalized) > self.max_resolved_addresses:
            raise httpx2.ConnectError("resolver snapshot exceeded the safe address limit")
        denied = False
        for value in normalized:
            address = ipaddress.ip_address(value)
            self.events.append(f"validate:{address}")
            if not self.policy(address):
                denied = True
        if denied:
            raise httpx2.ConnectError("resolver snapshot contained a denied address")
        snapshot = _Snapshot(hostname=hostname, addresses=normalized)
        if self.keepalive:
            self.snapshots[hostname] = snapshot
        self.backend.bind(snapshot, deadline)
        return snapshot

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        hostname = request.url.host
        deadline = self.clock() + self.delivery_deadline_seconds
        self._snapshot(hostname, deadline)
        core_request = httpcore2.Request(
            method=request.method,
            url=httpcore2.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.read(),
            extensions=request.extensions,
        )
        core_response = self.pool.handle_request(core_request)
        try:
            content = core_response.read()
        finally:
            core_response.close()
        return httpx2.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            content=content,
        )

    def close(self) -> None:
        self.pool.close()


def _policy(*approved: str) -> AddressPolicy:
    approved_addresses = {ipaddress.ip_address(value) for value in approved}
    return lambda address: address in approved_addresses


def _post(client: httpx2.Client) -> httpx2.Response:
    return client.post(
        "https://hooks.example.test/deliver?source=spike",
        content=b"{}",
        headers={"content-type": "application/json"},
        follow_redirects=False,
    )


def test_all_addresses_are_validated_fail_closed_before_any_dial() -> None:
    events: list[str] = []

    def resolver(hostname: str) -> tuple[str, ...]:
        return ("203.0.113.10", "127.0.0.1")

    dialer = _OfflineNumericDialer(events=events)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx2.ConnectError, match="denied address"):
            _post(client)

    assert events == [
        "resolve:hooks.example.test",
        "validate:127.0.0.1",
        "validate:203.0.113.10",
    ]


def test_snapshot_dials_only_approved_numeric_fallback_and_preserves_authority() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ("203.0.113.10", "203.0.113.11")

    dialer = _OfflineNumericDialer(events=events, failing_addresses={"203.0.113.10"})
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10", "203.0.113.11"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        response = _post(client)

    assert response.status_code == 204
    assert resolver_calls == 1
    assert "dial:203.0.113.10:443" in events
    assert "dial:203.0.113.11:443" in events
    assert all("127.0.0.1" not in event for event in events)
    assert "tls:sni=hooks.example.test:verify_mode=2:check_hostname=True" in events
    wire = "".join(event for event in events if event.startswith("write:"))
    assert "Host: hooks.example.test" in wire
    assert events.index("validate:203.0.113.11") < events.index("dial:203.0.113.10:443")
    assert events.index("peer_approved:203.0.113.11") < next(
        index for index, event in enumerate(events) if event.startswith("write:")
    )


def test_connected_peer_is_checked_before_http_write() -> None:
    events: list[str] = []
    dialer = _OfflineNumericDialer(
        events=events,
        peer_overrides={"203.0.113.10": "127.0.0.1"},
    )
    transport = _SpikeTransport(
        resolver=lambda hostname: ("203.0.113.10",),
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpcore2.ConnectError, match="connected peer"):
            _post(client)

    assert "peer:127.0.0.1" in events
    assert not any(event.startswith("write:") for event in events)


def test_default_keepalive_reuses_guarded_connection_without_second_lookup() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ("203.0.113.10",)

    dialer = _OfflineNumericDialer(events=events, responses_per_connection=2)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        assert _post(client).status_code == 204
        assert _post(client).status_code == 204

    assert resolver_calls == 1
    assert events.count("dial:203.0.113.10:443") == 1
    assert events.count("snapshot_reused:hooks.example.test") == 1


def test_zero_keepalive_creates_a_fresh_guarded_connection_per_request() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return ("203.0.113.10",)

    dialer = _OfflineNumericDialer(events=events, responses_per_connection=1)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
        max_keepalive_connections=0,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        assert _post(client).status_code == 204
        assert _post(client).status_code == 204

    assert resolver_calls == 2
    assert events.count("dial:203.0.113.10:443") == 2
    assert events.count("peer_approved:203.0.113.10") == 2


def test_trust_env_false_does_not_consult_environment_proxy_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    def unexpected_proxy_discovery() -> dict[str, str | None]:
        raise AssertionError("environment proxy discovery must remain disabled")

    monkeypatch.setattr(httpx2._client, "get_environment_proxies", unexpected_proxy_discovery)
    dialer = _OfflineNumericDialer(events=events)
    transport = _SpikeTransport(
        resolver=lambda hostname: ("203.0.113.10",),
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        assert _post(client).status_code == 204

    assert "dial:203.0.113.10:443" in events
    assert all("127.0.0.1" not in event for event in events)


def test_one_fake_clock_budget_covers_resolution_and_all_fallback_attempts() -> None:
    events: list[str] = []
    resolver_calls = 0
    clock = _FakeClock()

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        clock.advance(2.0)
        return ("203.0.113.10", "203.0.113.11", "203.0.113.12")

    dialer = _OfflineNumericDialer(
        events=events,
        failing_addresses={"203.0.113.10", "203.0.113.11", "203.0.113.12"},
        clock=clock,
        connect_costs={"203.0.113.10": 5.0, "203.0.113.11": 5.0},
    )
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10", "203.0.113.11", "203.0.113.12"),
        dialer=dialer,
        events=events,
        clock=clock,
        delivery_deadline_seconds=10.0,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpcore2.ConnectTimeout, match="shared delivery deadline"):
            _post(client)

    assert resolver_calls == 1
    assert [event for event in events if event.startswith("dial_budget:")] == [
        "dial_budget:8.0",
        "dial_budget:3.0",
    ]
    assert "dial:203.0.113.12:443" not in events
    assert events[-1] == "deadline_exhausted_before_dial"


def test_default_connect_attempt_limit_is_exactly_four() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return (
            "203.0.113.10",
            "203.0.113.11",
            "203.0.113.12",
            "203.0.113.13",
            "203.0.113.14",
        )

    dialer = _OfflineNumericDialer(
        events=events,
        failing_addresses={
            "203.0.113.10",
            "203.0.113.11",
            "203.0.113.12",
            "203.0.113.13",
        },
    )
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy(
            "203.0.113.10",
            "203.0.113.11",
            "203.0.113.12",
            "203.0.113.13",
            "203.0.113.14",
        ),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpcore2.ConnectError, match="offline failure"):
            _post(client)

    assert resolver_calls == 1
    assert [event for event in events if event.startswith("dial:")] == [
        "dial:203.0.113.10:443",
        "dial:203.0.113.11:443",
        "dial:203.0.113.12:443",
        "dial:203.0.113.13:443",
    ]
    assert "dial:203.0.113.14:443" not in events


def test_exactly_32_duplicate_heavy_raw_records_deduplicate_within_unique_limit() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return tuple(f"203.0.113.{value}" for value in range(10, 18)) * 4

    dialer = _OfflineNumericDialer(events=events)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy(
            "203.0.113.10",
            "203.0.113.11",
            "203.0.113.12",
            "203.0.113.13",
            "203.0.113.14",
            "203.0.113.15",
            "203.0.113.16",
            "203.0.113.17",
        ),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        assert _post(client).status_code == 204

    assert resolver_calls == 1
    assert len([event for event in events if event.startswith("validate:")]) == 8
    assert events.count("validate:203.0.113.10") == 1
    assert "dial:203.0.113.10:443" in events


def test_33_duplicate_raw_records_fail_closed_without_snapshot_dial_or_write() -> None:
    events: list[str] = []
    yielded_records = 0

    def resolver(hostname: str) -> Iterable[str]:
        nonlocal yielded_records
        for _ in range(40):
            yielded_records += 1
            yield "203.0.113.10"

    dialer = _OfflineNumericDialer(events=events)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy("203.0.113.10"),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx2.ConnectError, match="raw record limit"):
            _post(client)

    assert yielded_records == 33
    assert events == ["resolve:hooks.example.test"]
    assert transport.snapshots == {}


def test_mixed_family_round_robin_precedes_attempt_cap_and_stays_in_snapshot() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return (
            "192.0.2.40",
            "2001:db8::20",
            "192.0.2.20",
            "192.0.2.10",
            "2001:db8::10",
            "192.0.2.30",
            "192.0.2.10",
        )

    approved = (
        "192.0.2.10",
        "192.0.2.20",
        "192.0.2.30",
        "192.0.2.40",
        "2001:db8::10",
        "2001:db8::20",
    )
    dialer = _OfflineNumericDialer(
        events=events,
        failing_addresses={"192.0.2.10", "2001:db8::10", "192.0.2.20"},
    )
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy(*approved),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        assert _post(client).status_code == 204

    expected_snapshot = (
        "192.0.2.10",
        "2001:db8::10",
        "192.0.2.20",
        "2001:db8::20",
        "192.0.2.30",
        "192.0.2.40",
    )
    assert transport.snapshots["hooks.example.test"].addresses == expected_snapshot
    assert transport.backend.max_connect_attempts == MAX_CONNECT_ATTEMPTS
    assert resolver_calls == 1
    assert [event for event in events if event.startswith("dial:")] == [
        "dial:192.0.2.10:443",
        "dial:2001:db8::10:443",
        "dial:192.0.2.20:443",
        "dial:2001:db8::20:443",
    ]
    assert "dial:192.0.2.30:443" not in events
    assert all(
        event.removeprefix("dial:").removesuffix(":443") in expected_snapshot
        for event in events
        if event.startswith("dial:")
    )


def test_nine_unique_addresses_exceed_default_limit_before_any_dial() -> None:
    events: list[str] = []
    resolver_calls = 0

    def resolver(hostname: str) -> tuple[str, ...]:
        nonlocal resolver_calls
        resolver_calls += 1
        return tuple(f"203.0.113.{value}" for value in range(10, 19))

    dialer = _OfflineNumericDialer(events=events)
    transport = _SpikeTransport(
        resolver=resolver,
        policy=_policy(*(f"203.0.113.{value}" for value in range(10, 19))),
        dialer=dialer,
        events=events,
    )

    with httpx2.Client(transport=transport, trust_env=False) as client:
        with pytest.raises(httpx2.ConnectError, match="safe address limit"):
            _post(client)

    assert resolver_calls == 1
    assert events == ["resolve:hooks.example.test"]


def test_completion_marker_distinguishes_accepted_retry_from_recovery() -> None:
    handle = _ClaimHandle(job_id=41, delivery_cycle=0, claim_generation=7)
    accepted_retry = _CompletionJob(
        job_id=41,
        delivery_cycle=0,
        claim_generation=7,
        status="pending",
        processing_started=False,
        last_completed_delivery_cycle=0,
        last_completed_claim_generation=7,
    )
    recovered = _CompletionJob(
        job_id=41,
        delivery_cycle=0,
        claim_generation=7,
        status="pending",
        processing_started=False,
    )

    assert _classify_completion(accepted_retry, handle) == "already-completed"
    assert _classify_completion(recovered, handle) == "stale-claim"


def test_newer_claim_generation_fences_a_historical_completion_marker() -> None:
    old_handle = _ClaimHandle(job_id=41, delivery_cycle=0, claim_generation=7)
    reclaimed = _CompletionJob(
        job_id=41,
        delivery_cycle=0,
        claim_generation=8,
        status="processing",
        processing_started=True,
        last_completed_delivery_cycle=0,
        last_completed_claim_generation=7,
    )

    assert _classify_completion(reclaimed, old_handle) == "stale-claim"


def test_completion_marker_has_pair_nullability_and_no_http_outcome() -> None:
    marker_fields = {field.name for field in fields(_CompletionJob)}

    assert "last_completed_delivery_cycle" in marker_fields
    assert "last_completed_claim_generation" in marker_fields
    assert not any("http" in name or "outcome" in name for name in marker_fields)
    with pytest.raises(ValueError, match="null or non-null together"):
        _CompletionJob(
            job_id=41,
            delivery_cycle=0,
            claim_generation=7,
            status="pending",
            processing_started=False,
            last_completed_delivery_cycle=0,
        )
