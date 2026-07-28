import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from reliable_webhook_service.config import Settings
from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.main import create_app
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

RESPONSE_FIELDS = {
    "event_id",
    "delivery_job_id",
    "status",
    "next_attempt_at",
}

type TableCounts = tuple[int, int, int, int]
type AttemptSnapshot = tuple[uuid.UUID, int, str, int | None, str | None]


@dataclass(slots=True)
class DatabaseScope:
    initial_counts: TableCounts
    endpoint_ids: set[uuid.UUID] = field(default_factory=set)
    event_ids: set[uuid.UUID] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class PersistedReplayRecord:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID | None
    attempt_snapshots: tuple[AttemptSnapshot, ...]


def _table_counts() -> TableCounts:
    with SessionFactory() as session:
        counts = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                WebhookEndpoint,
                WebhookEvent,
                WebhookDeliveryJob,
                WebhookDeliveryAttempt,
            )
        )
    assert all(count is not None for count in counts)
    return (
        int(counts[0]),
        int(counts[1]),
        int(counts[2]),
        int(counts[3]),
    )


def _cleanup_scope(scope: DatabaseScope) -> None:
    with SessionFactory() as session:
        discovered_event_ids = set(
            session.scalars(
                select(WebhookEvent.id).where(WebhookEvent.endpoint_id.in_(scope.endpoint_ids))
            ).all()
        )
        event_ids = scope.event_ids | discovered_event_ids

        attempt_ids = set(
            session.scalars(
                select(WebhookDeliveryAttempt.id).where(
                    WebhookDeliveryAttempt.event_id.in_(event_ids)
                )
            ).all()
        )
        for attempt_id in attempt_ids:
            attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            if attempt is not None:
                session.delete(attempt)
        session.commit()

        job_ids = set(
            session.scalars(
                select(WebhookDeliveryJob.id).where(WebhookDeliveryJob.event_id.in_(event_ids))
            ).all()
        )
        for job_id in job_ids:
            job = session.get(WebhookDeliveryJob, job_id)
            if job is not None:
                session.delete(job)
        session.commit()

        for event_id in event_ids:
            event = session.get(WebhookEvent, event_id)
            if event is not None:
                session.delete(event)
        session.commit()

        for endpoint_id in scope.endpoint_ids:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if endpoint is not None:
                session.delete(endpoint)
        session.commit()


@pytest.fixture
def database_scope() -> Iterator[DatabaseScope]:
    scope = DatabaseScope(initial_counts=_table_counts())
    try:
        yield scope
    finally:
        _cleanup_scope(scope)
        assert _table_counts() == scope.initial_counts
        with SessionFactory() as session:
            idle_in_transaction = session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND state = 'idle in transaction'
                    """
                )
            )
            assert idle_in_transaction == 0


@contextmanager
def _application_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> Iterator[TestClient]:
    transport = httpx2.MockTransport(handler)
    with httpx2.Client(transport=transport) as raw_client:
        webhook_client = Httpx2WebhookHttpClient(raw_client)
        settings = Settings(
            _env_file=None,
            webhook_delivery_timeout_seconds=2.5,
        )
        application = create_app()
        application.dependency_overrides[get_webhook_http_client] = lambda: webhook_client
        application.dependency_overrides[get_settings] = lambda: settings
        get_settings.cache_clear()
        try:
            with TestClient(application) as client:
                yield client
        finally:
            application.dependency_overrides.clear()
            get_settings.cache_clear()


def _persist_replay_record(
    scope: DatabaseScope,
    *,
    label: str,
    status: str,
    is_active: bool = True,
    with_job: bool = True,
    attempt_total: int = 2,
) -> PersistedReplayRecord:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4() if with_job else None
    target_url = f"https://example.test/replay-api/{marker}/{label}"
    payload: dict[str, JsonValue] = {"marker": str(marker), "label": label}
    attempted_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    attempt_snapshots: list[AttemptSnapshot] = []
    scope.endpoint_ids.add(endpoint_id)
    scope.event_ids.add(event_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Replay API {marker} {label}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="replay.api.integration",
                payload=payload,
            )
        )
        session.flush()
        if job_id is not None:
            session.add(
                WebhookDeliveryJob(
                    id=job_id,
                    event_id=event_id,
                    status=status,
                    next_attempt_at=(
                        None
                        if status in {"succeeded", "dead_letter"}
                        else datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
                    ),
                    attempt_count=4,
                )
            )
            session.flush()
        for attempt_number in range(1, attempt_total + 1):
            attempt_id = uuid.uuid4()
            attempt = WebhookDeliveryAttempt(
                id=attempt_id,
                event_id=event_id,
                attempt_number=attempt_number,
                outcome="failed",
                target_url=target_url,
                response_status_code=503,
                error_message="HTTP response returned status 503",
                duration_ms=attempt_number,
                attempted_at=attempted_at,
            )
            session.add(attempt)
            attempt_snapshots.append(
                (
                    attempt_id,
                    attempt_number,
                    "failed",
                    503,
                    "HTTP response returned status 503",
                )
            )
        session.commit()

    return PersistedReplayRecord(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=job_id,
        attempt_snapshots=tuple(attempt_snapshots),
    )


def _attempt_snapshots(event_id: uuid.UUID) -> tuple[AttemptSnapshot, ...]:
    with SessionFactory() as session:
        rows = session.execute(
            select(
                WebhookDeliveryAttempt.id,
                WebhookDeliveryAttempt.attempt_number,
                WebhookDeliveryAttempt.outcome,
                WebhookDeliveryAttempt.response_status_code,
                WebhookDeliveryAttempt.error_message,
            )
            .where(WebhookDeliveryAttempt.event_id == event_id)
            .order_by(WebhookDeliveryAttempt.attempt_number)
        ).all()
    return tuple(tuple(row) for row in rows)


def _parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() is not None
    return timestamp.astimezone(UTC)


@pytest.mark.parametrize("terminal_status", ["succeeded", "dead_letter"])
def test_replay_terminal_job_returns_202_without_http_or_new_records(
    database_scope: DatabaseScope,
    terminal_status: str,
) -> None:
    persisted = _persist_replay_record(
        database_scope,
        label=terminal_status,
        status=terminal_status,
    )
    assert persisted.job_id is not None
    counts_before = _table_counts()
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        raise AssertionError(f"Unexpected replay HTTP request: {request.url}")

    with _application_client(handler) as client:
        response = client.post(f"/webhook-events/{persisted.event_id}/replay")

    assert response.status_code == 202
    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["event_id"] == str(persisted.event_id)
    assert body["delivery_job_id"] == str(persisted.job_id)
    assert body["status"] == "pending"
    replayed_at = _parse_timestamp(body["next_attempt_at"])
    assert requests == []

    with SessionFactory() as session:
        job = session.get(WebhookDeliveryJob, persisted.job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.attempt_count == 0
        assert job.next_attempt_at is not None
        assert job.next_attempt_at.astimezone(UTC) == replayed_at
        assert job.updated_at.astimezone(UTC) == replayed_at
        assert session.get(WebhookEvent, persisted.event_id) is not None
        assert session.get(WebhookEndpoint, persisted.endpoint_id) is not None

    assert _attempt_snapshots(persisted.event_id) == persisted.attempt_snapshots
    assert _table_counts() == counts_before

    with SessionFactory() as claim_session:
        claimed = claim_due_webhook_delivery_jobs(
            claim_session,
            claimed_at=replayed_at,
            limit=1000,
        )
        assert [job.id for job in claimed].count(persisted.job_id) == 1
        claim_session.rollback()


@pytest.mark.parametrize("active_status", ["pending", "processing"])
def test_replay_active_job_returns_409(
    database_scope: DatabaseScope,
    active_status: str,
) -> None:
    persisted = _persist_replay_record(
        database_scope,
        label=active_status,
        status=active_status,
        attempt_total=0,
    )

    with _application_client(lambda request: pytest.fail(str(request.url))) as client:
        response = client.post(f"/webhook-events/{persisted.event_id}/replay")

    assert response.status_code == 409
    assert response.json() == {"detail": "Webhook delivery job is not replayable"}


def test_replay_inactive_endpoint_returns_409(
    database_scope: DatabaseScope,
) -> None:
    persisted = _persist_replay_record(
        database_scope,
        label="inactive",
        status="succeeded",
        is_active=False,
        attempt_total=0,
    )

    with _application_client(lambda request: pytest.fail(str(request.url))) as client:
        response = client.post(f"/webhook-events/{persisted.event_id}/replay")

    assert response.status_code == 409
    assert response.json() == {"detail": "Webhook endpoint is inactive"}


def test_replay_missing_job_returns_409(
    database_scope: DatabaseScope,
) -> None:
    persisted = _persist_replay_record(
        database_scope,
        label="missing-job",
        status="succeeded",
        with_job=False,
        attempt_total=0,
    )

    with _application_client(lambda request: pytest.fail(str(request.url))) as client:
        response = client.post(f"/webhook-events/{persisted.event_id}/replay")

    assert response.status_code == 409
    assert response.json() == {"detail": "Webhook delivery job not found"}


def test_replay_missing_event_returns_404(
    database_scope: DatabaseScope,
) -> None:
    missing_event_id = uuid.uuid4()

    with _application_client(lambda request: pytest.fail(str(request.url))) as client:
        response = client.post(f"/webhook-events/{missing_event_id}/replay")

    assert response.status_code == 404
    assert response.json() == {"detail": "Webhook event not found"}


def test_manual_delivery_remains_synchronous_and_returns_201(
    database_scope: DatabaseScope,
) -> None:
    persisted = _persist_replay_record(
        database_scope,
        label="manual-regression",
        status="succeeded",
        with_job=False,
        attempt_total=0,
    )
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(204)

    with _application_client(handler) as client:
        response = client.post(f"/webhook-events/{persisted.event_id}/delivery-attempts")

    assert response.status_code == 201
    assert response.json()["attempt_number"] == 1
    assert response.json()["outcome"] == "succeeded"
    assert len(requests) == 1
    assert _attempt_snapshots(persisted.event_id)[0][1] == 1


def test_ingestion_idempotency_key_behavior_is_unchanged(
    database_scope: DatabaseScope,
) -> None:
    endpoint_id = uuid.uuid4()
    marker = uuid.uuid4()
    database_scope.endpoint_ids.add(endpoint_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Replay idempotency regression {marker}",
                target_url=f"https://example.test/replay-idempotency/{marker}",
            )
        )
        session.commit()

    request_body = {
        "endpoint_id": str(endpoint_id),
        "event_type": "replay.idempotency.regression",
        "payload": {"marker": str(marker)},
    }
    headers = {"Idempotency-Key": f"replay-regression-{marker}"}

    with _application_client(lambda request: pytest.fail(str(request.url))) as client:
        first = client.post("/webhook-events", json=request_body, headers=headers)
        second = client.post("/webhook-events", json=request_body, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()
    database_scope.event_ids.add(uuid.UUID(first.json()["id"]))
