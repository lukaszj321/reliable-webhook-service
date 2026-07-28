import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service import main, operations_api
from reliable_webhook_service.config import Settings
from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

GENERATED_AT = datetime(1900, 2, 2, 12, 0, tzinfo=UTC)
STALE_TIMEOUT_SECONDS = 300.0
STALE_BEFORE = GENERATED_AT - timedelta(seconds=STALE_TIMEOUT_SECONDS)


class _FakeRawHttpClient:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass(slots=True)
class _DatabaseScope:
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)


def _table_counts(session: Session) -> tuple[int, int, int, int]:
    values = tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (
            WebhookEndpoint,
            WebhookEvent,
            WebhookDeliveryJob,
            WebhookDeliveryAttempt,
        )
    )
    assert all(value is not None for value in values)
    return tuple(int(value) for value in values)


@pytest.fixture
def database_scope() -> Iterator[_DatabaseScope]:
    with SessionFactory() as session:
        scope = _DatabaseScope(initial_counts=_table_counts(session))

    try:
        yield scope
    finally:
        with SessionFactory() as session:
            session.rollback()
            for job_id in scope.job_ids:
                job = session.get(WebhookDeliveryJob, job_id)
                if job is not None:
                    session.delete(job)
            session.commit()
            for event_id in scope.event_ids:
                event = session.get(WebhookEvent, event_id)
                if event is not None:
                    session.delete(event)
            session.commit()
            for endpoint_id in scope.endpoint_ids:
                endpoint = session.get(WebhookEndpoint, endpoint_id)
                if endpoint is not None:
                    session.delete(endpoint)
            session.commit()

        with SessionFactory() as session:
            assert _table_counts(session) == scope.initial_counts
            idle_transactions = session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND state = 'idle in transaction'
                    """
                )
            )
            assert idle_transactions == 0


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(main.httpx2, "Client", _FakeRawHttpClient)
    application = main.create_app()

    def fail_http_dependency() -> None:
        raise AssertionError("Operational GET must not resolve HTTP dependency")

    application.dependency_overrides[get_webhook_http_client] = fail_http_dependency
    application.dependency_overrides[get_settings] = lambda: Settings(
        webhook_worker_stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS
    )
    yield application
    application.dependency_overrides.clear()


def _persist_event(scope: _DatabaseScope, *, label: str) -> uuid.UUID:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    scope.endpoint_ids.append(endpoint_id)
    scope.event_ids.append(event_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Operations API {label} {endpoint_id}",
                target_url=f"https://unreachable.example.test/{endpoint_id}",
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=f"operations.api.{label}",
                payload={"secret": "not-returned", "label": label},
            )
        )
        session.commit()
    return event_id


def _persist_job(
    scope: _DatabaseScope,
    *,
    label: str,
    status: str,
    next_attempt_at: datetime | None,
    updated_at: datetime,
) -> uuid.UUID:
    event_id = _persist_event(scope, label=label)
    job_id = uuid.uuid4()
    scope.job_ids.append(job_id)
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status=status,
                attempt_count=4,
                next_attempt_at=next_attempt_at,
                created_at=updated_at - timedelta(days=1),
                updated_at=updated_at,
            )
        )
        session.commit()
    return job_id


def _job_state(job_id: uuid.UUID) -> tuple[object, ...]:
    with SessionFactory() as session:
        job = session.get(WebhookDeliveryJob, job_id)
        assert job is not None
        return (
            job.status,
            job.attempt_count,
            job.next_attempt_at,
            job.created_at,
            job.updated_at,
        )


def _counts(body: dict[str, object]) -> dict[str, int]:
    delivery_jobs = body["delivery_jobs"]
    assert isinstance(delivery_jobs, dict)
    return {key: int(value) for key, value in delivery_jobs.items()}


def test_readiness_api_uses_real_postgresql_without_side_effects(
    application: FastAPI,
) -> None:
    with SessionFactory() as session:
        counts_before = _table_counts(session)

    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok"},
    }
    with SessionFactory() as session:
        assert _table_counts(session) == counts_before


def test_summary_api_returns_exact_mixed_status_deltas_and_boundaries(
    database_scope: _DatabaseScope,
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_api, "_utc_now", lambda: GENERATED_AT)
    with TestClient(application) as client:
        baseline_response = client.get("/operations/summary")
    assert baseline_response.status_code == 200
    baseline_body = baseline_response.json()
    baseline_counts = _counts(baseline_body)

    records = [
        _persist_job(
            database_scope,
            label="pending-future",
            status="pending",
            next_attempt_at=GENERATED_AT + timedelta(seconds=1),
            updated_at=GENERATED_AT,
        ),
        _persist_job(
            database_scope,
            label="pending-before",
            status="pending",
            next_attempt_at=GENERATED_AT - timedelta(days=1),
            updated_at=GENERATED_AT,
        ),
        _persist_job(
            database_scope,
            label="pending-exact",
            status="pending",
            next_attempt_at=GENERATED_AT,
            updated_at=GENERATED_AT,
        ),
        _persist_job(
            database_scope,
            label="processing-fresh",
            status="processing",
            next_attempt_at=GENERATED_AT,
            updated_at=STALE_BEFORE + timedelta(seconds=1),
        ),
        _persist_job(
            database_scope,
            label="processing-boundary",
            status="processing",
            next_attempt_at=GENERATED_AT,
            updated_at=STALE_BEFORE,
        ),
        _persist_job(
            database_scope,
            label="processing-stale",
            status="processing",
            next_attempt_at=GENERATED_AT,
            updated_at=STALE_BEFORE - timedelta(seconds=1),
        ),
        _persist_job(
            database_scope,
            label="succeeded",
            status="succeeded",
            next_attempt_at=None,
            updated_at=GENERATED_AT,
        ),
        _persist_job(
            database_scope,
            label="dead-letter",
            status="dead_letter",
            next_attempt_at=None,
            updated_at=GENERATED_AT,
        ),
    ]
    states_before = {job_id: _job_state(job_id) for job_id in records}
    with SessionFactory() as session:
        table_counts_before = _table_counts(session)

    with TestClient(application) as client:
        response = client.get("/operations/summary")

    assert response.status_code == 200
    body = response.json()
    counts = _counts(body)
    assert counts["pending"] - baseline_counts["pending"] == 3
    assert counts["processing"] - baseline_counts["processing"] == 3
    assert counts["succeeded"] - baseline_counts["succeeded"] == 1
    assert counts["dead_letter"] - baseline_counts["dead_letter"] == 1
    assert counts["due_pending"] - baseline_counts["due_pending"] == 2
    assert counts["stale_processing"] - baseline_counts["stale_processing"] == 1
    assert body["generated_at"] == "1900-02-02T12:00:00Z"
    assert body["oldest_due_pending_at"] == "1900-02-01T12:00:00Z"
    assert body["oldest_processing_updated_at"] == "1900-02-02T11:54:59Z"
    assert body["stale_processing_before"] == "1900-02-02T11:55:00Z"
    assert {job_id: _job_state(job_id) for job_id in records} == states_before
    with SessionFactory() as session:
        assert _table_counts(session) == table_counts_before


def test_summary_api_observes_flushed_job_only_after_commit(
    database_scope: _DatabaseScope,
    application: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_api, "_utc_now", lambda: GENERATED_AT)
    event_id = _persist_event(database_scope, label="visibility")
    job_id = uuid.uuid4()
    database_scope.job_ids.append(job_id)

    with TestClient(application) as client:
        baseline = _counts(client.get("/operations/summary").json())

        with SessionFactory() as writer:
            writer.add(
                WebhookDeliveryJob(
                    id=job_id,
                    event_id=event_id,
                    status="pending",
                    attempt_count=0,
                    next_attempt_at=GENERATED_AT,
                    created_at=GENERATED_AT,
                    updated_at=GENERATED_AT,
                )
            )
            writer.flush()
            before_commit = _counts(client.get("/operations/summary").json())
            assert before_commit["pending"] == baseline["pending"]
            writer.commit()

        after_commit = _counts(client.get("/operations/summary").json())

    assert after_commit["pending"] == baseline["pending"] + 1
