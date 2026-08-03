"""Experimental, offline PoC for binding DNS policy to webhook connections.

This is deliberately test-local. It characterizes public httpx2/httpcore2 seams and
must not be imported by production code.
"""

from __future__ import annotations

import ipaddress
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpcore2
import httpx2
import pytest

Resolver = Callable[[str], tuple[str, ...]]
AddressPolicy = Callable[[ipaddress.IPv4Address | ipaddress.IPv6Address], bool]


@dataclass(frozen=True)
class _Snapshot:
    hostname: str
    addresses: tuple[str, ...]


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
    ) -> None:
        self.events = events
        self.failing_addresses = set(failing_addresses)
        self.peer_overrides = peer_overrides or {}
        self.responses_per_connection = responses_per_connection

    def connect(self, address: str, port: int) -> _OfflineStream:
        # Parsing proves the boundary passes a numeric literal, not a DNS name.
        ipaddress.ip_address(address)
        self.events.append(f"dial:{address}:{port}")
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
    ) -> None:
        self.dialer = dialer
        self.policy = policy
        self.events = events
        self.snapshots: dict[str, _Snapshot] = {}

    def bind(self, snapshot: _Snapshot) -> None:
        self.snapshots[snapshot.hostname] = snapshot

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore2.SOCKET_OPTION] | None = None,
    ) -> httpcore2.NetworkStream:
        snapshot = self.snapshots[host]
        last_error: httpcore2.ConnectError | None = None
        for address in snapshot.addresses:
            try:
                stream = self.dialer.connect(address, port)
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
    ) -> None:
        self.resolver = resolver
        self.policy = policy
        self.events = events
        self.keepalive = max_keepalive_connections != 0
        self.snapshots: dict[str, _Snapshot] = {}
        self.backend = _SnapshotNetworkBackend(dialer=dialer, policy=policy, events=events)
        self.ssl_context = ssl.create_default_context()
        self.pool = httpcore2.ConnectionPool(
            ssl_context=self.ssl_context,
            network_backend=self.backend,
            max_keepalive_connections=max_keepalive_connections,
            http1=True,
            http2=False,
        )

    def _snapshot(self, hostname: str) -> _Snapshot:
        if self.keepalive and hostname in self.snapshots:
            self.events.append(f"snapshot_reused:{hostname}")
            return self.snapshots[hostname]

        self.events.append(f"resolve:{hostname}")
        addresses = self.resolver(hostname)
        if not addresses:
            raise httpx2.ConnectError("resolver returned no addresses")
        for value in addresses:
            address = ipaddress.ip_address(value)
            self.events.append(f"validate:{address}")
            if not self.policy(address):
                raise httpx2.ConnectError("resolver snapshot contained a denied address")
        snapshot = _Snapshot(hostname=hostname, addresses=addresses)
        if self.keepalive:
            self.snapshots[hostname] = snapshot
        self.backend.bind(snapshot)
        return snapshot

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        hostname = request.url.host
        self._snapshot(hostname)
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
        "validate:203.0.113.10",
        "validate:127.0.0.1",
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
