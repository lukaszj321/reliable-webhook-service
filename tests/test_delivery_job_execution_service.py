import uuid
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

import reliable_webhook_service.delivery_job_execution_service as execution_service
from reliable_webhook_service.delivery_job_execution_service import (
    WebhookDeliveryJobNotFoundError,
    WebhookDeliveryJobNotProcessingError,
    execute_webhook_delivery_job,
)
from reliable_webhook_service.models import WebhookDeliveryAttempt, WebhookDeliveryJob
from reliable_webhook_service.retry_policy import RetryDecision

JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
EVENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ATTEMPT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CREATED_AT = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
INITIAL_UPDATED_AT = datetime(2026, 7, 26, 8, 1, tzinfo=UTC)
INITIAL_NEXT_ATTEMPT_AT = datetime(2026, 7, 26, 8, 2, tzinfo=UTC)
ATTEMPTED_AT = datetime(2026, 7, 26, 8, 3, tzinfo=UTC)
TIMEOUT_SECONDS = 7.5
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0


def _job(
    *,
    status: str = "processing",
    attempt_count: int = 0,
) -> WebhookDeliveryJob:
    return WebhookDeliveryJob(
        id=JOB_ID,
        event_id=EVENT_ID,
        status=status,
        next_attempt_at=(
            None if status in {"succeeded", "dead_letter"} else INITIAL_NEXT_ATTEMPT_AT
        ),
        attempt_count=attempt_count,
        created_at=CREATED_AT,
        updated_at=INITIAL_UPDATED_AT,
    )


def _attempt(
    *,
    outcome: str,
    attempt_number: int,
) -> WebhookDeliveryAttempt:
    return WebhookDeliveryAttempt(
        id=ATTEMPT_ID,
        event_id=EVENT_ID,
        attempt_number=attempt_number,
        outcome=outcome,
        target_url="https://example.test/delivery-job-execution",
        response_status_code=200 if outcome == "succeeded" else 503,
        error_message=None if outcome == "succeeded" else "HTTP response returned status 503",
        duration_ms=12,
        attempted_at=ATTEMPTED_AT,
    )


def _session_returning(job: WebhookDeliveryJob | None) -> Mock:
    session = Mock(spec=Session)
    session.get.return_value = job
    return session


def _assert_caller_owns_transaction(session: Mock) -> None:
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.refresh.assert_not_called()
    session.close.assert_not_called()


def _execute(
    session: Mock,
    *,
    http_client: Mock,
    utc_now: Mock,
    decision_now: Mock,
    monotonic_ns: Mock,
):
    return execute_webhook_delivery_job(
        session,
        job_id=JOB_ID,
        http_client=http_client,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )


def _assert_execution_arguments(
    execution_mock: Mock,
    *,
    session: Mock,
    http_client: Mock,
    utc_now: Mock,
    monotonic_ns: Mock,
) -> None:
    execution_mock.assert_called_once_with(
        session,
        event_id=EVENT_ID,
        http_client=http_client,
        timeout_seconds=TIMEOUT_SECONDS,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )


def _assert_retry_arguments(
    retry_mock: Mock,
    *,
    attempt: WebhookDeliveryAttempt,
    cycle_attempt_number: int,
    decision_at: datetime,
) -> None:
    retry_mock.assert_called_once_with(
        outcome=attempt.outcome,
        attempt_number=cycle_attempt_number,
        decision_at=decision_at,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
    )


def _dependencies() -> tuple[Mock, Mock, Mock, Mock]:
    return Mock(), Mock(), Mock(), Mock()


def _job_values(job: WebhookDeliveryJob) -> tuple[object, ...]:
    return (
        job.id,
        job.event_id,
        job.status,
        job.next_attempt_at,
        job.attempt_count,
        job.created_at,
        job.updated_at,
    )


def test_rejects_missing_job_before_delivery_or_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _session_returning(None)
    execution_mock = Mock()
    retry_mock = Mock()
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(
        WebhookDeliveryJobNotFoundError,
        match="^Webhook delivery job not found$",
    ) as error_info:
        _execute(
            session,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )

    assert type(error_info.value) is WebhookDeliveryJobNotFoundError
    session.get.assert_called_once_with(WebhookDeliveryJob, JOB_ID)
    execution_mock.assert_not_called()
    retry_mock.assert_not_called()
    decision_now.assert_not_called()
    session.flush.assert_not_called()
    session.add.assert_not_called()
    _assert_caller_owns_transaction(session)


@pytest.mark.parametrize("status", ["pending", "succeeded", "dead_letter"])
def test_rejects_non_processing_job_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    job = _job(status=status)
    original_values = _job_values(job)
    session = _session_returning(job)
    execution_mock = Mock()
    retry_mock = Mock()
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(
        WebhookDeliveryJobNotProcessingError,
        match="^Webhook delivery job is not processing$",
    ) as error_info:
        _execute(
            session,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )

    assert type(error_info.value) is WebhookDeliveryJobNotProcessingError
    assert _job_values(job) == original_values
    execution_mock.assert_not_called()
    retry_mock.assert_not_called()
    decision_now.assert_not_called()
    session.flush.assert_not_called()
    session.add.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_applies_succeeded_decision_and_returns_same_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    original_identity_values = (job.id, job.event_id, job.created_at)
    attempt = _attempt(outcome="succeeded", attempt_number=2)
    decision_at = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    session = _session_returning(job)
    execution_mock = Mock(return_value=attempt)
    retry_mock = Mock(return_value=RetryDecision(status="succeeded", next_attempt_at=None))
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    result = _execute(
        session,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=1,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert job.status == "succeeded"
    assert job.next_attempt_at is None
    assert job.attempt_count == 1
    assert job.updated_at == decision_at
    assert (job.id, job.event_id, job.created_at) == original_identity_values
    session.flush.assert_called_once_with()
    session.add.assert_not_called()
    assert result.job is job
    assert result.attempt is attempt
    _assert_caller_owns_transaction(session)


def test_applies_retryable_failure_decision_without_recalculating_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(attempt_count=1)
    attempt = _attempt(outcome="failed", attempt_number=7)
    decision_at = datetime(2026, 7, 26, 9, 1, tzinfo=UTC)
    retry_at = datetime(2026, 7, 26, 9, 1, 10, tzinfo=UTC)
    session = _session_returning(job)
    execution_mock = Mock(return_value=attempt)
    retry_mock = Mock(return_value=RetryDecision(status="pending", next_attempt_at=retry_at))
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    result = _execute(
        session,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=2,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert job.status == "pending"
    assert job.next_attempt_at is retry_at
    assert job.attempt_count == 2
    assert job.updated_at == decision_at
    session.flush.assert_called_once_with()
    session.add.assert_not_called()
    assert result.job is job
    assert result.attempt is attempt
    _assert_caller_owns_transaction(session)


def test_applies_dead_letter_decision_and_returns_same_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(attempt_count=MAX_ATTEMPTS - 1)
    attempt = _attempt(outcome="failed", attempt_number=MAX_ATTEMPTS + 3)
    decision_at = datetime(2026, 7, 26, 9, 2, tzinfo=UTC)
    session = _session_returning(job)
    execution_mock = Mock(return_value=attempt)
    retry_mock = Mock(return_value=RetryDecision(status="dead_letter", next_attempt_at=None))
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    result = _execute(
        session,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=MAX_ATTEMPTS,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert job.status == "dead_letter"
    assert job.next_attempt_at is None
    assert job.attempt_count == MAX_ATTEMPTS
    assert job.updated_at == decision_at
    session.flush.assert_called_once_with()
    session.add.assert_not_called()
    assert result.job is job
    assert result.attempt is attempt
    _assert_caller_owns_transaction(session)


def test_normalizes_decision_timestamp_to_utc_after_retry_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    attempt = _attempt(outcome="succeeded", attempt_number=1)
    decision_at = datetime(
        2026,
        7,
        26,
        11,
        3,
        tzinfo=timezone(timedelta(hours=2)),
    )
    session = _session_returning(job)
    execution_mock = Mock(return_value=attempt)
    retry_mock = Mock(return_value=RetryDecision(status="succeeded", next_attempt_at=None))
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    result = _execute(
        session,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=1,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert retry_mock.call_args.kwargs["decision_at"] is decision_at
    assert job.attempt_count == 1
    assert job.updated_at == datetime(2026, 7, 26, 9, 3, tzinfo=UTC)
    assert job.updated_at.tzinfo is UTC
    session.flush.assert_called_once_with()
    session.add.assert_not_called()
    assert result.job is job
    assert result.attempt is attempt
    _assert_caller_owns_transaction(session)


def test_propagates_delivery_execution_failure_without_job_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    original_values = _job_values(job)
    session = _session_returning(job)
    execution_error = RuntimeError("delivery execution failed")
    execution_mock = Mock(side_effect=execution_error)
    retry_mock = Mock()
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^delivery execution failed$") as error_info:
        _execute(
            session,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )

    assert error_info.value is execution_error
    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    retry_mock.assert_not_called()
    decision_now.assert_not_called()
    assert _job_values(job) == original_values
    session.flush.assert_not_called()
    session.add.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_propagates_retry_policy_failure_without_job_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job()
    original_values = _job_values(job)
    attempt = _attempt(outcome="failed", attempt_number=2)
    decision_at = datetime(2026, 7, 26, 9, 4, tzinfo=UTC)
    session = _session_returning(job)
    execution_mock = Mock(return_value=attempt)
    retry_error = RuntimeError("retry decision failed")
    retry_mock = Mock(side_effect=retry_error)
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    with pytest.raises(RuntimeError, match="^retry decision failed$") as error_info:
        _execute(
            session,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )

    assert error_info.value is retry_error
    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=1,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert _job_values(job) == original_values
    session.flush.assert_not_called()
    session.add.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_propagates_job_flush_failure_after_preparing_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _job(attempt_count=1)
    attempt = _attempt(outcome="failed", attempt_number=7)
    decision_at = datetime(2026, 7, 26, 9, 5, tzinfo=UTC)
    retry_at = datetime(2026, 7, 26, 9, 5, 10, tzinfo=UTC)
    session = _session_returning(job)
    flush_error = RuntimeError("job flush failed")
    session.flush.side_effect = flush_error
    execution_mock = Mock(return_value=attempt)
    retry_mock = Mock(return_value=RetryDecision(status="pending", next_attempt_at=retry_at))
    monkeypatch.setattr(execution_service, "execute_webhook_delivery", execution_mock)
    monkeypatch.setattr(execution_service, "decide_webhook_retry", retry_mock)
    http_client, utc_now, _, monotonic_ns = _dependencies()
    decision_now = Mock(return_value=decision_at)

    with pytest.raises(RuntimeError, match="^job flush failed$") as error_info:
        _execute(
            session,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )

    assert error_info.value is flush_error
    _assert_execution_arguments(
        execution_mock,
        session=session,
        http_client=http_client,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    _assert_retry_arguments(
        retry_mock,
        attempt=attempt,
        cycle_attempt_number=2,
        decision_at=decision_at,
    )
    decision_now.assert_called_once_with()
    assert job.status == "pending"
    assert job.next_attempt_at is retry_at
    assert job.attempt_count == 2
    assert job.updated_at == decision_at
    session.flush.assert_called_once_with()
    session.add.assert_not_called()
    _assert_caller_owns_transaction(session)
