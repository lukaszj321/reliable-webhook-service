import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import WebhookHttpResponse
from reliable_webhook_service.delivery_job_execution_service import (
    execute_webhook_delivery_job,
)
from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs
from reliable_webhook_service.delivery_service import execute_webhook_delivery
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.replay_service import replay_webhook_event

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0
TIMEOUT_SECONDS = 2.5


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    target_url: str
    payload: dict[str, JsonValue]
    timeout_seconds: float


class _SequencedHttpClient:
    def __init__(self, *status_codes: int) -> None:
        self._status_codes = iter(status_codes)
        self.requests: list[_RecordedRequest] = []

    def post_json(
        self,
        *,
        target_url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
    ) -> WebhookHttpResponse:
        self.requests.append(
            _RecordedRequest(
                target_url=target_url,
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
        )
        return WebhookHttpResponse(status_code=next(self._status_codes))


@dataclass(slots=True)
class _CreatedRecords:
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedHistory:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    target_url: str
    payload: dict[str, JsonValue]
    original_attempt_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class _ExecutionSnapshot:
    global_attempt_number: int
    attempt_outcome: str
    attempt_count: int
    status: str
    next_attempt_at: datetime | None


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
def created_records() -> Iterator[_CreatedRecords]:
    with SessionFactory() as session:
        records = _CreatedRecords(initial_counts=_table_counts(session))

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
            assert _table_counts(session) == records.initial_counts


def _persist_terminal_history(
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    history_count: int,
) -> _PersistedHistory:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/replay-cycle/{marker}/{label}"
    payload: dict[str, JsonValue] = {
        "replay_cycle_marker": str(marker),
        "label": label,
    }
    original_attempt_ids = tuple(uuid.uuid4() for _ in range(history_count))
    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(job_id)
    records.attempt_ids.extend(original_attempt_ids)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Replay cycle {marker} {label}",
                target_url=target_url,
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="replay.cycle.integration",
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
                attempt_count=history_count,
            )
        )
        session.flush()
        for attempt_number, attempt_id in enumerate(original_attempt_ids, start=1):
            session.add(
                WebhookDeliveryAttempt(
                    id=attempt_id,
                    event_id=event_id,
                    attempt_number=attempt_number,
                    outcome="failed",
                    target_url=target_url,
                    response_status_code=503,
                    error_message="HTTP response returned status 503",
                    duration_ms=attempt_number,
                    attempted_at=datetime(2026, 7, 30, 8, attempt_number, tzinfo=UTC),
                )
            )
        session.commit()

    return _PersistedHistory(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=job_id,
        target_url=target_url,
        payload=payload,
        original_attempt_ids=original_attempt_ids,
    )


def _attempts(event_id: uuid.UUID) -> list[WebhookDeliveryAttempt]:
    with SessionFactory() as session:
        return list(
            session.scalars(
                select(WebhookDeliveryAttempt)
                .where(WebhookDeliveryAttempt.event_id == event_id)
                .order_by(WebhookDeliveryAttempt.attempt_number)
            ).all()
        )


def _job(job_id: uuid.UUID) -> WebhookDeliveryJob:
    with SessionFactory() as session:
        job = session.get(WebhookDeliveryJob, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _replay_and_commit(persisted: _PersistedHistory, replayed_at: datetime) -> None:
    attempts_before = [
        (attempt.id, attempt.attempt_number) for attempt in _attempts(persisted.event_id)
    ]
    with SessionFactory() as session:
        result = replay_webhook_event(
            session,
            event_id=persisted.event_id,
            replayed_at=replayed_at,
        )
        assert result.event_id == persisted.event_id
        assert result.delivery_job_id == persisted.job_id
        session.commit()

    job = _job(persisted.job_id)
    assert job.status == "pending"
    assert job.attempt_count == 0
    assert job.next_attempt_at == replayed_at
    assert [(attempt.id, attempt.attempt_number) for attempt in _attempts(persisted.event_id)] == (
        attempts_before
    )


def _claim_and_execute(
    persisted: _PersistedHistory,
    *,
    client: _SequencedHttpClient,
    execution_at: datetime,
) -> _ExecutionSnapshot:
    with SessionFactory() as claim_session:
        claimed = claim_due_webhook_delivery_jobs(
            claim_session,
            claimed_at=execution_at,
            limit=1,
        )
        assert [job.id for job in claimed] == [persisted.job_id]
        claim_session.commit()

    with SessionFactory() as execution_session:
        result = execute_webhook_delivery_job(
            execution_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=MAX_ATTEMPTS,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: execution_at,
            decision_now=lambda: execution_at,
            monotonic_ns=iter([1_000_000_000, 1_010_000_000]).__next__,
        )
        records_attempt_id = result.attempt.id
        execution_session.commit()

    assert isinstance(records_attempt_id, uuid.UUID)
    return _ExecutionSnapshot(
        global_attempt_number=result.attempt.attempt_number,
        attempt_outcome=result.attempt.outcome,
        attempt_count=result.job.attempt_count,
        status=result.job.status,
        next_attempt_at=result.job.next_attempt_at,
    )


def _record_new_attempt_ids(
    records: _CreatedRecords,
    persisted: _PersistedHistory,
) -> None:
    existing = set(records.attempt_ids)
    records.attempt_ids.extend(
        attempt.id for attempt in _attempts(persisted.event_id) if attempt.id not in existing
    )


def test_replay_restores_complete_retry_budget_after_dead_letter(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_history(
        created_records,
        label="fresh-failure-budget",
        status="dead_letter",
        history_count=MAX_ATTEMPTS,
    )
    replayed_at = datetime(1800, 1, 1, tzinfo=UTC)
    client = _SequencedHttpClient(503, 503, 503)

    try:
        _replay_and_commit(persisted, replayed_at)

        first = _claim_and_execute(persisted, client=client, execution_at=replayed_at)
        assert first == _ExecutionSnapshot(
            global_attempt_number=4,
            attempt_outcome="failed",
            attempt_count=1,
            status="pending",
            next_attempt_at=replayed_at + timedelta(seconds=5),
        )

        second = _claim_and_execute(
            persisted,
            client=client,
            execution_at=first.next_attempt_at,
        )
        assert second == _ExecutionSnapshot(
            global_attempt_number=5,
            attempt_outcome="failed",
            attempt_count=2,
            status="pending",
            next_attempt_at=replayed_at + timedelta(seconds=15),
        )

        third = _claim_and_execute(
            persisted,
            client=client,
            execution_at=second.next_attempt_at,
        )
        assert third == _ExecutionSnapshot(
            global_attempt_number=6,
            attempt_outcome="failed",
            attempt_count=3,
            status="dead_letter",
            next_attempt_at=None,
        )

        assert [attempt.attempt_number for attempt in _attempts(persisted.event_id)] == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]
        assert len(client.requests) == MAX_ATTEMPTS
        with SessionFactory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(WebhookEvent)
                    .where(WebhookEvent.id == persisted.event_id)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(WebhookDeliveryJob)
                    .where(WebhookDeliveryJob.event_id == persisted.event_id)
                )
                == 1
            )
    finally:
        _record_new_attempt_ids(created_records, persisted)


def test_replay_cycle_can_fail_then_succeed_with_global_history_preserved(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_history(
        created_records,
        label="failure-then-success",
        status="succeeded",
        history_count=2,
    )
    replayed_at = datetime(1800, 2, 1, tzinfo=UTC)
    client = _SequencedHttpClient(503, 204)

    try:
        _replay_and_commit(persisted, replayed_at)
        first = _claim_and_execute(persisted, client=client, execution_at=replayed_at)
        assert first == _ExecutionSnapshot(
            global_attempt_number=3,
            attempt_outcome="failed",
            attempt_count=1,
            status="pending",
            next_attempt_at=replayed_at + timedelta(seconds=5),
        )

        second = _claim_and_execute(
            persisted,
            client=client,
            execution_at=first.next_attempt_at,
        )
        assert second == _ExecutionSnapshot(
            global_attempt_number=4,
            attempt_outcome="succeeded",
            attempt_count=2,
            status="succeeded",
            next_attempt_at=None,
        )
        assert [attempt.attempt_number for attempt in _attempts(persisted.event_id)] == [
            1,
            2,
            3,
            4,
        ]
        assert len(client.requests) == 2
    finally:
        _record_new_attempt_ids(created_records, persisted)


def test_manual_delivery_between_worker_attempts_does_not_consume_retry_budget(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_terminal_history(
        created_records,
        label="worker-manual-worker",
        status="dead_letter",
        history_count=3,
    )
    replayed_at = datetime(1800, 3, 1, tzinfo=UTC)
    client = _SequencedHttpClient(503, 204, 503)

    try:
        _replay_and_commit(persisted, replayed_at)
        first_worker = _claim_and_execute(
            persisted,
            client=client,
            execution_at=replayed_at,
        )
        assert first_worker.global_attempt_number == 4
        assert first_worker.attempt_count == 1
        assert first_worker.status == "pending"
        assert first_worker.next_attempt_at == replayed_at + timedelta(seconds=5)

        job_before_manual = _job(persisted.job_id)
        with SessionFactory() as manual_session:
            manual_attempt = execute_webhook_delivery(
                manual_session,
                event_id=persisted.event_id,
                http_client=client,
                timeout_seconds=TIMEOUT_SECONDS,
                utc_now=lambda: replayed_at + timedelta(seconds=1),
                monotonic_ns=iter([2_000_000_000, 2_010_000_000]).__next__,
            )
            manual_session.commit()
        assert manual_attempt.attempt_number == 5

        job_after_manual = _job(persisted.job_id)
        assert job_after_manual.status == job_before_manual.status == "pending"
        assert job_after_manual.attempt_count == job_before_manual.attempt_count == 1
        assert job_after_manual.next_attempt_at == job_before_manual.next_attempt_at

        second_worker = _claim_and_execute(
            persisted,
            client=client,
            execution_at=first_worker.next_attempt_at,
        )
        assert second_worker == _ExecutionSnapshot(
            global_attempt_number=6,
            attempt_outcome="failed",
            attempt_count=2,
            status="pending",
            next_attempt_at=replayed_at + timedelta(seconds=15),
        )
        assert [attempt.attempt_number for attempt in _attempts(persisted.event_id)] == [
            1,
            2,
            3,
            4,
            5,
            6,
        ]
        assert len(client.requests) == 3
    finally:
        _record_new_attempt_ids(created_records, persisted)
