import math
import uuid
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

import reliable_webhook_service.worker_iteration_service as worker_iteration_service
from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_job_recovery_service import (
    WebhookDeliveryJobRecoveryResult,
)
from reliable_webhook_service.delivery_processing_service import (
    WebhookDeliveryProcessingCycleResult,
    WebhookDeliveryProcessingJobResult,
)
from reliable_webhook_service.worker_iteration_service import (
    WebhookWorkerIterationResult,
    run_webhook_worker_iteration,
)

ITERATION_AT = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
STALE_BEFORE = ITERATION_AT - timedelta(minutes=30)
RECOVERY_LIMIT = 4
PROCESSING_LIMIT = 3
TIMEOUT_SECONDS = 4.5
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0


class _RecordingSessionFactory:
    def __init__(self, session: Mock, events: list[str]) -> None:
        self.session = session
        self.events = events
        self.created: list[Mock] = []

    def __call__(self) -> Session:
        if self.created:
            raise AssertionError("Unexpected recovery session creation")
        self.created.append(self.session)
        self.events.append("create:recovery")
        return cast(Session, self.session)


def _session(
    events: list[str],
    *,
    commit_error: Exception | None = None,
) -> Mock:
    session = Mock(spec=Session, name="recovery")

    def commit() -> None:
        events.append("recovery:commit")
        if commit_error is not None:
            raise commit_error

    session.commit.side_effect = commit
    session.rollback.side_effect = lambda: events.append("recovery:rollback")
    session.close.side_effect = lambda: events.append("recovery:close")
    return session


def _dependencies() -> tuple[Mock, Mock, Mock, Mock]:
    return (
        Mock(spec=WebhookHttpClient),
        Mock(name="utc_now"),
        Mock(name="decision_now"),
        Mock(name="monotonic_ns"),
    )


def _arguments(
    *,
    session_factory: object,
    http_client: Mock,
    utc_now: Mock,
    decision_now: Mock,
    monotonic_ns: Mock,
) -> dict[str, object]:
    return {
        "session_factory": session_factory,
        "http_client": http_client,
        "iteration_at": ITERATION_AT,
        "stale_before": STALE_BEFORE,
        "recovery_limit": RECOVERY_LIMIT,
        "processing_limit": PROCESSING_LIMIT,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
        "base_delay_seconds": BASE_DELAY_SECONDS,
        "max_delay_seconds": MAX_DELAY_SECONDS,
        "utc_now": utc_now,
        "decision_now": decision_now,
        "monotonic_ns": monotonic_ns,
    }


def _empty_recovery_result() -> WebhookDeliveryJobRecoveryResult:
    return WebhookDeliveryJobRecoveryResult(recovered_job_ids=())


def _empty_processing_result() -> WebhookDeliveryProcessingCycleResult:
    return WebhookDeliveryProcessingCycleResult(
        claimed_job_ids=(),
        completed_jobs=(),
    )


@pytest.mark.parametrize(
    ("overrides", "expected_argument"),
    [
        ({"recovery_limit": False}, "recovery_limit"),
        ({"recovery_limit": 0}, "recovery_limit"),
        ({"recovery_limit": -1}, "recovery_limit"),
        ({"recovery_limit": 1.5}, "recovery_limit"),
        ({"processing_limit": False}, "processing_limit"),
        ({"processing_limit": 0}, "processing_limit"),
        ({"processing_limit": -1}, "processing_limit"),
        ({"processing_limit": 1.5}, "processing_limit"),
        ({"timeout_seconds": False}, "timeout_seconds"),
        ({"timeout_seconds": 0.0}, "timeout_seconds"),
        ({"timeout_seconds": -1.0}, "timeout_seconds"),
        ({"timeout_seconds": math.inf}, "timeout_seconds"),
        ({"timeout_seconds": -math.inf}, "timeout_seconds"),
        ({"timeout_seconds": math.nan}, "timeout_seconds"),
        ({"timeout_seconds": "invalid"}, "timeout_seconds"),
        ({"iteration_at": datetime(2026, 7, 31, 12, 0)}, "iteration_at"),
        ({"iteration_at": "invalid"}, "iteration_at"),
        ({"stale_before": datetime(2026, 7, 31, 11, 30)}, "stale_before"),
        ({"stale_before": "invalid"}, "stale_before"),
        (
            {
                "iteration_at": STALE_BEFORE - timedelta(seconds=1),
                "stale_before": STALE_BEFORE,
            },
            "iteration_at",
        ),
    ],
)
def test_rejects_invalid_orchestration_arguments_before_session_creation(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected_argument: str,
) -> None:
    session_factory = Mock()
    recovery_mock = Mock()
    processing_mock = Mock()
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()
    arguments = _arguments(
        session_factory=session_factory,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )
    arguments.update(overrides)

    with pytest.raises(ValueError, match=expected_argument):
        run_webhook_worker_iteration(**arguments)

    session_factory.assert_not_called()
    recovery_mock.assert_not_called()
    processing_mock.assert_not_called()
    assert http_client.mock_calls == []
    utc_now.assert_not_called()
    decision_now.assert_not_called()
    monotonic_ns.assert_not_called()


def test_orchestrates_recovery_then_processing_with_exact_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recovery_session = _session(events)
    session_factory = _RecordingSessionFactory(recovery_session, events)
    recovered_ids = (uuid.uuid4(), uuid.uuid4())
    recovery_result = WebhookDeliveryJobRecoveryResult(recovered_job_ids=recovered_ids)
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    processing_result = WebhookDeliveryProcessingCycleResult(
        claimed_job_ids=(job_id,),
        completed_jobs=(
            WebhookDeliveryProcessingJobResult(
                job_id=job_id,
                attempt_id=attempt_id,
                status="succeeded",
                next_attempt_at=None,
            ),
        ),
    )
    iteration_at = datetime(
        2026,
        7,
        31,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    stale_before = datetime(
        2026,
        7,
        31,
        6,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    expected_iteration_at = iteration_at.astimezone(UTC)
    expected_stale_before = stale_before.astimezone(UTC)
    recovery_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("recover") or recovery_result
    )
    processing_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("process") or processing_result
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    result = run_webhook_worker_iteration(
        session_factory=session_factory,
        http_client=http_client,
        iteration_at=iteration_at,
        stale_before=stale_before,
        recovery_limit=RECOVERY_LIMIT,
        processing_limit=PROCESSING_LIMIT,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    recovery_mock.assert_called_once_with(
        recovery_session,
        stale_before=expected_stale_before,
        recovered_at=expected_iteration_at,
        limit=RECOVERY_LIMIT,
    )
    processing_mock.assert_called_once_with(
        session_factory=session_factory,
        http_client=http_client,
        claimed_at=expected_iteration_at,
        limit=PROCESSING_LIMIT,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )
    recovery_session.commit.assert_called_once_with()
    recovery_session.rollback.assert_not_called()
    recovery_session.close.assert_called_once_with()
    assert len(session_factory.created) == 1
    assert events == [
        "create:recovery",
        "recover",
        "recovery:commit",
        "recovery:close",
        "process",
    ]
    assert result.recovery is recovery_result
    assert result.processing is processing_result
    assert result.recovered_count == 2
    assert result.claimed_count == 1
    assert result.completed_count == 1


def test_empty_recovery_still_commits_closes_and_runs_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recovery_session = _session(events)
    session_factory = _RecordingSessionFactory(recovery_session, events)
    recovery_result = _empty_recovery_result()
    processing_result = _empty_processing_result()
    recovery_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("recover") or recovery_result
    )
    processing_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("process") or processing_result
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    result = run_webhook_worker_iteration(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )

    recovery_mock.assert_called_once()
    processing_mock.assert_called_once()
    recovery_session.commit.assert_called_once_with()
    recovery_session.rollback.assert_not_called()
    recovery_session.close.assert_called_once_with()
    assert events == [
        "create:recovery",
        "recover",
        "recovery:commit",
        "recovery:close",
        "process",
    ]
    assert result.recovery is recovery_result
    assert result.processing is processing_result
    assert result.recovered_count == result.claimed_count == result.completed_count == 0


def test_recovery_failure_rolls_back_closes_and_propagates_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recovery_session = _session(events)
    session_factory = _RecordingSessionFactory(recovery_session, events)
    error = RuntimeError("recovery failed")

    def fail_recovery(*args: object, **kwargs: object) -> WebhookDeliveryJobRecoveryResult:
        events.append("recover")
        raise error

    recovery_mock = Mock(side_effect=fail_recovery)
    processing_mock = Mock()
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^recovery failed$") as error_info:
        run_webhook_worker_iteration(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    recovery_mock.assert_called_once()
    processing_mock.assert_not_called()
    recovery_session.commit.assert_not_called()
    recovery_session.rollback.assert_called_once_with()
    recovery_session.close.assert_called_once_with()
    assert len(session_factory.created) == 1
    assert events == [
        "create:recovery",
        "recover",
        "recovery:rollback",
        "recovery:close",
    ]
    assert http_client.mock_calls == []


def test_recovery_commit_failure_rolls_back_closes_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    error = RuntimeError("recovery commit failed")
    recovery_session = _session(events, commit_error=error)
    session_factory = _RecordingSessionFactory(recovery_session, events)
    recovery_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("recover") or _empty_recovery_result()
    )
    processing_mock = Mock()
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^recovery commit failed$") as error_info:
        run_webhook_worker_iteration(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    recovery_mock.assert_called_once()
    processing_mock.assert_not_called()
    recovery_session.commit.assert_called_once_with()
    recovery_session.rollback.assert_called_once_with()
    recovery_session.close.assert_called_once_with()
    assert len(session_factory.created) == 1
    assert events == [
        "create:recovery",
        "recover",
        "recovery:commit",
        "recovery:rollback",
        "recovery:close",
    ]
    assert http_client.mock_calls == []


def test_processing_failure_follows_durable_recovery_and_propagates_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    recovery_session = _session(events)
    session_factory = _RecordingSessionFactory(recovery_session, events)
    recovery_mock = Mock(
        side_effect=lambda *args, **kwargs: events.append("recover") or _empty_recovery_result()
    )
    error = RuntimeError("processing failed")

    def fail_processing(*args: object, **kwargs: object) -> WebhookDeliveryProcessingCycleResult:
        events.append("process")
        raise error

    processing_mock = Mock(side_effect=fail_processing)
    monkeypatch.setattr(
        worker_iteration_service,
        "recover_stale_webhook_delivery_jobs",
        recovery_mock,
    )
    monkeypatch.setattr(
        worker_iteration_service,
        "run_webhook_delivery_processing_cycle",
        processing_mock,
    )
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^processing failed$") as error_info:
        run_webhook_worker_iteration(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    recovery_mock.assert_called_once()
    processing_mock.assert_called_once()
    recovery_session.commit.assert_called_once_with()
    recovery_session.rollback.assert_not_called()
    recovery_session.close.assert_called_once_with()
    assert len(session_factory.created) == 1
    assert events == [
        "create:recovery",
        "recover",
        "recovery:commit",
        "recovery:close",
        "process",
    ]
    assert http_client.mock_calls == []


def test_result_is_frozen_composition_without_mutable_or_orm_values() -> None:
    recovery_result = WebhookDeliveryJobRecoveryResult(
        recovered_job_ids=(uuid.uuid4(), uuid.uuid4())
    )
    job_id = uuid.uuid4()
    processing_result = WebhookDeliveryProcessingCycleResult(
        claimed_job_ids=(job_id,),
        completed_jobs=(
            WebhookDeliveryProcessingJobResult(
                job_id=job_id,
                attempt_id=uuid.uuid4(),
                status="succeeded",
                next_attempt_at=None,
            ),
        ),
    )
    result = WebhookWorkerIterationResult(
        recovery=recovery_result,
        processing=processing_result,
    )

    assert worker_iteration_service.__all__ == [
        "WebhookWorkerIterationResult",
        "run_webhook_worker_iteration",
    ]
    assert is_dataclass(result)
    assert [field.name for field in fields(result)] == ["recovery", "processing"]
    assert result.recovery is recovery_result
    assert result.processing is processing_result
    assert result.recovered_count == 2
    assert result.claimed_count == 1
    assert result.completed_count == 1
    assert isinstance(type(result).recovered_count, property)
    assert isinstance(type(result).claimed_count, property)
    assert isinstance(type(result).completed_count, property)
    assert type(result).recovered_count.fset is None
    assert type(result).claimed_count.fset is None
    assert type(result).completed_count.fset is None
    assert not hasattr(result, "__dict__")
    assert not any(
        isinstance(value, (Session, list, dict))
        for value in (
            result.recovery,
            result.processing,
            result.recovery.recovered_job_ids,
            result.processing.claimed_job_ids,
            result.processing.completed_jobs,
        )
    )
    with pytest.raises(FrozenInstanceError):
        result.recovery = _empty_recovery_result()
