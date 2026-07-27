import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_job_recovery_service import (
    recover_stale_webhook_delivery_jobs,
)
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


@dataclass(slots=True)
class _CreatedRecords:
    marker: uuid.UUID = field(default_factory=uuid.uuid4)
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    target_url: str
    event_type: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _JobState:
    status: str
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _AttemptState:
    id: uuid.UUID
    event_id: uuid.UUID
    attempt_number: int
    outcome: str
    target_url: str
    response_status_code: int | None
    error_message: str | None
    duration_ms: int
    attempted_at: datetime


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords()
    try:
        yield records
    finally:
        _cleanup_records(records)


def _cleanup_records(records: _CreatedRecords) -> None:
    with SessionFactory() as session:
        for attempt_id in records.attempt_ids:
            attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            if attempt is not None:
                session.delete(attempt)
        session.commit()

        for job_id in records.job_ids:
            job = session.get(WebhookDeliveryJob, job_id)
            if job is not None:
                session.delete(job)
        session.commit()

        for event_id in records.event_ids:
            event = session.get(WebhookEvent, event_id)
            if event is not None:
                session.delete(event)
        session.commit()

        for endpoint_id in records.endpoint_ids:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if endpoint is not None:
                session.delete(endpoint)
        session.commit()

    with SessionFactory() as session:
        for attempt_id in records.attempt_ids:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        for job_id in records.job_ids:
            assert session.get(WebhookDeliveryJob, job_id) is None
        for event_id in records.event_ids:
            assert session.get(WebhookEvent, event_id) is None
        for endpoint_id in records.endpoint_ids:
            assert session.get(WebhookEndpoint, endpoint_id) is None
        marker_count = session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.payload["recovery_integration_marker"].as_string()
                == str(records.marker)
            )
        )
        assert marker_count == 0


def _persist_job(
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    next_attempt_at: datetime | None,
    created_at: datetime,
    updated_at: datetime,
    job_id: uuid.UUID | None = None,
) -> _PersistedJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    resolved_job_id = uuid.uuid4() if job_id is None else job_id
    target_url = f"https://example.test/recovery/{records.marker}/{label}"
    event_type = f"delivery.recovery.integration.{label}"
    payload: dict[str, JsonValue] = {
        "recovery_integration_marker": str(records.marker),
        "label": label,
    }
    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(resolved_job_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery recovery {records.marker} {label}",
                target_url=target_url,
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=event_type,
                payload=payload,
            )
        )
        session.flush()
        session.add(
            WebhookDeliveryJob(
                id=resolved_job_id,
                event_id=event_id,
                status=status,
                next_attempt_at=next_attempt_at,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.commit()

    return _PersistedJob(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=resolved_job_id,
        target_url=target_url,
        event_type=event_type,
        payload=payload,
    )


def _persist_attempt(
    records: _CreatedRecords,
    persisted: _PersistedJob,
) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    records.attempt_ids.append(attempt_id)
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryAttempt(
                id=attempt_id,
                event_id=persisted.event_id,
                attempt_number=1,
                outcome="failed",
                target_url=persisted.target_url,
                response_status_code=503,
                error_message="HTTP response returned status 503",
                duration_ms=17,
                attempted_at=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
            )
        )
        session.commit()
    return attempt_id


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


def _optional_as_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)


def _get_job(session: Session, job_id: uuid.UUID) -> WebhookDeliveryJob:
    job = session.get(WebhookDeliveryJob, job_id)
    assert job is not None
    return job


def _job_state(job: WebhookDeliveryJob) -> _JobState:
    return _JobState(
        status=job.status,
        next_attempt_at=_optional_as_utc(job.next_attempt_at),
        created_at=_as_utc(job.created_at),
        updated_at=_as_utc(job.updated_at),
    )


def _job_states(job_ids: list[uuid.UUID]) -> dict[uuid.UUID, _JobState]:
    with SessionFactory() as session:
        return {job_id: _job_state(_get_job(session, job_id)) for job_id in job_ids}


def _attempt_state(attempt_id: uuid.UUID) -> _AttemptState:
    with SessionFactory() as session:
        attempt = session.get(WebhookDeliveryAttempt, attempt_id)
        assert attempt is not None
        return _AttemptState(
            id=attempt.id,
            event_id=attempt.event_id,
            attempt_number=attempt.attempt_number,
            outcome=attempt.outcome,
            target_url=attempt.target_url,
            response_status_code=attempt.response_status_code,
            error_message=attempt.error_message,
            duration_ms=attempt.duration_ms,
            attempted_at=_as_utc(attempt.attempted_at),
        )


def _attempt_count(event_ids: list[uuid.UUID]) -> int:
    with SessionFactory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.event_id.in_(event_ids))
        )
    assert count is not None
    return count


def _parent_states(
    records: _CreatedRecords,
) -> tuple[
    dict[uuid.UUID, tuple[str, str, bool, datetime, datetime]],
    dict[uuid.UUID, tuple[uuid.UUID, str, dict[str, JsonValue], datetime]],
]:
    with SessionFactory() as session:
        endpoints: dict[uuid.UUID, tuple[str, str, bool, datetime, datetime]] = {}
        for endpoint_id in records.endpoint_ids:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            assert endpoint is not None
            endpoints[endpoint_id] = (
                endpoint.name,
                endpoint.target_url,
                endpoint.is_active,
                _as_utc(endpoint.created_at),
                _as_utc(endpoint.updated_at),
            )

        events: dict[
            uuid.UUID,
            tuple[uuid.UUID, str, dict[str, JsonValue], datetime],
        ] = {}
        for event_id in records.event_ids:
            event = session.get(WebhookEvent, event_id)
            assert event is not None
            events[event_id] = (
                event.endpoint_id,
                event.event_type,
                event.payload,
                _as_utc(event.created_at),
            )
    return endpoints, events


def test_recovery_commits_only_stale_processing_jobs_and_preserves_attempts(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
    recovered_at = datetime(
        2026,
        7,
        30,
        14,
        30,
        tzinfo=timezone(timedelta(hours=2)),
    )
    expected_recovered_at = recovered_at.astimezone(UTC)
    old = _persist_job(
        created_records,
        label="eligible-old",
        status="processing",
        next_attempt_at=stale_before - timedelta(hours=3),
        created_at=stale_before - timedelta(hours=5),
        updated_at=stale_before - timedelta(hours=2),
    )
    boundary = _persist_job(
        created_records,
        label="eligible-boundary",
        status="processing",
        next_attempt_at=stale_before - timedelta(hours=2),
        created_at=stale_before - timedelta(hours=4),
        updated_at=stale_before,
    )
    fresh = _persist_job(
        created_records,
        label="fresh-processing",
        status="processing",
        next_attempt_at=stale_before + timedelta(hours=1),
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before + timedelta(seconds=1),
    )
    pending = _persist_job(
        created_records,
        label="pending",
        status="pending",
        next_attempt_at=stale_before - timedelta(hours=1),
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=1),
    )
    succeeded = _persist_job(
        created_records,
        label="succeeded",
        status="succeeded",
        next_attempt_at=None,
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=1),
    )
    dead_letter = _persist_job(
        created_records,
        label="dead-letter",
        status="dead_letter",
        next_attempt_at=None,
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=1),
    )
    attempt_id = _persist_attempt(created_records, old)
    initial_jobs = _job_states(created_records.job_ids)
    initial_attempt = _attempt_state(attempt_id)
    initial_attempt_count = _attempt_count(created_records.event_ids)
    initial_parents = _parent_states(created_records)

    with SessionFactory() as session:
        result = recover_stale_webhook_delivery_jobs(
            session,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=10,
        )
        assert result.recovered_job_ids == (old.job_id, boundary.job_id)
        assert result.recovered_count == 2
        for job_id in result.recovered_job_ids:
            job = _get_job(session, job_id)
            assert job.status == "pending"
            assert job.next_attempt_at == expected_recovered_at
            assert job.updated_at == expected_recovered_at
        session.commit()

    committed = _job_states(created_records.job_ids)
    for job_id in (old.job_id, boundary.job_id):
        assert committed[job_id].status == "pending"
        assert committed[job_id].next_attempt_at == expected_recovered_at
        assert committed[job_id].updated_at == expected_recovered_at
        assert committed[job_id].created_at == initial_jobs[job_id].created_at
    for job_id in (
        fresh.job_id,
        pending.job_id,
        succeeded.job_id,
        dead_letter.job_id,
    ):
        assert committed[job_id] == initial_jobs[job_id]
    assert _attempt_state(attempt_id) == initial_attempt
    assert _attempt_count(created_records.event_ids) == initial_attempt_count == 1
    assert _parent_states(created_records) == initial_parents


def test_recovery_rollback_restores_processing_state(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
    recovered_at = stale_before + timedelta(hours=1)
    persisted = _persist_job(
        created_records,
        label="rollback",
        status="processing",
        next_attempt_at=stale_before - timedelta(hours=2),
        created_at=stale_before - timedelta(hours=4),
        updated_at=stale_before - timedelta(hours=3),
    )
    initial = _job_states([persisted.job_id])[persisted.job_id]

    session = SessionFactory()
    try:
        result = recover_stale_webhook_delivery_jobs(
            session,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=1,
        )
        assert result.recovered_job_ids == (persisted.job_id,)
        job = _get_job(session, persisted.job_id)
        assert job.status == "pending"
        assert job.next_attempt_at == recovered_at
        assert job.updated_at == recovered_at
        session.rollback()
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()

    assert _job_states([persisted.job_id])[persisted.job_id] == initial
    assert _attempt_count(created_records.event_ids) == 0


def test_recovery_respects_deterministic_order_and_limit(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    recovered_at = stale_before + timedelta(hours=1)
    first_updated_at = stale_before - timedelta(hours=3)
    shared_updated_at = stale_before - timedelta(hours=2)
    first_created_at = stale_before - timedelta(hours=6)
    second_created_at = stale_before - timedelta(hours=5)
    shared_created_at = stale_before - timedelta(hours=4)
    low_id, high_id = sorted((uuid.uuid4(), uuid.uuid4()))
    jobs = [
        _persist_job(
            created_records,
            label="order-updated",
            status="processing",
            next_attempt_at=first_updated_at,
            created_at=shared_created_at,
            updated_at=first_updated_at,
        ),
        _persist_job(
            created_records,
            label="order-created",
            status="processing",
            next_attempt_at=shared_updated_at,
            created_at=first_created_at,
            updated_at=shared_updated_at,
        ),
        _persist_job(
            created_records,
            label="order-low-uuid",
            status="processing",
            next_attempt_at=shared_updated_at,
            created_at=second_created_at,
            updated_at=shared_updated_at,
            job_id=low_id,
        ),
        _persist_job(
            created_records,
            label="order-high-uuid",
            status="processing",
            next_attempt_at=shared_updated_at,
            created_at=second_created_at,
            updated_at=shared_updated_at,
            job_id=high_id,
        ),
    ]
    initial = _job_states(created_records.job_ids)
    expected_order = tuple(job.job_id for job in jobs)

    with SessionFactory() as session:
        result = recover_stale_webhook_delivery_jobs(
            session,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=3,
        )
        assert result.recovered_count == 3
        assert result.recovered_job_ids == expected_order[:3]
        assert expected_order[3] not in result.recovered_job_ids
        session.commit()

    committed = _job_states(created_records.job_ids)
    for job_id in expected_order[:3]:
        assert committed[job_id].status == "pending"
        assert committed[job_id].next_attempt_at == recovered_at
        assert committed[job_id].updated_at == recovered_at
    assert committed[expected_order[3]] == initial[expected_order[3]]
    assert _attempt_count(created_records.event_ids) == 0


def test_concurrent_recovery_sessions_use_skip_locked_and_recover_disjoint_jobs(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 13, 0, tzinfo=UTC)
    recovered_at = stale_before + timedelta(hours=1)
    jobs = [
        _persist_job(
            created_records,
            label=f"concurrent-{index}",
            status="processing",
            next_attempt_at=stale_before - timedelta(hours=2),
            created_at=stale_before - timedelta(hours=4),
            updated_at=stale_before - timedelta(hours=3, seconds=-index),
        )
        for index in range(4)
    ]
    expected_ids = tuple(job.job_id for job in jobs)
    session_a = SessionFactory()
    session_b = SessionFactory()
    result_a_ids: tuple[uuid.UUID, ...] = ()
    result_b_ids: tuple[uuid.UUID, ...] = ()
    try:
        result_a = recover_stale_webhook_delivery_jobs(
            session_a,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=2,
        )
        result_a_ids = result_a.recovered_job_ids
        session_b.execute(text("SET LOCAL lock_timeout = '2s'"))
        result_b = recover_stale_webhook_delivery_jobs(
            session_b,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=2,
        )
        result_b_ids = result_b.recovered_job_ids

        assert result_a_ids == expected_ids[:2]
        assert result_b_ids == expected_ids[2:]
        assert set(result_a_ids).isdisjoint(result_b_ids)
        assert set(result_a_ids + result_b_ids) == set(expected_ids)
        session_b.commit()
        session_a.commit()
    finally:
        if session_b.in_transaction():
            session_b.rollback()
        if session_a.in_transaction():
            session_a.rollback()
        session_b.close()
        session_a.close()

    committed = _job_states(created_records.job_ids)
    assert result_a_ids == expected_ids[:2]
    assert result_b_ids == expected_ids[2:]
    for job_id in expected_ids:
        assert committed[job_id].status == "pending"
        assert committed[job_id].next_attempt_at == recovered_at
        assert committed[job_id].updated_at == recovered_at
    assert _attempt_count(created_records.event_ids) == 0


def test_recovery_skips_stale_job_locked_by_another_transaction(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 14, 0, tzinfo=UTC)
    recovered_at = stale_before + timedelta(hours=1)
    first = _persist_job(
        created_records,
        label="manually-locked",
        status="processing",
        next_attempt_at=stale_before - timedelta(hours=2),
        created_at=stale_before - timedelta(hours=4),
        updated_at=stale_before - timedelta(hours=3),
    )
    second = _persist_job(
        created_records,
        label="not-locked",
        status="processing",
        next_attempt_at=stale_before - timedelta(hours=2),
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=2),
    )
    initial = _job_states(created_records.job_ids)
    locking_session = SessionFactory()
    recovery_session = SessionFactory()
    recovered_ids: tuple[uuid.UUID, ...] = ()
    try:
        locked_job = locking_session.scalar(
            select(WebhookDeliveryJob)
            .where(WebhookDeliveryJob.id == first.job_id)
            .with_for_update()
        )
        assert locked_job is not None
        recovery_session.execute(text("SET LOCAL lock_timeout = '2s'"))
        result = recover_stale_webhook_delivery_jobs(
            recovery_session,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=1,
        )
        recovered_ids = result.recovered_job_ids
        assert recovered_ids == (second.job_id,)
        assert first.job_id not in recovered_ids
        assert _get_job(recovery_session, second.job_id).status == "pending"
        recovery_session.commit()
        locking_session.rollback()
    finally:
        if recovery_session.in_transaction():
            recovery_session.rollback()
        if locking_session.in_transaction():
            locking_session.rollback()
        recovery_session.close()
        locking_session.close()

    committed = _job_states(created_records.job_ids)
    assert committed[first.job_id] == initial[first.job_id]
    assert committed[second.job_id].status == "pending"
    assert committed[second.job_id].next_attempt_at == recovered_at
    assert committed[second.job_id].updated_at == recovered_at
    assert _attempt_count(created_records.event_ids) == 0


def test_recovery_with_no_stale_processing_jobs_performs_no_writes(
    created_records: _CreatedRecords,
) -> None:
    stale_before = datetime(2026, 7, 30, 15, 0, tzinfo=UTC)
    recovered_at = stale_before + timedelta(hours=1)
    _persist_job(
        created_records,
        label="no-eligible-fresh",
        status="processing",
        next_attempt_at=stale_before + timedelta(hours=2),
        created_at=stale_before - timedelta(hours=2),
        updated_at=stale_before + timedelta(seconds=1),
    )
    _persist_job(
        created_records,
        label="no-eligible-pending",
        status="pending",
        next_attempt_at=stale_before - timedelta(hours=1),
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=2),
    )
    _persist_job(
        created_records,
        label="no-eligible-succeeded",
        status="succeeded",
        next_attempt_at=None,
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=2),
    )
    _persist_job(
        created_records,
        label="no-eligible-dead-letter",
        status="dead_letter",
        next_attempt_at=None,
        created_at=stale_before - timedelta(hours=3),
        updated_at=stale_before - timedelta(hours=2),
    )
    initial_jobs = _job_states(created_records.job_ids)
    initial_attempt_count = _attempt_count(created_records.event_ids)
    initial_parents = _parent_states(created_records)

    with SessionFactory() as session:
        result = recover_stale_webhook_delivery_jobs(
            session,
            stale_before=stale_before,
            recovered_at=recovered_at,
            limit=10,
        )
        assert result.recovered_job_ids == ()
        assert result.recovered_count == 0
        session.commit()

    assert _job_states(created_records.job_ids) == initial_jobs
    assert _attempt_count(created_records.event_ids) == initial_attempt_count == 0
    assert _parent_states(created_records) == initial_parents
