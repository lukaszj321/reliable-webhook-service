import logging
import math
import uuid
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock, call

import pytest
from sqlalchemy.orm import Session

import reliable_webhook_service.worker_loop_service as worker_loop_service
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
)
from reliable_webhook_service.worker_loop_service import (
    WebhookWorkerRunResult,
    run_webhook_worker,
)

POLL_INTERVAL_SECONDS = 2.5
STALE_PROCESSING_TIMEOUT_SECONDS = 90.0
RECOVERY_LIMIT = 7
PROCESSING_LIMIT = 3
TIMEOUT_SECONDS = 4.5
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0
ITERATION_AT = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _iteration_result(
    *,
    recovered_count: int,
    claimed_count: int,
    completed_count: int,
) -> WebhookWorkerIterationResult:
    recovered_ids = tuple(uuid.uuid4() for _ in range(recovered_count))
    claimed_ids = tuple(uuid.uuid4() for _ in range(claimed_count))
    completed_jobs = tuple(
        WebhookDeliveryProcessingJobResult(
            job_id=claimed_ids[index],
            attempt_id=uuid.uuid4(),
            status="succeeded",
            next_attempt_at=None,
        )
        for index in range(completed_count)
    )
    return WebhookWorkerIterationResult(
        recovery=WebhookDeliveryJobRecoveryResult(
            recovered_job_ids=recovered_ids,
        ),
        processing=WebhookDeliveryProcessingCycleResult(
            claimed_job_ids=claimed_ids,
            completed_jobs=completed_jobs,
        ),
    )


def _dependencies() -> tuple[Mock, Mock, Mock, Mock, Mock, Mock, Mock, Mock]:
    return (
        Mock(name="session_factory"),
        Mock(spec=WebhookHttpClient),
        Mock(name="stop_requested"),
        Mock(name="wait"),
        Mock(name="iteration_now"),
        Mock(name="utc_now"),
        Mock(name="decision_now"),
        Mock(name="monotonic_ns"),
    )


def _arguments(
    *,
    session_factory: Mock,
    http_client: Mock,
    stop_requested: Mock,
    wait: Mock,
    iteration_now: Mock,
    utc_now: Mock,
    decision_now: Mock,
    monotonic_ns: Mock,
) -> dict[str, object]:
    return {
        "session_factory": session_factory,
        "http_client": http_client,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "stale_processing_timeout_seconds": STALE_PROCESSING_TIMEOUT_SECONDS,
        "recovery_limit": RECOVERY_LIMIT,
        "processing_limit": PROCESSING_LIMIT,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
        "base_delay_seconds": BASE_DELAY_SECONDS,
        "max_delay_seconds": MAX_DELAY_SECONDS,
        "stop_requested": stop_requested,
        "wait": wait,
        "iteration_now": iteration_now,
        "utc_now": utc_now,
        "decision_now": decision_now,
        "monotonic_ns": monotonic_ns,
    }


def _enable_worker_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_logger = logging.getLogger(worker_loop_service.__name__)
    monkeypatch.setattr(worker_logger, "disabled", False)


@pytest.mark.parametrize(
    ("overrides", "expected_argument"),
    [
        ({"poll_interval_seconds": False}, "poll_interval_seconds"),
        ({"poll_interval_seconds": 0.0}, "poll_interval_seconds"),
        ({"poll_interval_seconds": -1.0}, "poll_interval_seconds"),
        ({"poll_interval_seconds": math.inf}, "poll_interval_seconds"),
        ({"poll_interval_seconds": -math.inf}, "poll_interval_seconds"),
        ({"poll_interval_seconds": math.nan}, "poll_interval_seconds"),
        ({"poll_interval_seconds": "invalid"}, "poll_interval_seconds"),
        (
            {"stale_processing_timeout_seconds": False},
            "stale_processing_timeout_seconds",
        ),
        ({"stale_processing_timeout_seconds": 0.0}, "stale_processing_timeout_seconds"),
        ({"stale_processing_timeout_seconds": -1.0}, "stale_processing_timeout_seconds"),
        ({"stale_processing_timeout_seconds": math.inf}, "stale_processing_timeout_seconds"),
        ({"stale_processing_timeout_seconds": math.nan}, "stale_processing_timeout_seconds"),
        ({"stale_processing_timeout_seconds": "invalid"}, "stale_processing_timeout_seconds"),
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
        ({"timeout_seconds": math.nan}, "timeout_seconds"),
        ({"timeout_seconds": "invalid"}, "timeout_seconds"),
    ],
)
def test_rejects_invalid_orchestration_before_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    expected_argument: str,
) -> None:
    dependencies = _dependencies()
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = dependencies
    iteration_mock = Mock()
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)
    arguments = _arguments(
        session_factory=session_factory,
        http_client=http_client,
        stop_requested=stop_requested,
        wait=wait,
        iteration_now=iteration_now,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )
    arguments.update(overrides)

    with pytest.raises(ValueError, match=expected_argument):
        run_webhook_worker(**arguments)

    for dependency in dependencies:
        dependency.assert_not_called()
    iteration_mock.assert_not_called()


def test_stop_before_first_iteration_returns_zero_result_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dependencies = _dependencies()
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = dependencies
    stop_requested.return_value = True
    iteration_mock = Mock()
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)
    _enable_worker_logger(monkeypatch)

    with caplog.at_level(logging.INFO, logger=worker_loop_service.__name__):
        result = run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert result == WebhookWorkerRunResult(
        iterations_started=0,
        iterations_completed=0,
        total_recovered_count=0,
        total_claimed_count=0,
        total_completed_count=0,
        shutdown_requested=True,
        final_iteration=None,
    )
    stop_requested.assert_called_once_with()
    iteration_now.assert_not_called()
    wait.assert_not_called()
    iteration_mock.assert_not_called()
    session_factory.assert_not_called()
    assert http_client.mock_calls == []
    assert "Webhook worker loop starting" in caplog.messages
    assert "Webhook worker shutdown requested before iteration" in caplog.messages
    assert "Webhook worker stopped gracefully after 0 completed iterations" in caplog.messages


@pytest.mark.parametrize(
    "invalid_timestamp",
    [
        "invalid",
        datetime(2026, 8, 5, 12, 0),
    ],
)
def test_rejects_invalid_iteration_timestamp_before_iteration_or_wait(
    monkeypatch: pytest.MonkeyPatch,
    invalid_timestamp: object,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.return_value = False
    iteration_now.return_value = invalid_timestamp
    iteration_mock = Mock()
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)

    with pytest.raises(ValueError, match="iteration_now"):
        run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    stop_requested.assert_called_once_with()
    iteration_now.assert_called_once_with()
    iteration_mock.assert_not_called()
    wait.assert_not_called()
    session_factory.assert_not_called()
    assert http_client.mock_calls == []


def test_single_iteration_normalizes_time_forwards_exactly_and_stops_afterward(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.side_effect = [False, True]
    offset_time = datetime(
        2026,
        8,
        5,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    iteration_now.return_value = offset_time
    expected_time = offset_time.astimezone(UTC)
    iteration_result = _iteration_result(
        recovered_count=2,
        claimed_count=3,
        completed_count=2,
    )
    iteration_mock = Mock(return_value=iteration_result)
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)
    _enable_worker_logger(monkeypatch)

    with caplog.at_level(logging.INFO, logger=worker_loop_service.__name__):
        result = run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    iteration_now.assert_called_once_with()
    iteration_mock.assert_called_once_with(
        session_factory=session_factory,
        http_client=http_client,
        iteration_at=expected_time,
        stale_before=expected_time - timedelta(seconds=STALE_PROCESSING_TIMEOUT_SECONDS),
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
    assert result.iterations_started == result.iterations_completed == 1
    assert result.total_recovered_count == 2
    assert result.total_claimed_count == 3
    assert result.total_completed_count == 2
    assert result.shutdown_requested is True
    assert result.final_iteration is iteration_result
    wait.assert_not_called()
    assert "Webhook worker iteration 1 starting" in caplog.messages
    assert (
        "Webhook worker iteration 1 completed: recovered=2 claimed=3 completed=2" in caplog.messages
    )
    assert "Webhook worker shutdown requested after iteration" in caplog.messages
    assert "Webhook worker stopped gracefully after 1 completed iterations" in caplog.messages


def test_wait_true_stops_after_completed_iteration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.side_effect = [False, False]
    wait.return_value = True
    iteration_now.return_value = ITERATION_AT
    iteration_result = _iteration_result(
        recovered_count=0,
        claimed_count=1,
        completed_count=1,
    )
    iteration_mock = Mock(return_value=iteration_result)
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)
    _enable_worker_logger(monkeypatch)

    with caplog.at_level(logging.INFO, logger=worker_loop_service.__name__):
        result = run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    wait.assert_called_once_with(POLL_INTERVAL_SECONDS)
    iteration_mock.assert_called_once()
    assert result.shutdown_requested is True
    assert result.final_iteration is iteration_result
    assert "Webhook worker shutdown requested during wait" in caplog.messages
    assert "Webhook worker stopped gracefully after 1 completed iterations" in caplog.messages


def test_wait_false_returns_to_stop_check_before_next_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.side_effect = [False, False, True]
    wait.return_value = False
    iteration_now.return_value = ITERATION_AT
    iteration_result = _iteration_result(
        recovered_count=1,
        claimed_count=1,
        completed_count=1,
    )
    iteration_mock = Mock(return_value=iteration_result)
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)

    result = run_webhook_worker(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            stop_requested=stop_requested,
            wait=wait,
            iteration_now=iteration_now,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )

    assert stop_requested.call_count == 3
    wait.assert_called_once_with(POLL_INTERVAL_SECONDS)
    iteration_now.assert_called_once_with()
    iteration_mock.assert_called_once()
    assert result.iterations_completed == 1
    assert result.shutdown_requested is True


def test_multiple_iterations_aggregate_counts_and_preserve_last_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.side_effect = [False, False, False, True]
    wait.return_value = False
    second_time = ITERATION_AT + timedelta(seconds=POLL_INTERVAL_SECONDS)
    iteration_now.side_effect = [ITERATION_AT, second_time]
    first = _iteration_result(
        recovered_count=2,
        claimed_count=1,
        completed_count=1,
    )
    second = _iteration_result(
        recovered_count=3,
        claimed_count=4,
        completed_count=2,
    )
    iteration_mock = Mock(side_effect=[first, second])
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)

    result = run_webhook_worker(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            stop_requested=stop_requested,
            wait=wait,
            iteration_now=iteration_now,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )

    assert iteration_now.call_count == 2
    assert iteration_mock.call_count == 2
    assert iteration_mock.call_args_list[0].kwargs["iteration_at"] == ITERATION_AT
    assert iteration_mock.call_args_list[1].kwargs["iteration_at"] == second_time
    assert wait.call_args_list == [call(POLL_INTERVAL_SECONDS)]
    assert result.iterations_started == result.iterations_completed == 2
    assert result.total_recovered_count == 5
    assert result.total_claimed_count == 5
    assert result.total_completed_count == 3
    assert result.shutdown_requested is True
    assert result.final_iteration is second


def test_iteration_failure_logs_safely_and_propagates_without_wait(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.return_value = False
    iteration_now.return_value = ITERATION_AT
    error = RuntimeError(
        "payload-marker https://secret.example postgresql://user:password@localhost/database"
    )
    iteration_mock = Mock(side_effect=error)
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)
    _enable_worker_logger(monkeypatch)

    with caplog.at_level(logging.INFO, logger=worker_loop_service.__name__):
        with pytest.raises(RuntimeError) as error_info:
            run_webhook_worker(
                **_arguments(
                    session_factory=session_factory,
                    http_client=http_client,
                    stop_requested=stop_requested,
                    wait=wait,
                    iteration_now=iteration_now,
                    utc_now=utc_now,
                    decision_now=decision_now,
                    monotonic_ns=monotonic_ns,
                )
            )

    assert error_info.value is error
    iteration_mock.assert_called_once()
    wait.assert_not_called()
    assert stop_requested.call_count == 1
    log_text = caplog.text
    assert "Webhook worker iteration fatal failure: RuntimeError" in log_text
    assert "payload-marker" not in log_text
    assert "https://secret.example" not in log_text
    assert "postgresql://" not in log_text
    assert "password" not in log_text


def test_wait_failure_propagates_without_next_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    stop_requested.side_effect = [False, False]
    iteration_now.return_value = ITERATION_AT
    iteration_mock = Mock(
        return_value=_iteration_result(
            recovered_count=0,
            claimed_count=0,
            completed_count=0,
        )
    )
    error = RuntimeError("wait failed")
    wait.side_effect = error
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)

    with pytest.raises(RuntimeError) as error_info:
        run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    iteration_mock.assert_called_once()
    iteration_now.assert_called_once()
    wait.assert_called_once_with(POLL_INTERVAL_SECONDS)
    assert stop_requested.call_count == 2


@pytest.mark.parametrize("failure_position", ["before", "after"])
def test_stop_callback_failure_propagates_without_wait_or_extra_iteration(
    monkeypatch: pytest.MonkeyPatch,
    failure_position: str,
) -> None:
    (
        session_factory,
        http_client,
        stop_requested,
        wait,
        iteration_now,
        utc_now,
        decision_now,
        monotonic_ns,
    ) = _dependencies()
    error = RuntimeError("stop callback failed")
    iteration_result = _iteration_result(
        recovered_count=0,
        claimed_count=0,
        completed_count=0,
    )
    if failure_position == "before":
        stop_requested.side_effect = error
    else:
        stop_requested.side_effect = [False, error]
        iteration_now.return_value = ITERATION_AT
    iteration_mock = Mock(return_value=iteration_result)
    monkeypatch.setattr(worker_loop_service, "run_webhook_worker_iteration", iteration_mock)

    with pytest.raises(RuntimeError) as error_info:
        run_webhook_worker(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                stop_requested=stop_requested,
                wait=wait,
                iteration_now=iteration_now,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    wait.assert_not_called()
    assert iteration_mock.call_count == (0 if failure_position == "before" else 1)


def test_result_contract_is_frozen_slotted_and_contains_no_mutable_or_orm_values() -> None:
    iteration_result = _iteration_result(
        recovered_count=1,
        claimed_count=1,
        completed_count=1,
    )
    result = WebhookWorkerRunResult(
        iterations_started=1,
        iterations_completed=1,
        total_recovered_count=1,
        total_claimed_count=1,
        total_completed_count=1,
        shutdown_requested=True,
        final_iteration=iteration_result,
    )

    assert worker_loop_service.__all__ == [
        "WebhookWorkerRunResult",
        "run_webhook_worker",
    ]
    assert is_dataclass(result)
    assert [field.name for field in fields(result)] == [
        "iterations_started",
        "iterations_completed",
        "total_recovered_count",
        "total_claimed_count",
        "total_completed_count",
        "shutdown_requested",
        "final_iteration",
    ]
    assert result.final_iteration is iteration_result
    assert not hasattr(result, "__dict__")
    assert not any(
        isinstance(value, (list, dict, set, Session))
        for value in (
            result.iterations_started,
            result.iterations_completed,
            result.total_recovered_count,
            result.total_claimed_count,
            result.total_completed_count,
            result.shutdown_requested,
            result.final_iteration,
        )
    )
    with pytest.raises(FrozenInstanceError):
        result.iterations_started = 2


def test_module_delegates_only_to_worker_iteration() -> None:
    assert not hasattr(worker_loop_service, "recover_stale_webhook_delivery_jobs")
    assert not hasattr(worker_loop_service, "run_webhook_delivery_processing_cycle")
    assert not hasattr(worker_loop_service, "claim_due_webhook_delivery_jobs")
    assert not hasattr(worker_loop_service, "execute_webhook_delivery_job")
    assert not hasattr(worker_loop_service, "execute_webhook_delivery")
    assert not hasattr(worker_loop_service, "SessionFactory")
    assert not hasattr(worker_loop_service, "engine")
