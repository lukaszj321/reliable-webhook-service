import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_job_query_service import (
    WebhookDeliveryJobEventNotFoundError,
    WebhookDeliveryJobNotFoundError,
    WebhookDeliveryJobStatus,
    get_webhook_delivery_job,
    list_webhook_delivery_jobs,
)
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


@dataclass(slots=True)
class _DatabaseScope:
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    status: WebhookDeliveryJobStatus
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


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
    event_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    endpoint_id = uuid.uuid4()
    stored_event_id = event_id or uuid.uuid4()
    scope.endpoint_ids.append(endpoint_id)
    scope.event_ids.append(stored_event_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery job query {label} {endpoint_id}",
                target_url=f"https://example.test/job-query/{endpoint_id}",
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=stored_event_id,
                endpoint_id=endpoint_id,
                event_type=f"job.query.{label}",
                payload={"label": label},
            )
        )
        session.commit()
    return endpoint_id, stored_event_id


def _persist_job(
    scope: _DatabaseScope,
    *,
    label: str,
    status: WebhookDeliveryJobStatus,
    updated_at: datetime,
    job_id: uuid.UUID | None = None,
    attempt_count: int = 0,
) -> _PersistedJob:
    endpoint_id, event_id = _persist_event(scope, label=label)
    stored_job_id = job_id or uuid.uuid4()
    next_attempt_at = updated_at if status in {"pending", "processing"} else None
    created_at = updated_at - timedelta(days=1)
    scope.job_ids.append(stored_job_id)
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryJob(
                id=stored_job_id,
                event_id=event_id,
                status=status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.commit()
    return _PersistedJob(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=stored_job_id,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _job_state(job_id: uuid.UUID) -> tuple[object, ...]:
    with SessionFactory() as session:
        job = session.get(WebhookDeliveryJob, job_id)
        assert job is not None
        return (
            job.id,
            job.event_id,
            job.status,
            job.attempt_count,
            job.next_attempt_at,
            job.created_at,
            job.updated_at,
        )


@pytest.mark.parametrize(
    "job_status",
    ["pending", "processing", "succeeded", "dead_letter"],
)
def test_lookup_returns_committed_exact_snapshot_without_mutation(
    database_scope: _DatabaseScope,
    job_status: WebhookDeliveryJobStatus,
) -> None:
    updated_at = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
    persisted = _persist_job(
        database_scope,
        label=f"lookup-{job_status}",
        status=job_status,
        updated_at=updated_at,
        attempt_count=3,
    )
    state_before = _job_state(persisted.job_id)
    with SessionFactory() as session:
        counts_before = _table_counts(session)
        snapshot = get_webhook_delivery_job(session, event_id=persisted.event_id)
        assert snapshot.id == persisted.job_id
        assert snapshot.event_id == persisted.event_id
        assert snapshot.status == job_status
        assert snapshot.attempt_count == 3
        assert snapshot.next_attempt_at == persisted.next_attempt_at
        assert snapshot.created_at == persisted.created_at
        assert snapshot.updated_at == persisted.updated_at

    with SessionFactory() as session:
        assert _table_counts(session) == counts_before
    assert _job_state(persisted.job_id) == state_before


def test_lookup_distinguishes_missing_event_from_event_without_job(
    database_scope: _DatabaseScope,
) -> None:
    _, event_id = _persist_event(database_scope, label="missing-job")

    with SessionFactory() as session:
        with pytest.raises(
            WebhookDeliveryJobEventNotFoundError,
            match="^Webhook event not found$",
        ):
            get_webhook_delivery_job(session, event_id=uuid.uuid4())
        with pytest.raises(
            WebhookDeliveryJobNotFoundError,
            match="^Webhook delivery job not found$",
        ):
            get_webhook_delivery_job(session, event_id=event_id)


def test_lookup_observes_job_only_after_caller_commit(
    database_scope: _DatabaseScope,
) -> None:
    _, event_id = _persist_event(database_scope, label="commit-visibility")
    job_id = uuid.uuid4()
    database_scope.job_ids.append(job_id)
    created_at = datetime(2099, 1, 2, 12, 0, tzinfo=UTC)

    with SessionFactory() as writer:
        writer.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status="pending",
                attempt_count=0,
                next_attempt_at=created_at,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        writer.flush()

        with SessionFactory() as observer:
            with pytest.raises(WebhookDeliveryJobNotFoundError):
                get_webhook_delivery_job(observer, event_id=event_id)
            observer.rollback()

        writer.commit()

    with SessionFactory() as observer:
        assert get_webhook_delivery_job(observer, event_id=event_id).id == job_id


def test_mixed_status_listing_filters_and_orders_test_records(
    database_scope: _DatabaseScope,
) -> None:
    base = datetime(2099, 2, 1, 12, 0, tzinfo=UTC)
    records = [
        _persist_job(
            database_scope,
            label=f"mixed-{index}",
            status=job_status,
            updated_at=base + timedelta(minutes=index),
            job_id=uuid.UUID(int=10 + index),
        )
        for index, job_status in enumerate(("pending", "processing", "succeeded", "dead_letter"))
    ]
    ids = {record.job_id for record in records}

    with SessionFactory() as session:
        page = list_webhook_delivery_jobs(session, status=None, limit=100, cursor=None)
        returned = [item for item in page.items if item.id in ids]
        assert [item.id for item in returned] == [record.job_id for record in reversed(records)]

        for record in records:
            filtered = list_webhook_delivery_jobs(
                session,
                status=record.status,
                limit=100,
                cursor=None,
            )
            test_items = [item for item in filtered.items if item.id in ids]
            assert [item.id for item in test_items] == [record.job_id]
            assert all(item.status == record.status for item in filtered.items)


def test_keyset_pagination_is_complete_stable_and_uses_uuid_tie_breaker(
    database_scope: _DatabaseScope,
) -> None:
    base = datetime(2099, 3, 1, 12, 0, tzinfo=UTC)
    records = [
        _persist_job(
            database_scope,
            label=f"page-{identifier}",
            status="dead_letter",
            updated_at=updated_at,
            job_id=uuid.UUID(int=identifier),
            attempt_count=identifier,
        )
        for identifier, updated_at in (
            (1, base),
            (2, base + timedelta(minutes=1)),
            (3, base + timedelta(minutes=2)),
            (4, base + timedelta(minutes=2)),
            (5, base + timedelta(minutes=3)),
        )
    ]
    expected_ids = [
        uuid.UUID(int=5),
        uuid.UUID(int=4),
        uuid.UUID(int=3),
        uuid.UUID(int=2),
        uuid.UUID(int=1),
    ]
    test_ids = {record.job_id for record in records}
    collected: list[uuid.UUID] = []
    cursor: str | None = None

    with SessionFactory() as session:
        for _ in range(3):
            page = list_webhook_delivery_jobs(
                session,
                status="dead_letter",
                limit=2,
                cursor=cursor,
            )
            collected.extend(item.id for item in page.items if item.id in test_ids)
            cursor = page.next_cursor
            if cursor is None:
                break

    assert collected == expected_ids
    assert len(collected) == len(set(collected))
    assert set(collected) == test_ids

    with SessionFactory() as session:
        single = list_webhook_delivery_jobs(
            session,
            status="dead_letter",
            limit=1,
            cursor=None,
        )
    assert single.items[0].id == uuid.UUID(int=5)
    assert single.next_cursor is not None


def test_listing_is_read_only_for_rows_and_table_counts(
    database_scope: _DatabaseScope,
) -> None:
    persisted = _persist_job(
        database_scope,
        label="read-only",
        status="processing",
        updated_at=datetime(2099, 4, 1, 12, 0, tzinfo=UTC),
        attempt_count=7,
    )
    state_before = _job_state(persisted.job_id)
    with SessionFactory() as session:
        counts_before = _table_counts(session)
        list_webhook_delivery_jobs(
            session,
            status="processing",
            limit=100,
            cursor=None,
        )

    assert _job_state(persisted.job_id) == state_before
    with SessionFactory() as session:
        assert _table_counts(session) == counts_before
