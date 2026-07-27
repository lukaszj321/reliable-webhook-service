import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from reliable_webhook_service.worker_loop_service import (
    WebhookWorkerRunResult,
    run_webhook_worker,
)

POLL_INTERVAL_SECONDS = 1.25
STALE_PROCESSING_TIMEOUT_SECONDS = 300.0
RECOVERY_LIMIT = 10
PROCESSING_LIMIT = 10
TIMEOUT_SECONDS = 4.5
MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    target_url: str
    payload_marker: str
    timeout_seconds: float


class _FakeHttpClient:
    def __init__(
        self,
        *,
        status_codes: list[int],
        on_request: Callable[[int, _RecordedRequest], None] | None = None,
    ) -> None:
        self._status_codes = status_codes
        self._on_request = on_request
        self.requests: list[_RecordedRequest] = []

    def post_json(
        self,
        *,
        target_url: str,
        payload: dict[str, JsonValue],
        timeout_seconds: float,
    ) -> WebhookHttpResponse:
        marker = payload["long_running_worker_integration_marker"]
        assert isinstance(marker, str)
        request = _RecordedRequest(
            target_url=target_url,
            payload_marker=marker,
            timeout_seconds=timeout_seconds,
        )
        request_index = len(self.requests)
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request(request_index, request)
        if request_index >= len(self._status_codes):
            raise AssertionError("Unexpected HTTP request")
        return WebhookHttpResponse(status_code=self._status_codes[request_index])


class _Values[T]:
    def __init__(self, values: list[T]) -> None:
        self._values = iter(values)
        self.call_count = 0

    def __call__(self) -> T:
        self.call_count += 1
        try:
            return next(self._values)
        except StopIteration as error:
            raise AssertionError("Unexpected clock invocation") from error


class _NeverStop:
    def __init__(self) -> None:
        self.call_count = 0

    def __call__(self) -> bool:
        self.call_count += 1
        return False


class _ControlledWait:
    def __init__(
        self,
        *,
        returns: list[bool],
        on_wait: Callable[[int], None] | None = None,
    ) -> None:
        self._returns = returns
        self._on_wait = on_wait
        self.intervals: list[float] = []

    def __call__(self, interval_seconds: float) -> bool:
        wait_index = len(self.intervals)
        self.intervals.append(interval_seconds)
        if self._on_wait is not None:
            self._on_wait(wait_index)
        if wait_index >= len(self._returns):
            raise AssertionError("Unexpected wait invocation")
        return self._returns[wait_index]


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
    payload_marker: str
    next_attempt_at: datetime
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
        marker_count = session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.payload["long_running_worker_integration_marker"].as_string()
                == str(records.marker)
            )
        )
        assert marker_count == 0
        for job_id in records.job_ids:
            assert session.get(WebhookDeliveryJob, job_id) is None
        for event_id in records.event_ids:
            assert session.get(WebhookEvent, event_id) is None
        for endpoint_id in records.endpoint_ids:
            assert session.get(WebhookEndpoint, endpoint_id) is None


def _persist_job(
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    next_attempt_at: datetime,
    created_at: datetime,
    updated_at: datetime,
    is_active: bool = True,
) -> _PersistedJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/long-running-worker/{records.marker}/{label}"
    payload_marker = str(records.marker)
    payload: dict[str, JsonValue] = {
        "long_running_worker_integration_marker": payload_marker,
        "label": label,
    }
    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(job_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Long-running worker {records.marker} {label}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=f"long-running-worker.integration.{label}",
                payload=payload,
            )
        )
        session.flush()
        session.add(
            WebhookDeliveryJob(
                id=job_id,
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
        job_id=job_id,
        target_url=target_url,
        payload_marker=payload_marker,
        next_attempt_at=next_attempt_at,
        updated_at=updated_at,
    )


def _get_job(session: Session, job_id: uuid.UUID) -> WebhookDeliveryJob:
    job = session.get(WebhookDeliveryJob, job_id)
    assert job is not None
    return job


def _attempts_for_event(
    session: Session,
    event_id: uuid.UUID,
) -> list[WebhookDeliveryAttempt]:
    return list(
        session.scalars(
            select(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.event_id == event_id)
            .order_by(WebhookDeliveryAttempt.attempt_number)
        ).all()
    )


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


def _run_worker(
    *,
    client: _FakeHttpClient,
    iteration_now: Callable[[], datetime],
    utc_now: Callable[[], datetime],
    decision_now: Callable[[], datetime],
    monotonic_ns: Callable[[], int],
    stop_requested: Callable[[], bool],
    wait: Callable[[float], bool],
) -> WebhookWorkerRunResult:
    return run_webhook_worker(
        session_factory=SessionFactory,
        http_client=client,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        stale_processing_timeout_seconds=STALE_PROCESSING_TIMEOUT_SECONDS,
        recovery_limit=RECOVERY_LIMIT,
        processing_limit=PROCESSING_LIMIT,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        stop_requested=stop_requested,
        wait=wait,
        iteration_now=iteration_now,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )


def test_due_pending_job_is_delivered_in_first_iteration() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
        persisted = _persist_job(
            records,
            label="due-first-iteration",
            status="pending",
            next_attempt_at=iteration_at - timedelta(minutes=1),
            created_at=iteration_at - timedelta(hours=2),
            updated_at=iteration_at - timedelta(hours=1),
        )
        client = _FakeHttpClient(status_codes=[204])
        iteration_clock = _Values([iteration_at])
        attempted_clock = _Values([iteration_at + timedelta(seconds=1)])
        decision_clock = _Values([iteration_at + timedelta(seconds=2)])
        monotonic_clock = _Values([1_000_000_000, 1_005_000_000])
        stop = _NeverStop()
        wait = _ControlledWait(returns=[True])

        result = _run_worker(
            client=client,
            iteration_now=iteration_clock,
            utc_now=attempted_clock,
            decision_now=decision_clock,
            monotonic_ns=monotonic_clock,
            stop_requested=stop,
            wait=wait,
        )

        assert result.iterations_started == 1
        assert result.iterations_completed == 1
        assert result.total_recovered_count == 0
        assert result.total_claimed_count == 1
        assert result.total_completed_count == 1
        assert result.shutdown_requested is True
        assert result.final_iteration is not None
        assert result.final_iteration.recovered_count == 0
        assert result.final_iteration.claimed_count == 1
        assert result.final_iteration.completed_count == 1
        assert iteration_clock.call_count == 1
        assert wait.intervals == [POLL_INTERVAL_SECONDS]
        assert client.requests == [
            _RecordedRequest(
                target_url=persisted.target_url,
                payload_marker=persisted.payload_marker,
                timeout_seconds=TIMEOUT_SECONDS,
            )
        ]

        with SessionFactory() as session:
            job = _get_job(session, persisted.job_id)
            attempts = _attempts_for_event(session, persisted.event_id)
            assert job.status == "succeeded"
            assert job.next_attempt_at is None
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].response_status_code == 204
            assert attempts[0].duration_ms == 5
        assert _idle_in_transaction_count() == 0


def test_retryable_failure_is_retried_in_later_iteration() -> None:
    with _isolated_records() as records:
        first_iteration_at = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
        second_iteration_at = first_iteration_at + timedelta(seconds=BASE_DELAY_SECONDS)
        persisted = _persist_job(
            records,
            label="retry-later-iteration",
            status="pending",
            next_attempt_at=first_iteration_at - timedelta(minutes=1),
            created_at=first_iteration_at - timedelta(hours=2),
            updated_at=first_iteration_at - timedelta(hours=1),
        )
        client = _FakeHttpClient(status_codes=[500, 204])
        iteration_clock = _Values([first_iteration_at, second_iteration_at])
        attempted_clock = _Values([first_iteration_at, second_iteration_at])
        decision_clock = _Values([first_iteration_at, second_iteration_at])
        monotonic_clock = _Values(
            [
                1_000_000_000,
                1_005_000_000,
                2_000_000_000,
                2_007_000_000,
            ]
        )
        stop = _NeverStop()

        def verify_first_iteration(wait_index: int) -> None:
            if wait_index != 0:
                return
            with SessionFactory() as session:
                job = _get_job(session, persisted.job_id)
                attempts = _attempts_for_event(session, persisted.event_id)
                assert job.status == "pending"
                assert job.next_attempt_at is not None
                assert _as_utc(job.next_attempt_at) == second_iteration_at
                assert len(attempts) == 1
                assert attempts[0].attempt_number == 1
                assert attempts[0].outcome == "failed"
                assert attempts[0].response_status_code == 500

        wait = _ControlledWait(
            returns=[False, True],
            on_wait=verify_first_iteration,
        )

        result = _run_worker(
            client=client,
            iteration_now=iteration_clock,
            utc_now=attempted_clock,
            decision_now=decision_clock,
            monotonic_ns=monotonic_clock,
            stop_requested=stop,
            wait=wait,
        )

        assert result.iterations_started == 2
        assert result.iterations_completed == 2
        assert result.total_recovered_count == 0
        assert result.total_claimed_count == 2
        assert result.total_completed_count == 2
        assert result.shutdown_requested is True
        assert result.final_iteration is not None
        assert result.final_iteration.recovered_count == 0
        assert result.final_iteration.claimed_count == 1
        assert result.final_iteration.completed_count == 1
        assert iteration_clock.call_count == 2
        assert wait.intervals == [POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS]
        assert [request.target_url for request in client.requests] == [
            persisted.target_url,
            persisted.target_url,
        ]

        with SessionFactory() as session:
            job = _get_job(session, persisted.job_id)
            attempts = _attempts_for_event(session, persisted.event_id)
            assert job.status == "succeeded"
            assert job.next_attempt_at is None
            assert len(attempts) == 2
            assert [attempt.attempt_number for attempt in attempts] == [1, 2]
            assert [attempt.outcome for attempt in attempts] == ["failed", "succeeded"]
            assert [attempt.response_status_code for attempt in attempts] == [500, 204]
            assert [attempt.duration_ms for attempt in attempts] == [5, 7]
        assert _idle_in_transaction_count() == 0


def test_stale_processing_job_is_recovered_and_delivered_in_later_iteration() -> None:
    with _isolated_records() as records:
        first_iteration_at = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
        second_iteration_at = first_iteration_at + timedelta(minutes=10)
        updated_at = first_iteration_at - timedelta(minutes=4)
        persisted = _persist_job(
            records,
            label="stale-later-iteration",
            status="processing",
            next_attempt_at=updated_at,
            created_at=first_iteration_at - timedelta(hours=2),
            updated_at=updated_at,
        )
        recovery_lock_was_released = False

        def verify_recovery_lock_released(
            request_index: int,
            request: _RecordedRequest,
        ) -> None:
            nonlocal recovery_lock_was_released
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
                assert _as_utc(locked_job.next_attempt_at) == second_iteration_at
                recovery_lock_was_released = True
            finally:
                if observer_session.in_transaction():
                    observer_session.rollback()
                observer_session.close()

        client = _FakeHttpClient(
            status_codes=[204],
            on_request=verify_recovery_lock_released,
        )
        iteration_clock = _Values([first_iteration_at, second_iteration_at])
        attempted_clock = _Values([second_iteration_at + timedelta(seconds=1)])
        decision_clock = _Values([second_iteration_at + timedelta(seconds=2)])
        monotonic_clock = _Values([1_000_000_000, 1_006_000_000])
        stop = _NeverStop()

        def verify_first_iteration(wait_index: int) -> None:
            if wait_index != 0:
                return
            with SessionFactory() as session:
                job = _get_job(session, persisted.job_id)
                attempts = _attempts_for_event(session, persisted.event_id)
                assert job.status == "processing"
                assert job.next_attempt_at is not None
                assert _as_utc(job.next_attempt_at) == updated_at
                assert _as_utc(job.updated_at) == updated_at
                assert attempts == []

        wait = _ControlledWait(
            returns=[False, True],
            on_wait=verify_first_iteration,
        )

        result = _run_worker(
            client=client,
            iteration_now=iteration_clock,
            utc_now=attempted_clock,
            decision_now=decision_clock,
            monotonic_ns=monotonic_clock,
            stop_requested=stop,
            wait=wait,
        )

        assert recovery_lock_was_released is True
        assert result.iterations_started == 2
        assert result.iterations_completed == 2
        assert result.total_recovered_count == 1
        assert result.total_claimed_count == 1
        assert result.total_completed_count == 1
        assert result.final_iteration is not None
        assert result.final_iteration.recovery.recovered_job_ids == (persisted.job_id,)
        assert result.final_iteration.processing.claimed_job_ids == (persisted.job_id,)
        assert result.final_iteration.recovered_count == 1
        assert result.final_iteration.claimed_count == 1
        assert result.final_iteration.completed_count == 1
        assert iteration_clock.call_count == 2
        assert wait.intervals == [POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS]
        assert len(client.requests) == 1

        with SessionFactory() as session:
            job = _get_job(session, persisted.job_id)
            attempts = _attempts_for_event(session, persisted.event_id)
            assert job.status == "succeeded"
            assert job.next_attempt_at is None
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].response_status_code == 204
        assert _idle_in_transaction_count() == 0


def test_real_iteration_failure_stops_worker_without_wait_or_second_pass() -> None:
    with _isolated_records() as records:
        iteration_at = datetime(2026, 8, 10, 15, 0, tzinfo=UTC)
        persisted = _persist_job(
            records,
            label="inactive-failure",
            status="pending",
            next_attempt_at=iteration_at - timedelta(minutes=1),
            created_at=iteration_at - timedelta(hours=2),
            updated_at=iteration_at - timedelta(hours=1),
            is_active=False,
        )
        client = _FakeHttpClient(status_codes=[])
        iteration_clock = _Values([iteration_at])
        attempted_clock = _Values([iteration_at + timedelta(seconds=1)])
        decision_clock = _Values([iteration_at + timedelta(seconds=2)])
        monotonic_clock = _Values([1_000_000_000, 1_005_000_000])
        stop = _NeverStop()
        wait = _ControlledWait(returns=[True])

        with pytest.raises(
            InactiveWebhookEndpointError,
            match="^Webhook endpoint is inactive$",
        ) as error_info:
            _run_worker(
                client=client,
                iteration_now=iteration_clock,
                utc_now=attempted_clock,
                decision_now=decision_clock,
                monotonic_ns=monotonic_clock,
                stop_requested=stop,
                wait=wait,
            )

        assert type(error_info.value) is InactiveWebhookEndpointError
        assert iteration_clock.call_count == 1
        assert wait.intervals == []
        assert client.requests == []

        with SessionFactory() as session:
            job = _get_job(session, persisted.job_id)
            attempts = _attempts_for_event(session, persisted.event_id)
            assert job.status == "processing"
            assert job.next_attempt_at is not None
            assert _as_utc(job.next_attempt_at) == _as_utc(persisted.next_attempt_at)
            assert attempts == []
        assert _idle_in_transaction_count() == 0
