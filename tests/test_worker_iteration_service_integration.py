import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import WebhookHttpResponse
from reliable_webhook_service.delivery_service import InactiveWebhookEndpointError
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.worker_iteration_service import (
    WebhookWorkerIterationResult,
    run_webhook_worker_iteration,
)

TIMEOUT_SECONDS = 4.5
MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    target_url: str
    payload: dict[str, JsonValue]
    timeout_seconds: float


class _FakeHttpClient:
    def __init__(
        self,
        *,
        responses: list[WebhookHttpResponse],
        on_request: Callable[[int, _RecordedRequest], None] | None = None,
    ) -> None:
        self._responses = responses
        self._on_request = on_request
        self.requests: list[_RecordedRequest] = []

    def post_json(
        self,
        *,
        target_url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
    ) -> WebhookHttpResponse:
        request = _RecordedRequest(
            target_url=target_url,
            payload=payload,
            timeout_seconds=timeout_seconds,
        )
        request_index = len(self.requests)
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(request_index, request)
        if request_index >= len(self._responses):
            raise AssertionError("Unexpected HTTP request")
        return self._responses[request_index]


@dataclass(slots=True)
class _CreatedRecords:
    marker: uuid.UUID = field(default_factory=uuid.uuid4)
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    target_url: str
    payload: dict[str, JsonValue]
    status: str
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


@contextmanager
def _isolated_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords()
    try:
        yield records
    finally:
        _cleanup_records(records)


def _cleanup_records(records: _CreatedRecords) -> None:
    with SessionFactory() as session:
        if records.event_ids:
            attempts = list(
                session.scalars(
                    select(WebhookDeliveryAttempt).where(
                        WebhookDeliveryAttempt.event_id.in_(records.event_ids)
                    )
                ).all()
            )
            for attempt in attempts:
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
                WebhookEvent.payload["worker_iteration_integration_marker"].as_string()
                == str(records.marker)
            )
        )
        assert marker_count == 0


def _persist_job(
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    next_attempt_at: datetime,
    created_at: datetime,
    updated_at: datetime,
    is_active: bool = True,
    job_id: uuid.UUID | None = None,
) -> _PersistedJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    resolved_job_id = uuid.uuid4() if job_id is None else job_id
    target_url = f"https://example.test/worker-iteration/{records.marker}/{label}"
    payload: dict[str, JsonValue] = {
        "worker_iteration_integration_marker": str(records.marker),
        "label": label,
    }
    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(resolved_job_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Worker iteration {records.marker} {label}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=f"delivery.worker-iteration.integration.{label}",
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
        payload=payload,
        status=status,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _run_iteration(
    *,
    client: _FakeHttpClient,
    iteration_at: datetime,
    stale_before: datetime,
    recovery_limit: int,
    processing_limit: int,
    attempted_at: datetime,
    decision_at: datetime,
) -> WebhookWorkerIterationResult:
    monotonic_values = iter(range(1_000_000_000, 10_000_000_000, 5_000_000))
    return run_webhook_worker_iteration(
        session_factory=SessionFactory,
        http_client=client,
        iteration_at=iteration_at,
        stale_before=stale_before,
        recovery_limit=recovery_limit,
        processing_limit=processing_limit,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=lambda: attempted_at,
        decision_now=lambda: decision_at,
        monotonic_ns=monotonic_values.__next__,
    )


def _get_job(session: Session, job_id: uuid.UUID) -> WebhookDeliveryJob:
    job = session.get(WebhookDeliveryJob, job_id)
    assert job is not None
    return job


def _attempts_for_event(
    session: Session,
    event_id: uuid.UUID,
) -> list[WebhookDeliveryAttempt]:
    statement = (
        select(WebhookDeliveryAttempt)
        .where(WebhookDeliveryAttempt.event_id == event_id)
        .order_by(WebhookDeliveryAttempt.attempt_number)
    )
    return list(session.scalars(statement).all())


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


def _idle_in_transaction_count() -> int:
    with SessionFactory() as session:
        count = session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND state = 'idle in transaction' "
                "AND pid <> pg_backend_pid()"
            )
        )
    assert count is not None
    return count


def test_worker_iteration_recovers_and_processes_stale_job_in_same_iteration() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(
            2026,
            8,
            1,
            14,
            0,
            tzinfo=timezone(timedelta(hours=2)),
        )
        expected_iteration_at = iteration_at.astimezone(UTC)
        stale_before = datetime(2026, 8, 1, 11, 30, tzinfo=UTC)
        persisted = _persist_job(
            records,
            label="same-iteration",
            status="processing",
            next_attempt_at=stale_before - timedelta(hours=1),
            created_at=stale_before - timedelta(hours=3),
            updated_at=stale_before - timedelta(minutes=1),
        )
        lock_was_obtained = False

        def verify_recovery_lock_released(
            request_index: int,
            request: _RecordedRequest,
        ) -> None:
            nonlocal lock_was_obtained
            assert request_index == 0
            assert request.target_url == persisted.target_url
            observer_session = SessionFactory()
            try:
                locked_job = observer_session.scalar(
                    select(WebhookDeliveryJob)
                    .where(WebhookDeliveryJob.id == persisted.job_id)
                    .with_for_update(nowait=True)
                )
                assert locked_job is not None
                assert locked_job.status == "processing"
                assert locked_job.next_attempt_at is not None
                assert _as_utc(locked_job.next_attempt_at) == expected_iteration_at
                lock_was_obtained = True
            finally:
                if observer_session.in_transaction():
                    observer_session.rollback()
                observer_session.close()

        client = _FakeHttpClient(
            responses=[WebhookHttpResponse(status_code=204)],
            on_request=verify_recovery_lock_released,
        )

        result = _run_iteration(
            client=client,
            iteration_at=iteration_at,
            stale_before=stale_before,
            recovery_limit=1,
            processing_limit=1,
            attempted_at=expected_iteration_at + timedelta(minutes=1),
            decision_at=expected_iteration_at + timedelta(minutes=2),
        )

        assert lock_was_obtained is True
        assert result.recovery.recovered_job_ids == (persisted.job_id,)
        assert result.recovered_count == 1
        assert result.processing.claimed_job_ids == (persisted.job_id,)
        assert tuple(item.job_id for item in result.processing.completed_jobs) == (
            persisted.job_id,
        )
        assert result.claimed_count == 1
        assert result.completed_count == 1
        assert client.requests == [
            _RecordedRequest(
                target_url=persisted.target_url,
                payload=persisted.payload,
                timeout_seconds=TIMEOUT_SECONDS,
            )
        ]

        with SessionFactory() as verification_session:
            job = _get_job(verification_session, persisted.job_id)
            attempts = _attempts_for_event(verification_session, persisted.event_id)
            assert job.status == "succeeded"
            assert job.next_attempt_at is None
            assert len(attempts) == 1
            assert attempts[0].event_id == persisted.event_id
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].id == result.processing.completed_jobs[0].attempt_id


def test_worker_iteration_processes_recovered_and_due_pending_jobs_in_claim_order() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
        stale_before = iteration_at - timedelta(hours=1)
        due_first = _persist_job(
            records,
            label="due-first",
            status="pending",
            next_attempt_at=iteration_at - timedelta(minutes=10),
            created_at=iteration_at - timedelta(hours=4),
            updated_at=iteration_at - timedelta(hours=2),
        )
        recovered = _persist_job(
            records,
            label="recovered-second",
            status="processing",
            next_attempt_at=iteration_at - timedelta(hours=2),
            created_at=iteration_at - timedelta(hours=3),
            updated_at=stale_before - timedelta(minutes=1),
        )
        due_third = _persist_job(
            records,
            label="due-third",
            status="pending",
            next_attempt_at=iteration_at,
            created_at=iteration_at - timedelta(hours=2),
            updated_at=iteration_at - timedelta(hours=1),
        )
        expected_claimed = (due_first.job_id, recovered.job_id)
        client = _FakeHttpClient(
            responses=[
                WebhookHttpResponse(status_code=200),
                WebhookHttpResponse(status_code=204),
            ]
        )

        result = _run_iteration(
            client=client,
            iteration_at=iteration_at,
            stale_before=stale_before,
            recovery_limit=1,
            processing_limit=2,
            attempted_at=iteration_at + timedelta(minutes=1),
            decision_at=iteration_at + timedelta(minutes=2),
        )

        assert result.recovery.recovered_job_ids == (recovered.job_id,)
        assert result.processing.claimed_job_ids == expected_claimed
        assert tuple(item.job_id for item in result.processing.completed_jobs) == expected_claimed
        assert result.recovered_count == 1
        assert result.claimed_count == 2
        assert result.completed_count == 2
        assert due_third.job_id not in result.processing.claimed_job_ids
        assert [request.target_url for request in client.requests] == [
            due_first.target_url,
            recovered.target_url,
        ]

        with SessionFactory() as verification_session:
            stored_jobs = [
                _get_job(verification_session, persisted.job_id)
                for persisted in (due_first, recovered, due_third)
            ]
            attempts = [
                _attempts_for_event(verification_session, persisted.event_id)
                for persisted in (due_first, recovered, due_third)
            ]
            assert [job.status for job in stored_jobs] == [
                "succeeded",
                "succeeded",
                "pending",
            ]
            assert [len(event_attempts) for event_attempts in attempts] == [1, 1, 0]
            assert stored_jobs[2].next_attempt_at is not None
            assert _as_utc(stored_jobs[2].next_attempt_at) == iteration_at
            assert _as_utc(stored_jobs[2].updated_at) == due_third.updated_at


def test_worker_iteration_applies_recovery_and_processing_limits_independently() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)
        stale_before = iteration_at - timedelta(hours=1)
        jobs = [
            _persist_job(
                records,
                label=f"independent-limits-{index}",
                status="processing",
                next_attempt_at=iteration_at - timedelta(hours=3),
                created_at=iteration_at - timedelta(hours=6 - index),
                updated_at=stale_before - timedelta(minutes=3 - index),
            )
            for index in range(3)
        ]
        client = _FakeHttpClient(responses=[WebhookHttpResponse(status_code=204)])

        result = _run_iteration(
            client=client,
            iteration_at=iteration_at,
            stale_before=stale_before,
            recovery_limit=2,
            processing_limit=1,
            attempted_at=iteration_at + timedelta(minutes=1),
            decision_at=iteration_at + timedelta(minutes=2),
        )

        assert result.recovery.recovered_job_ids == (jobs[0].job_id, jobs[1].job_id)
        assert result.processing.claimed_job_ids == (jobs[0].job_id,)
        assert tuple(item.job_id for item in result.processing.completed_jobs) == (jobs[0].job_id,)
        assert result.recovered_count == 2
        assert result.claimed_count == 1
        assert result.completed_count == 1
        assert client.requests == [
            _RecordedRequest(
                target_url=jobs[0].target_url,
                payload=jobs[0].payload,
                timeout_seconds=TIMEOUT_SECONDS,
            )
        ]

        with SessionFactory() as verification_session:
            stored_jobs = [_get_job(verification_session, job.job_id) for job in jobs]
            attempts = [_attempts_for_event(verification_session, job.event_id) for job in jobs]
            assert [job.status for job in stored_jobs] == [
                "succeeded",
                "pending",
                "processing",
            ]
            assert [len(event_attempts) for event_attempts in attempts] == [1, 0, 0]
            assert stored_jobs[1].next_attempt_at is not None
            assert _as_utc(stored_jobs[1].next_attempt_at) == iteration_at
            assert _as_utc(stored_jobs[1].updated_at) == iteration_at
            assert stored_jobs[2].next_attempt_at is not None
            assert _as_utc(stored_jobs[2].next_attempt_at) == jobs[2].next_attempt_at
            assert _as_utc(stored_jobs[2].updated_at) == jobs[2].updated_at


def test_worker_iteration_with_empty_recovery_still_processes_due_pending_job() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
        stale_before = iteration_at - timedelta(hours=1)
        persisted = _persist_job(
            records,
            label="empty-recovery",
            status="pending",
            next_attempt_at=iteration_at - timedelta(minutes=1),
            created_at=iteration_at - timedelta(hours=2),
            updated_at=iteration_at - timedelta(hours=1),
        )
        client = _FakeHttpClient(responses=[WebhookHttpResponse(status_code=200)])

        result = _run_iteration(
            client=client,
            iteration_at=iteration_at,
            stale_before=stale_before,
            recovery_limit=1,
            processing_limit=1,
            attempted_at=iteration_at + timedelta(minutes=1),
            decision_at=iteration_at + timedelta(minutes=2),
        )

        assert result.recovery.recovered_job_ids == ()
        assert result.recovered_count == 0
        assert result.processing.claimed_job_ids == (persisted.job_id,)
        assert tuple(item.job_id for item in result.processing.completed_jobs) == (
            persisted.job_id,
        )
        assert result.claimed_count == 1
        assert result.completed_count == 1
        assert len(client.requests) == 1

        with SessionFactory() as verification_session:
            job = _get_job(verification_session, persisted.job_id)
            attempts = _attempts_for_event(verification_session, persisted.event_id)
            assert job.status == "succeeded"
            assert job.next_attempt_at is None
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"


def test_worker_iteration_processing_failure_does_not_rollback_committed_recovery() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 1, 16, 0, tzinfo=UTC)
        stale_before = iteration_at - timedelta(hours=1)
        failing = _persist_job(
            records,
            label="failing-inactive",
            status="processing",
            next_attempt_at=iteration_at - timedelta(hours=3),
            created_at=iteration_at - timedelta(hours=5),
            updated_at=stale_before - timedelta(minutes=2),
            is_active=False,
        )
        untouched = _persist_job(
            records,
            label="recovered-not-claimed",
            status="processing",
            next_attempt_at=iteration_at - timedelta(hours=2),
            created_at=iteration_at - timedelta(hours=4),
            updated_at=stale_before - timedelta(minutes=1),
        )
        client = _FakeHttpClient(responses=[])

        with pytest.raises(
            InactiveWebhookEndpointError,
            match="^Webhook endpoint is inactive$",
        ) as error_info:
            _run_iteration(
                client=client,
                iteration_at=iteration_at,
                stale_before=stale_before,
                recovery_limit=2,
                processing_limit=1,
                attempted_at=iteration_at + timedelta(minutes=1),
                decision_at=iteration_at + timedelta(minutes=2),
            )

        assert type(error_info.value) is InactiveWebhookEndpointError
        assert client.requests == []

        with SessionFactory() as verification_session:
            failing_job = _get_job(verification_session, failing.job_id)
            untouched_job = _get_job(verification_session, untouched.job_id)
            failing_attempts = _attempts_for_event(verification_session, failing.event_id)
            untouched_attempts = _attempts_for_event(verification_session, untouched.event_id)
            assert failing_job.status == "processing"
            assert failing_job.next_attempt_at is not None
            assert _as_utc(failing_job.next_attempt_at) == iteration_at
            assert _as_utc(failing_job.updated_at) == iteration_at
            assert untouched_job.status == "pending"
            assert untouched_job.next_attempt_at is not None
            assert _as_utc(untouched_job.next_attempt_at) == iteration_at
            assert _as_utc(untouched_job.updated_at) == iteration_at
            assert failing_attempts == []
            assert untouched_attempts == []

        assert _idle_in_transaction_count() == 0


def test_worker_iteration_preserves_completed_job_when_later_job_fails() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 1, 17, 0, tzinfo=UTC)
        stale_before = iteration_at - timedelta(hours=1)
        jobs = [
            _persist_job(
                records,
                label=label,
                status="processing",
                next_attempt_at=iteration_at - timedelta(hours=3),
                created_at=iteration_at - timedelta(hours=6 - index),
                updated_at=stale_before - timedelta(minutes=3 - index),
                is_active=is_active,
            )
            for index, (label, is_active) in enumerate(
                (
                    ("first-active", True),
                    ("second-inactive", False),
                    ("third-active", True),
                )
            )
        ]
        client = _FakeHttpClient(responses=[WebhookHttpResponse(status_code=204)])

        with pytest.raises(
            InactiveWebhookEndpointError,
            match="^Webhook endpoint is inactive$",
        ) as error_info:
            _run_iteration(
                client=client,
                iteration_at=iteration_at,
                stale_before=stale_before,
                recovery_limit=3,
                processing_limit=3,
                attempted_at=iteration_at + timedelta(minutes=1),
                decision_at=iteration_at + timedelta(minutes=2),
            )

        assert type(error_info.value) is InactiveWebhookEndpointError
        assert client.requests == [
            _RecordedRequest(
                target_url=jobs[0].target_url,
                payload=jobs[0].payload,
                timeout_seconds=TIMEOUT_SECONDS,
            )
        ]

        with SessionFactory() as verification_session:
            stored_jobs = [_get_job(verification_session, job.job_id) for job in jobs]
            attempts = [_attempts_for_event(verification_session, job.event_id) for job in jobs]
            assert stored_jobs[0].status == "succeeded"
            assert stored_jobs[0].next_attempt_at is None
            assert len(attempts[0]) == 1
            assert attempts[0][0].attempt_number == 1
            assert attempts[0][0].outcome == "succeeded"
            assert stored_jobs[1].status == "processing"
            assert stored_jobs[1].next_attempt_at is not None
            assert _as_utc(stored_jobs[1].next_attempt_at) == iteration_at
            assert _as_utc(stored_jobs[1].updated_at) == iteration_at
            assert attempts[1] == []
            assert stored_jobs[2].status == "processing"
            assert stored_jobs[2].next_attempt_at is not None
            assert _as_utc(stored_jobs[2].next_attempt_at) == iteration_at
            assert _as_utc(stored_jobs[2].updated_at) == iteration_at
            assert attempts[2] == []

        assert _idle_in_transaction_count() == 0
