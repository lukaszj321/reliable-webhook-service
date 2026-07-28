import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_http import WebhookHttpResponse
from reliable_webhook_service.delivery_job_execution_service import (
    execute_webhook_delivery_job,
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
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class _RecordedRequest:
    target_url: str
    payload: dict[str, JsonValue]
    timeout_seconds: float


class _RecordingHttpClient:
    def __init__(self, *, status_code: int) -> None:
        self.status_code = status_code
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
        return WebhookHttpResponse(status_code=self.status_code)


@dataclass(slots=True)
class _CreatedRecords:
    marker: uuid.UUID
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)
    attempt_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedProcessingJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    target_url: str
    payload: dict[str, JsonValue]
    attempt_count: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords(marker=uuid.uuid4())

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

            marker_count = session.scalar(
                select(func.count())
                .select_from(WebhookEndpoint)
                .where(WebhookEndpoint.name.contains(str(records.marker)))
            )
            assert marker_count == 0


def _persist_processing_job(
    records: _CreatedRecords,
    *,
    label: str,
    is_active: bool = True,
    attempt_count: int = 0,
) -> _PersistedProcessingJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    target_url = f"https://example.test/atomic-delivery-job-completion/{records.marker}/{label}"
    payload: dict[str, JsonValue] = {
        "atomic_completion_test_marker": str(records.marker),
        "label": label,
    }
    next_attempt_at = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)
    created_at = datetime(2026, 7, 27, 8, 1, tzinfo=UTC)
    updated_at = datetime(2026, 7, 27, 8, 2, tzinfo=UTC)

    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(job_id)

    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Atomic delivery job completion {records.marker} {label}",
                target_url=target_url,
                is_active=is_active,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type="delivery.job.atomic-completion.integration",
                payload=payload,
            )
        )
        session.flush()
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status="processing",
                next_attempt_at=next_attempt_at,
                attempt_count=attempt_count,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.commit()

    return _PersistedProcessingJob(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=job_id,
        target_url=target_url,
        payload=payload,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _seed_previous_attempts(
    records: _CreatedRecords,
    persisted: _PersistedProcessingJob,
    *,
    count: int,
) -> list[uuid.UUID]:
    attempt_ids: list[uuid.UUID] = []

    with SessionFactory() as session:
        for attempt_number in range(1, count + 1):
            attempt_id = uuid.uuid4()
            attempt_ids.append(attempt_id)
            records.attempt_ids.append(attempt_id)
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
                        27,
                        8,
                        10 + attempt_number,
                        tzinfo=UTC,
                    ),
                )
            )
        session.commit()

    return attempt_ids


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


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


def _assert_single_request(
    client: _RecordingHttpClient,
    persisted: _PersistedProcessingJob,
) -> None:
    assert client.requests == [
        _RecordedRequest(
            target_url=persisted.target_url,
            payload=persisted.payload,
            timeout_seconds=TIMEOUT_SECONDS,
        )
    ]


def test_successful_completion_is_invisible_before_commit_and_visible_after_commit(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(created_records, label="succeeded")
    attempted_at = datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    decision_at = datetime(
        2026,
        7,
        27,
        11,
        1,
        tzinfo=timezone(timedelta(hours=2)),
    )
    expected_updated_at = decision_at.astimezone(UTC)
    client = _RecordingHttpClient(status_code=204)

    with SessionFactory() as caller_session:
        job = _get_job(caller_session, persisted.job_id)
        result = execute_webhook_delivery_job(
            caller_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=5,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: attempted_at,
            decision_now=lambda: decision_at,
            monotonic_ns=iter([1_000_000_000, 1_025_000_000]).__next__,
        )

        assert isinstance(result.attempt.id, uuid.UUID)
        created_records.attempt_ids.append(result.attempt.id)
        assert result.job is job
        assert result.attempt in caller_session
        assert result.attempt.attempt_number == 1
        assert result.attempt.outcome == "succeeded"
        assert job.status == "succeeded"
        assert job.next_attempt_at is None
        assert job.attempt_count == 1
        assert _as_utc(job.updated_at) == expected_updated_at

        with SessionFactory() as observer_session:
            assert observer_session.get(WebhookDeliveryAttempt, result.attempt.id) is None
            observed_job = _get_job(observer_session, persisted.job_id)
            assert observed_job.status == "processing"
            assert observed_job.attempt_count == 0
            assert observed_job.next_attempt_at is not None
            assert _as_utc(observed_job.next_attempt_at) == persisted.next_attempt_at
            assert _as_utc(observed_job.updated_at) == persisted.updated_at
            observer_session.rollback()

        caller_session.commit()
        attempt_id = result.attempt.id

    with SessionFactory() as verification_session:
        stored_attempt = verification_session.get(WebhookDeliveryAttempt, attempt_id)
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_attempt is not None
        assert stored_attempt.attempt_number == 1
        assert stored_attempt.outcome == "succeeded"
        assert stored_job.status == "succeeded"
        assert stored_job.attempt_count == 1
        assert stored_job.next_attempt_at is None
        assert _as_utc(stored_job.updated_at) == expected_updated_at
        assert verification_session.get(WebhookEndpoint, persisted.endpoint_id) is not None
        assert verification_session.get(WebhookEvent, persisted.event_id) is not None

    _assert_single_request(client, persisted)


def test_failed_completion_commits_exact_retry_schedule(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(created_records, label="retryable")
    attempted_at = datetime(2026, 7, 27, 9, 2, tzinfo=UTC)
    decision_at = datetime(2026, 7, 27, 9, 3, tzinfo=UTC)
    expected_next_attempt_at = decision_at.astimezone(UTC) + timedelta(seconds=5)
    client = _RecordingHttpClient(status_code=503)

    with SessionFactory() as caller_session:
        result = execute_webhook_delivery_job(
            caller_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=5,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: attempted_at,
            decision_now=lambda: decision_at,
            monotonic_ns=iter([2_000_000_000, 2_010_000_000]).__next__,
        )
        assert isinstance(result.attempt.id, uuid.UUID)
        created_records.attempt_ids.append(result.attempt.id)
        assert result.attempt.attempt_number == 1
        assert result.attempt.outcome == "failed"
        assert result.attempt.response_status_code == 503
        caller_session.commit()
        attempt_id = result.attempt.id

    with SessionFactory() as verification_session:
        stored_attempt = verification_session.get(WebhookDeliveryAttempt, attempt_id)
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_attempt is not None
        assert stored_attempt.attempt_number == 1
        assert stored_attempt.outcome == "failed"
        assert stored_attempt.response_status_code == 503
        assert stored_job.status == "pending"
        assert stored_job.attempt_count == 1
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == expected_next_attempt_at
        assert _as_utc(stored_job.updated_at) == decision_at.astimezone(UTC)

    _assert_single_request(client, persisted)


def test_final_failed_completion_commits_dead_letter_transition(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(
        created_records,
        label="dead-letter",
        attempt_count=2,
    )
    previous_attempt_ids = _seed_previous_attempts(
        created_records,
        persisted,
        count=2,
    )
    attempted_at = datetime(2026, 7, 27, 9, 4, tzinfo=UTC)
    decision_at = datetime(2026, 7, 27, 9, 5, tzinfo=UTC)
    client = _RecordingHttpClient(status_code=500)

    with SessionFactory() as caller_session:
        result = execute_webhook_delivery_job(
            caller_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=3,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: attempted_at,
            decision_now=lambda: decision_at,
            monotonic_ns=iter([3_000_000_000, 3_015_000_000]).__next__,
        )
        assert isinstance(result.attempt.id, uuid.UUID)
        created_records.attempt_ids.append(result.attempt.id)
        assert result.attempt.attempt_number == 3
        assert result.attempt.outcome == "failed"
        assert result.attempt.response_status_code == 500
        assert result.job.attempt_count == 3
        caller_session.commit()
        new_attempt_id = result.attempt.id

    with SessionFactory() as verification_session:
        attempts = _attempts_for_event(verification_session, persisted.event_id)
        stored_job = _get_job(verification_session, persisted.job_id)
        assert len(attempts) == 3
        assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3]
        assert {attempt.id for attempt in attempts} - set(previous_attempt_ids) == {new_attempt_id}
        assert stored_job.status == "dead_letter"
        assert stored_job.attempt_count == 3
        assert stored_job.next_attempt_at is None
        assert _as_utc(stored_job.updated_at) == decision_at.astimezone(UTC)

    _assert_single_request(client, persisted)


def test_manual_attempt_history_is_independent_from_worker_retry_cycle(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(created_records, label="manual-history")
    previous_attempt_ids = _seed_previous_attempts(
        created_records,
        persisted,
        count=2,
    )
    first_decision_at = datetime(2026, 7, 27, 9, 6, tzinfo=UTC)
    second_decision_at = datetime(2026, 7, 27, 9, 7, tzinfo=UTC)
    client = _RecordingHttpClient(status_code=503)

    with SessionFactory() as first_worker_session:
        first_result = execute_webhook_delivery_job(
            first_worker_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=3,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: datetime(2026, 7, 27, 9, 6, tzinfo=UTC),
            decision_now=lambda: first_decision_at,
            monotonic_ns=iter([4_000_000_000, 4_010_000_000]).__next__,
        )
        assert isinstance(first_result.attempt.id, uuid.UUID)
        created_records.attempt_ids.append(first_result.attempt.id)
        assert first_result.attempt.attempt_number == 3
        assert first_result.job.attempt_count == 1
        assert first_result.job.status == "pending"
        assert first_result.job.next_attempt_at == first_decision_at + timedelta(seconds=5)
        first_worker_session.commit()
        first_worker_attempt_id = first_result.attempt.id

    with SessionFactory() as claim_session:
        claimed_job = _get_job(claim_session, persisted.job_id)
        assert claimed_job.attempt_count == 1
        claimed_job.status = "processing"
        claimed_job.next_attempt_at = second_decision_at
        claim_session.commit()

    with SessionFactory() as second_worker_session:
        second_result = execute_webhook_delivery_job(
            second_worker_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=3,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: datetime(2026, 7, 27, 9, 7, tzinfo=UTC),
            decision_now=lambda: second_decision_at,
            monotonic_ns=iter([5_000_000_000, 5_010_000_000]).__next__,
        )
        assert isinstance(second_result.attempt.id, uuid.UUID)
        created_records.attempt_ids.append(second_result.attempt.id)
        assert second_result.attempt.attempt_number == 4
        assert second_result.job.attempt_count == 2
        assert second_result.job.status == "pending"
        assert second_result.job.next_attempt_at == second_decision_at + timedelta(seconds=10)
        second_worker_session.commit()
        second_worker_attempt_id = second_result.attempt.id

    with SessionFactory() as verification_session:
        attempts = _attempts_for_event(verification_session, persisted.event_id)
        stored_job = _get_job(verification_session, persisted.job_id)
        assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3, 4]
        assert {attempt.id for attempt in attempts} - set(previous_attempt_ids) == {
            first_worker_attempt_id,
            second_worker_attempt_id,
        }
        assert stored_job.attempt_count == 2
        assert stored_job.status == "pending"
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == second_decision_at + timedelta(seconds=10)

    assert len(client.requests) == 2


def test_caller_rollback_removes_attempt_and_restores_processing_job(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(created_records, label="rollback")
    attempted_at = datetime(2026, 7, 27, 9, 6, tzinfo=UTC)
    decision_at = datetime(2026, 7, 27, 9, 7, tzinfo=UTC)
    client = _RecordingHttpClient(status_code=200)

    with SessionFactory() as caller_session:
        result = execute_webhook_delivery_job(
            caller_session,
            job_id=persisted.job_id,
            http_client=client,
            timeout_seconds=TIMEOUT_SECONDS,
            max_attempts=5,
            base_delay_seconds=BASE_DELAY_SECONDS,
            max_delay_seconds=MAX_DELAY_SECONDS,
            utc_now=lambda: attempted_at,
            decision_now=lambda: decision_at,
            monotonic_ns=iter([4_000_000_000, 4_020_000_000]).__next__,
        )
        assert isinstance(result.attempt.id, uuid.UUID)
        attempt_id = result.attempt.id
        created_records.attempt_ids.append(attempt_id)
        assert caller_session.get(WebhookDeliveryAttempt, attempt_id) is result.attempt
        assert result.job.status == "succeeded"
        assert result.job.attempt_count == 1

        with SessionFactory() as observer_session:
            assert observer_session.get(WebhookDeliveryAttempt, attempt_id) is None
            assert _get_job(observer_session, persisted.job_id).status == "processing"
            assert _get_job(observer_session, persisted.job_id).attempt_count == 0
            observer_session.rollback()

        caller_session.rollback()

    with SessionFactory() as verification_session:
        assert verification_session.get(WebhookDeliveryAttempt, attempt_id) is None
        assert _attempts_for_event(verification_session, persisted.event_id) == []
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.status == "processing"
        assert stored_job.attempt_count == 0
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == persisted.next_attempt_at
        assert _as_utc(stored_job.updated_at) == persisted.updated_at
        assert verification_session.get(WebhookEndpoint, persisted.endpoint_id) is not None
        assert verification_session.get(WebhookEvent, persisted.event_id) is not None
        assert verification_session.get(WebhookDeliveryJob, persisted.job_id) is not None

    _assert_single_request(client, persisted)


def test_inactive_endpoint_error_leaves_processing_job_unchanged(
    created_records: _CreatedRecords,
) -> None:
    persisted = _persist_processing_job(
        created_records,
        label="inactive-endpoint",
        is_active=False,
    )
    client = _RecordingHttpClient(status_code=204)

    with SessionFactory() as caller_session:
        job = _get_job(caller_session, persisted.job_id)

        with pytest.raises(
            InactiveWebhookEndpointError,
            match="^Webhook endpoint is inactive$",
        ) as error_info:
            execute_webhook_delivery_job(
                caller_session,
                job_id=persisted.job_id,
                http_client=client,
                timeout_seconds=TIMEOUT_SECONDS,
                max_attempts=5,
                base_delay_seconds=BASE_DELAY_SECONDS,
                max_delay_seconds=MAX_DELAY_SECONDS,
                utc_now=lambda: datetime(2026, 7, 27, 9, 8, tzinfo=UTC),
                decision_now=lambda: datetime(2026, 7, 27, 9, 9, tzinfo=UTC),
                monotonic_ns=iter([5_000_000_000, 5_010_000_000]).__next__,
            )

        assert type(error_info.value) is InactiveWebhookEndpointError
        assert client.requests == []
        assert _attempts_for_event(caller_session, persisted.event_id) == []
        assert job.status == "processing"
        assert job.attempt_count == 0
        assert job.next_attempt_at is not None
        assert _as_utc(job.next_attempt_at) == persisted.next_attempt_at
        assert _as_utc(job.updated_at) == persisted.updated_at
        caller_session.rollback()

    with SessionFactory() as verification_session:
        assert _attempts_for_event(verification_session, persisted.event_id) == []
        stored_job = _get_job(verification_session, persisted.job_id)
        assert stored_job.status == "processing"
        assert stored_job.attempt_count == 0
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == persisted.next_attempt_at
        assert _as_utc(stored_job.updated_at) == persisted.updated_at
        assert verification_session.get(WebhookEndpoint, persisted.endpoint_id) is not None
        assert verification_session.get(WebhookEvent, persisted.event_id) is not None
        assert verification_session.get(WebhookDeliveryJob, persisted.job_id) is not None
