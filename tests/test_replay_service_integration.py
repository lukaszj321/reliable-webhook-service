import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from queue import Queue
from threading import Thread

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.replay_service import (
    WebhookReplayDeliveryJobNotReplayableError,
    WebhookReplayResult,
    replay_webhook_event,
)


@dataclass(slots=True)
class _CreatedRecords:
    marker: uuid.UUID
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedReplayData:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    attempt_ids: tuple[uuid.UUID, ...]
    status: str
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _table_counts(session: Session) -> tuple[int, int, int, int]:
    counts = session.execute(
        select(
            select(func.count()).select_from(WebhookEndpoint).scalar_subquery(),
            select(func.count()).select_from(WebhookEvent).scalar_subquery(),
            select(func.count()).select_from(WebhookDeliveryJob).scalar_subquery(),
            select(func.count()).select_from(WebhookDeliveryAttempt).scalar_subquery(),
        )
    ).one()
    return tuple(counts)


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    with SessionFactory() as session:
        initial_counts = _table_counts(session)

    records = _CreatedRecords(
        marker=uuid.uuid4(),
        initial_counts=initial_counts,
    )
    try:
        yield records
    finally:
        with SessionFactory() as session:
            session.rollback()
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
            assert _table_counts(session) == records.initial_counts

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


def _persist_terminal_job(
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    attempt_count: int = 4,
    attempt_total: int = 2,
) -> _PersistedReplayData:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    attempt_ids = tuple(uuid.uuid4() for _ in range(attempt_total))
    created_at = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    updated_at = datetime(2026, 7, 29, 8, 1, tzinfo=UTC)
    payload: dict[str, JsonValue] = {
        "replay_integration_marker": str(records.marker),
        "label": label,
    }

    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(job_id)
    records.attempt_ids.extend(attempt_ids)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Replay integration {records.marker} {label}",
                target_url=f"https://example.test/replay/{records.marker}/{label}",
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="replay.integration",
                payload=payload,
            )
        )
        session.flush()
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status=status,
                next_attempt_at=None,
                attempt_count=attempt_count,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.flush()
        for attempt_number, attempt_id in enumerate(attempt_ids, start=1):
            session.add(
                WebhookDeliveryAttempt(
                    id=attempt_id,
                    event_id=event_id,
                    attempt_number=attempt_number,
                    outcome="failed",
                    target_url=f"https://example.test/replay/{records.marker}/{label}",
                    response_status_code=503,
                    error_message="HTTP response returned status 503",
                    duration_ms=attempt_number,
                    attempted_at=created_at + timedelta(minutes=attempt_number),
                )
            )
        session.commit()

    return _PersistedReplayData(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=job_id,
        attempt_ids=attempt_ids,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=None,
        created_at=created_at,
        updated_at=updated_at,
    )


def _get_job(session: Session, job_id: uuid.UUID) -> WebhookDeliveryJob:
    job = session.get(WebhookDeliveryJob, job_id)
    assert job is not None
    return job


def _attempt_snapshot(
    session: Session,
    event_id: uuid.UUID,
) -> list[tuple[uuid.UUID, int, str, int | None, str | None]]:
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
    return [tuple(row) for row in rows]


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


@pytest.mark.parametrize("terminal_status", ["succeeded", "dead_letter"])
def test_commit_replays_terminal_job_atomically(
    created_records: _CreatedRecords,
    terminal_status: str,
) -> None:
    persisted = _persist_terminal_job(
        created_records,
        label=f"commit-{terminal_status}",
        status=terminal_status,
    )
    replayed_at = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

    with SessionFactory() as before_session:
        counts_before = _table_counts(before_session)
        attempts_before = _attempt_snapshot(before_session, persisted.event_id)

    with SessionFactory() as replay_session:
        result = replay_webhook_event(
            replay_session,
            event_id=persisted.event_id,
            replayed_at=replayed_at,
        )

        assert result == WebhookReplayResult(
            event_id=persisted.event_id,
            delivery_job_id=persisted.job_id,
            status="pending",
            next_attempt_at=replayed_at,
        )
        replayed_job = _get_job(replay_session, persisted.job_id)
        assert replayed_job.status == "pending"
        assert replayed_job.attempt_count == 0
        assert _as_utc(replayed_job.next_attempt_at) == replayed_at
        assert _as_utc(replayed_job.updated_at) == replayed_at

        with SessionFactory() as observer_session:
            observed_job = _get_job(observer_session, persisted.job_id)
            assert observed_job.status == terminal_status
            assert observed_job.attempt_count == persisted.attempt_count
            assert observed_job.next_attempt_at == persisted.next_attempt_at
            assert _as_utc(observed_job.updated_at) == persisted.updated_at
            observer_session.rollback()

        replay_session.commit()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.id == persisted.job_id
        assert stored_job.event_id == persisted.event_id
        assert _as_utc(stored_job.created_at) == persisted.created_at
        assert stored_job.status == "pending"
        assert stored_job.attempt_count == 0
        assert _as_utc(stored_job.next_attempt_at) == replayed_at
        assert _as_utc(stored_job.updated_at) == replayed_at
        assert _attempt_snapshot(verification_session, persisted.event_id) == attempts_before
        assert _table_counts(verification_session) == counts_before


def test_rollback_restores_complete_terminal_job_state(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_job(
        created_records,
        label="rollback",
        status="dead_letter",
        attempt_count=7,
    )
    replayed_at = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)

    with SessionFactory() as before_session:
        attempts_before = _attempt_snapshot(before_session, persisted.event_id)

    with SessionFactory() as replay_session:
        result = replay_webhook_event(
            replay_session,
            event_id=persisted.event_id,
            replayed_at=replayed_at,
        )
        assert result.status == "pending"
        assert _get_job(replay_session, persisted.job_id).attempt_count == 0
        replay_session.rollback()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.status == persisted.status
        assert stored_job.attempt_count == persisted.attempt_count
        assert stored_job.next_attempt_at == persisted.next_attempt_at
        assert _as_utc(stored_job.updated_at) == persisted.updated_at
        assert _attempt_snapshot(verification_session, persisted.event_id) == attempts_before


def test_replayed_job_can_be_claimed_by_existing_service(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_job(
        created_records,
        label="claimable",
        status="succeeded",
    )
    replayed_at = datetime(1900, 1, 1, tzinfo=UTC)

    with SessionFactory() as replay_session:
        replay_webhook_event(
            replay_session,
            event_id=persisted.event_id,
            replayed_at=replayed_at,
        )
        replay_session.commit()

    with SessionFactory() as claim_session:
        claimed_jobs = claim_due_webhook_delivery_jobs(
            claim_session,
            claimed_at=replayed_at,
            limit=1000,
        )
        matching_jobs = [job for job in claimed_jobs if job.id == persisted.job_id]
        assert len(matching_jobs) == 1
        assert matching_jobs[0].status == "processing"
        assert matching_jobs[0].attempt_count == 0
        claim_session.rollback()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.status == "pending"
        assert stored_job.attempt_count == 0
        assert _as_utc(stored_job.next_attempt_at) == replayed_at


def _wait_for_transaction_lock(backend_pid: int) -> tuple[str, str]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with SessionFactory() as observer_session:
            wait_state = observer_session.execute(
                text(
                    """
                    SELECT wait_event_type, wait_event
                    FROM pg_stat_activity
                    WHERE pid = :backend_pid
                    """
                ),
                {"backend_pid": backend_pid},
            ).one_or_none()
        if wait_state == ("Lock", "transactionid"):
            return wait_state
        time.sleep(0.02)

    raise AssertionError("Second replay session did not wait on the PostgreSQL row lock")


def test_concurrent_replay_waits_for_row_lock_then_rejects_active_job(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_job(
        created_records,
        label="concurrent",
        status="dead_letter",
    )
    first_replayed_at = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
    second_replayed_at = datetime(2026, 7, 29, 14, 1, tzinfo=UTC)
    backend_pids: Queue[int] = Queue()
    outcomes: Queue[WebhookReplayResult | BaseException] = Queue()

    def run_second_replay() -> None:
        with SessionFactory() as second_session:
            backend_pid = second_session.scalar(text("SELECT pg_backend_pid()"))
            assert backend_pid is not None
            backend_pids.put(backend_pid)
            try:
                outcomes.put(
                    replay_webhook_event(
                        second_session,
                        event_id=persisted.event_id,
                        replayed_at=second_replayed_at,
                    )
                )
            except BaseException as error:
                outcomes.put(error)
            finally:
                second_session.rollback()

    first_session = SessionFactory()
    second_thread = Thread(
        target=run_second_replay,
        name=f"replay-row-lock-{uuid.uuid4()}",
    )
    try:
        first_result = replay_webhook_event(
            first_session,
            event_id=persisted.event_id,
            replayed_at=first_replayed_at,
        )
        assert first_result.status == "pending"

        second_thread.start()
        backend_pid = backend_pids.get(timeout=5)
        assert _wait_for_transaction_lock(backend_pid) == ("Lock", "transactionid")
        assert second_thread.is_alive()

        first_session.commit()
        second_thread.join(timeout=5)
        assert not second_thread.is_alive()
    finally:
        if first_session.in_transaction():
            first_session.rollback()
        first_session.close()
        if second_thread.is_alive():
            second_thread.join(timeout=5)

    outcome = outcomes.get(timeout=1)
    assert isinstance(outcome, WebhookReplayDeliveryJobNotReplayableError)
    assert str(outcome) == "Webhook delivery job is not replayable"
    assert outcomes.empty()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.status == "pending"
        assert stored_job.attempt_count == 0
        assert _as_utc(stored_job.next_attempt_at) == first_replayed_at
        assert _as_utc(stored_job.updated_at) == first_replayed_at
