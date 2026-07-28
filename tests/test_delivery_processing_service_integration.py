import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import WebhookHttpResponse
from reliable_webhook_service.delivery_processing_service import (
    WebhookDeliveryProcessingCycleResult,
    run_webhook_delivery_processing_cycle,
)
from reliable_webhook_service.delivery_service import InactiveWebhookEndpointError
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
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
    direct_attempt_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    target_url: str
    payload: dict[str, JsonValue]
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
        for attempt_id in records.direct_attempt_ids:
            assert session.get(WebhookDeliveryAttempt, attempt_id) is None
        for job_id in records.job_ids:
            assert session.get(WebhookDeliveryJob, job_id) is None
        for event_id in records.event_ids:
            assert session.get(WebhookEvent, event_id) is None
        for endpoint_id in records.endpoint_ids:
            assert session.get(WebhookEndpoint, endpoint_id) is None
        marker_count = session.scalar(
            select(func.count())
            .select_from(WebhookEndpoint)
            .where(WebhookEndpoint.name.contains(str(records.marker)))
        )
        assert marker_count == 0


def _persist_job(
    records: _CreatedRecords,
    *,
    label: str,
    next_attempt_at: datetime,
    created_at: datetime,
    updated_at: datetime,
    is_active: bool = True,
    attempt_count: int = 0,
) -> _PersistedJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/processing-cycle/{records.marker}/{label}"
    payload: dict[str, JsonValue] = {
        "processing_cycle_test_marker": str(records.marker),
        "label": label,
    }
    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(job_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery processing cycle {records.marker} {label}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="delivery.processing-cycle.integration",
                payload=payload,
            )
        )
        session.flush()
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status="pending",
                next_attempt_at=next_attempt_at,
                attempt_count=attempt_count,
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
        payload=payload,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _seed_failed_attempts(
    records: _CreatedRecords,
    persisted: _PersistedJob,
    *,
    count: int,
) -> list[uuid.UUID]:
    attempt_ids: list[uuid.UUID] = []
    with SessionFactory() as session:
        for attempt_number in range(1, count + 1):
            attempt_id = uuid.uuid4()
            attempt_ids.append(attempt_id)
            records.direct_attempt_ids.append(attempt_id)
            session.add(
                WebhookDeliveryAttempt(
                    id=attempt_id,
                    event_id=persisted.event_id,
                    attempt_number=attempt_number,
                    outcome="failed",
                    target_url=persisted.target_url,
                    response_status_code=503,
                    error_message="HTTP response returned status 503",
                    duration_ms=attempt_number,
                    attempted_at=datetime(
                        2026,
                        7,
                        29,
                        8,
                        attempt_number,
                        tzinfo=UTC,
                    ),
                )
            )
        session.commit()
    return attempt_ids


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


def _run_cycle(
    *,
    client: _FakeHttpClient,
    claimed_at: datetime,
    limit: int,
    attempted_at: datetime,
    decision_at: datetime,
) -> WebhookDeliveryProcessingCycleResult:
    monotonic_values = iter(range(1_000_000_000, 2_000_000_000, 5_000_000))
    return run_webhook_delivery_processing_cycle(
        session_factory=SessionFactory,
        http_client=client,
        claimed_at=claimed_at,
        limit=limit,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=lambda: attempted_at,
        decision_now=lambda: decision_at,
        monotonic_ns=monotonic_values.__next__,
    )


def test_processing_cycle_commits_claim_and_releases_lock_before_http() -> None:
    with _isolated_records() as records:
        due_at = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
        claimed_at = due_at + timedelta(minutes=1)
        attempted_at = due_at + timedelta(minutes=2)
        decision_at = due_at + timedelta(minutes=3)
        persisted = _persist_job(
            records,
            label="claim-lock",
            next_attempt_at=due_at,
            created_at=due_at - timedelta(minutes=1),
            updated_at=due_at - timedelta(seconds=30),
        )
        lock_was_obtained = False

        def verify_claim_commit_and_lock(
            request_index: int,
            request: _RecordedRequest,
        ) -> None:
            nonlocal lock_was_obtained
            assert request_index == 0
            assert request.target_url == persisted.target_url
            with SessionFactory() as observer_session:
                try:
                    observed_job = _get_job(observer_session, persisted.job_id)
                    assert observed_job.status == "processing"
                    statement = (
                        select(WebhookDeliveryJob)
                        .where(WebhookDeliveryJob.id == persisted.job_id)
                        .with_for_update(nowait=True)
                    )
                    locked_job = observer_session.scalar(statement)
                    assert locked_job is not None
                    assert locked_job.id == persisted.job_id
                    lock_was_obtained = True
                finally:
                    observer_session.rollback()

        client = _FakeHttpClient(
            responses=[WebhookHttpResponse(status_code=204)],
            on_request=verify_claim_commit_and_lock,
        )

        result = _run_cycle(
            client=client,
            claimed_at=claimed_at,
            limit=1,
            attempted_at=attempted_at,
            decision_at=decision_at,
        )

        assert lock_was_obtained is True
        assert result.claimed_job_ids == (persisted.job_id,)
        assert result.claimed_count == 1
        assert result.completed_count == 1
        summary = result.completed_jobs[0]
        assert summary.job_id == persisted.job_id
        assert summary.status == "succeeded"
        assert summary.next_attempt_at is None
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
            assert attempts[0].id == summary.attempt_id
            assert attempts[0].attempt_number == 1
            assert attempts[0].outcome == "succeeded"
            assert attempts[0].response_status_code == 204


def test_processing_cycle_commits_ordered_success_retry_and_dead_letter_results() -> None:
    with _isolated_records() as records:
        base_time = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)
        jobs = [
            _persist_job(
                records,
                label=label,
                next_attempt_at=base_time + timedelta(seconds=index),
                created_at=base_time - timedelta(minutes=1),
                updated_at=base_time - timedelta(seconds=30),
                attempt_count=2 if label == "dead-letter" else 0,
            )
            for index, label in enumerate(("succeeded", "retryable", "dead-letter"))
        ]
        previous_attempt_ids = _seed_failed_attempts(records, jobs[2], count=2)
        claimed_at = base_time + timedelta(minutes=1)
        attempted_at = base_time + timedelta(minutes=2)
        decision_at = base_time + timedelta(minutes=3)
        expected_retry_at = decision_at + timedelta(seconds=5)
        committed_before_next_request: list[uuid.UUID] = []

        def verify_previous_completion(
            request_index: int,
            request: _RecordedRequest,
        ) -> None:
            assert request == _RecordedRequest(
                target_url=jobs[request_index].target_url,
                payload=jobs[request_index].payload,
                timeout_seconds=TIMEOUT_SECONDS,
            )
            if request_index == 0:
                return
            previous = jobs[request_index - 1]
            with SessionFactory() as observer_session:
                try:
                    previous_job = _get_job(observer_session, previous.job_id)
                    previous_attempts = _attempts_for_event(
                        observer_session,
                        previous.event_id,
                    )
                    assert previous_job.status == ("succeeded" if request_index == 1 else "pending")
                    assert len(previous_attempts) == 1
                    committed_before_next_request.append(previous.job_id)
                finally:
                    observer_session.rollback()

        client = _FakeHttpClient(
            responses=[
                WebhookHttpResponse(status_code=204),
                WebhookHttpResponse(status_code=503),
                WebhookHttpResponse(status_code=500),
            ],
            on_request=verify_previous_completion,
        )

        result = _run_cycle(
            client=client,
            claimed_at=claimed_at,
            limit=3,
            attempted_at=attempted_at,
            decision_at=decision_at,
        )

        expected_job_ids = tuple(job.job_id for job in jobs)
        assert result.claimed_job_ids == expected_job_ids
        assert tuple(summary.job_id for summary in result.completed_jobs) == expected_job_ids
        assert result.claimed_count == 3
        assert result.completed_count == 3
        assert [summary.status for summary in result.completed_jobs] == [
            "succeeded",
            "pending",
            "dead_letter",
        ]
        assert result.completed_jobs[0].next_attempt_at is None
        assert result.completed_jobs[1].next_attempt_at == expected_retry_at
        assert result.completed_jobs[2].next_attempt_at is None
        assert committed_before_next_request == [jobs[0].job_id, jobs[1].job_id]
        assert len(client.requests) == 3
        assert [request.target_url for request in client.requests] == [
            job.target_url for job in jobs
        ]
        assert [request.payload for request in client.requests] == [job.payload for job in jobs]

        with SessionFactory() as verification_session:
            stored_jobs = [_get_job(verification_session, job.job_id) for job in jobs]
            attempts = [_attempts_for_event(verification_session, job.event_id) for job in jobs]
            assert [job.status for job in stored_jobs] == [
                "succeeded",
                "pending",
                "dead_letter",
            ]
            assert [job.attempt_count for job in stored_jobs] == [1, 1, 3]
            assert stored_jobs[0].next_attempt_at is None
            assert stored_jobs[1].next_attempt_at is not None
            assert _as_utc(stored_jobs[1].next_attempt_at) == expected_retry_at
            assert stored_jobs[2].next_attempt_at is None
            assert [len(event_attempts) for event_attempts in attempts] == [1, 1, 3]
            assert [attempt.attempt_number for attempt in attempts[0]] == [1]
            assert [attempt.attempt_number for attempt in attempts[1]] == [1]
            assert [attempt.attempt_number for attempt in attempts[2]] == [1, 2, 3]
            assert {attempt.id for attempt in attempts[2][:-1]} == set(previous_attempt_ids)
            assert [attempts[index][-1].id for index in range(3)] == [
                summary.attempt_id for summary in result.completed_jobs
            ]
            assert [attempts[index][-1].outcome for index in range(3)] == [
                "succeeded",
                "failed",
                "failed",
            ]


def test_processing_cycle_with_no_due_jobs_performs_no_writes_or_http() -> None:
    with _isolated_records() as records:
        claimed_at = datetime(2026, 7, 29, 11, 0, tzinfo=UTC)
        future_at = claimed_at + timedelta(hours=1)
        created_at = claimed_at - timedelta(minutes=2)
        updated_at = claimed_at - timedelta(minutes=1)
        persisted = _persist_job(
            records,
            label="future",
            next_attempt_at=future_at,
            created_at=created_at,
            updated_at=updated_at,
        )
        client = _FakeHttpClient(responses=[])

        with SessionFactory() as snapshot_session:
            endpoint_before = snapshot_session.get(WebhookEndpoint, persisted.endpoint_id)
            event_before = snapshot_session.get(WebhookEvent, persisted.event_id)
            assert endpoint_before is not None
            assert event_before is not None
            endpoint_snapshot = (
                endpoint_before.name,
                endpoint_before.target_url,
                endpoint_before.is_active,
                endpoint_before.created_at,
                endpoint_before.updated_at,
            )
            event_snapshot = (
                event_before.endpoint_id,
                event_before.event_type,
                event_before.payload,
                event_before.created_at,
            )

        result = _run_cycle(
            client=client,
            claimed_at=claimed_at,
            limit=1,
            attempted_at=claimed_at + timedelta(minutes=1),
            decision_at=claimed_at + timedelta(minutes=2),
        )

        assert result.claimed_job_ids == ()
        assert result.completed_jobs == ()
        assert result.claimed_count == 0
        assert result.completed_count == 0
        assert client.requests == []

        with SessionFactory() as verification_session:
            job = _get_job(verification_session, persisted.job_id)
            endpoint = verification_session.get(WebhookEndpoint, persisted.endpoint_id)
            event = verification_session.get(WebhookEvent, persisted.event_id)
            assert endpoint is not None
            assert event is not None
            assert job.status == "pending"
            assert job.next_attempt_at is not None
            assert _as_utc(job.next_attempt_at) == future_at
            assert _as_utc(job.updated_at) == updated_at
            assert _attempts_for_event(verification_session, persisted.event_id) == []
            assert (
                endpoint.name,
                endpoint.target_url,
                endpoint.is_active,
                endpoint.created_at,
                endpoint.updated_at,
            ) == endpoint_snapshot
            assert (
                event.endpoint_id,
                event.event_type,
                event.payload,
                event.created_at,
            ) == event_snapshot


def test_processing_cycle_preserves_earlier_commit_and_stops_after_later_failure() -> None:
    with _isolated_records() as records:
        base_time = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
        jobs = [
            _persist_job(
                records,
                label=label,
                next_attempt_at=base_time + timedelta(seconds=index),
                created_at=base_time - timedelta(minutes=1),
                updated_at=base_time - timedelta(seconds=30),
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
        claimed_at = base_time + timedelta(minutes=1)
        decision_at = base_time + timedelta(minutes=3)
        client = _FakeHttpClient(responses=[WebhookHttpResponse(status_code=204)])

        with pytest.raises(
            InactiveWebhookEndpointError,
            match="^Webhook endpoint is inactive$",
        ) as error_info:
            _run_cycle(
                client=client,
                claimed_at=claimed_at,
                limit=3,
                attempted_at=base_time + timedelta(minutes=2),
                decision_at=decision_at,
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
            assert attempts[0][0].outcome == "succeeded"
            assert stored_jobs[1].status == "processing"
            assert stored_jobs[1].next_attempt_at is not None
            assert _as_utc(stored_jobs[1].next_attempt_at) == jobs[1].next_attempt_at
            assert _as_utc(stored_jobs[1].updated_at) == claimed_at
            assert attempts[1] == []
            assert stored_jobs[2].status == "processing"
            assert stored_jobs[2].next_attempt_at is not None
            assert _as_utc(stored_jobs[2].next_attempt_at) == jobs[2].next_attempt_at
            assert _as_utc(stored_jobs[2].updated_at) == claimed_at
            assert attempts[2] == []


def test_processing_cycle_respects_limit_and_leaves_remaining_job_pending() -> None:
    with _isolated_records() as records:
        base_time = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)
        jobs = [
            _persist_job(
                records,
                label=f"limit-{index}",
                next_attempt_at=base_time + timedelta(seconds=index),
                created_at=base_time - timedelta(minutes=1),
                updated_at=base_time - timedelta(seconds=30),
            )
            for index in range(3)
        ]
        claimed_at = base_time + timedelta(minutes=1)
        client = _FakeHttpClient(
            responses=[
                WebhookHttpResponse(status_code=200),
                WebhookHttpResponse(status_code=204),
            ]
        )

        result = _run_cycle(
            client=client,
            claimed_at=claimed_at,
            limit=2,
            attempted_at=base_time + timedelta(minutes=2),
            decision_at=base_time + timedelta(minutes=3),
        )

        expected_claimed_ids = (jobs[0].job_id, jobs[1].job_id)
        assert result.claimed_job_ids == expected_claimed_ids
        assert tuple(summary.job_id for summary in result.completed_jobs) == expected_claimed_ids
        assert result.claimed_count == 2
        assert result.completed_count == 2
        assert jobs[2].job_id not in result.claimed_job_ids
        assert [request.target_url for request in client.requests] == [
            jobs[0].target_url,
            jobs[1].target_url,
        ]

        with SessionFactory() as verification_session:
            stored_jobs = [_get_job(verification_session, job.job_id) for job in jobs]
            attempts = [_attempts_for_event(verification_session, job.event_id) for job in jobs]
            assert [job.status for job in stored_jobs] == [
                "succeeded",
                "succeeded",
                "pending",
            ]
            assert [len(event_attempts) for event_attempts in attempts] == [1, 1, 0]
            assert stored_jobs[2].next_attempt_at is not None
            assert _as_utc(stored_jobs[2].next_attempt_at) == jobs[2].next_attempt_at
            assert _as_utc(stored_jobs[2].updated_at) == jobs[2].updated_at
