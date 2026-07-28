import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.operations_service import (
    check_database_readiness,
    get_webhook_operational_summary,
)

GENERATED_AT = datetime(1900, 1, 2, 12, 0, tzinfo=UTC)
STALE_TIMEOUT_SECONDS = 300.0
STALE_BEFORE = GENERATED_AT - timedelta(seconds=STALE_TIMEOUT_SECONDS)


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


def _persist_event(
    scope: _DatabaseScope,
    *,
    label: str,
) -> uuid.UUID:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    scope.endpoint_ids.append(endpoint_id)
    scope.event_ids.append(event_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Operations service {label} {endpoint_id}",
                target_url=f"https://example.test/operations/{endpoint_id}",
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=f"operations.{label}",
                payload={"label": label},
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
                attempt_count=2,
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


def test_readiness_uses_real_postgresql_without_table_mutation() -> None:
    with SessionFactory() as session:
        counts_before = _table_counts(session)
        result = check_database_readiness(session)
        assert result.database == "ok"

    with SessionFactory() as session:
        assert _table_counts(session) == counts_before


def test_mixed_status_summary_has_exact_boundary_deltas_and_minima(
    database_scope: _DatabaseScope,
) -> None:
    with SessionFactory() as session:
        baseline = get_webhook_operational_summary(
            session,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS,
        )

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
            label="pending-due-before",
            status="pending",
            next_attempt_at=GENERATED_AT - timedelta(days=1),
            updated_at=GENERATED_AT,
        ),
        _persist_job(
            database_scope,
            label="pending-due-exact",
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
        counts_before = _table_counts(session)
        result = get_webhook_operational_summary(
            session,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS,
        )

    assert result.delivery_jobs.pending - baseline.delivery_jobs.pending == 3
    assert result.delivery_jobs.processing - baseline.delivery_jobs.processing == 3
    assert result.delivery_jobs.succeeded - baseline.delivery_jobs.succeeded == 1
    assert result.delivery_jobs.dead_letter - baseline.delivery_jobs.dead_letter == 1
    assert result.delivery_jobs.due_pending - baseline.delivery_jobs.due_pending == 2
    assert result.delivery_jobs.stale_processing - baseline.delivery_jobs.stale_processing == 1
    assert result.oldest_due_pending_at == GENERATED_AT - timedelta(days=1)
    assert result.oldest_processing_updated_at == STALE_BEFORE - timedelta(seconds=1)
    assert result.stale_processing_before == STALE_BEFORE
    assert {job_id: _job_state(job_id) for job_id in records} == states_before
    with SessionFactory() as session:
        assert _table_counts(session) == counts_before


def test_summary_visibility_changes_only_after_caller_commit(
    database_scope: _DatabaseScope,
) -> None:
    event_id = _persist_event(database_scope, label="visibility")
    job_id = uuid.uuid4()
    database_scope.job_ids.append(job_id)
    with SessionFactory() as session:
        baseline = get_webhook_operational_summary(
            session,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS,
        )

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

        with SessionFactory() as observer:
            before_commit = get_webhook_operational_summary(
                observer,
                generated_at=GENERATED_AT,
                stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS,
            )
            assert before_commit.delivery_jobs.pending == baseline.delivery_jobs.pending
            observer.rollback()

        writer.commit()

    with SessionFactory() as observer:
        after_commit = get_webhook_operational_summary(
            observer,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=STALE_TIMEOUT_SECONDS,
        )
        assert after_commit.delivery_jobs.pending == baseline.delivery_jobs.pending + 1
