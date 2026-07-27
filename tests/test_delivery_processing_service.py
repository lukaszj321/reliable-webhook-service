import math
import uuid
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import Mock, call

import pytest
from sqlalchemy.orm import Session

import reliable_webhook_service.delivery_processing_service as processing_service
from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_job_execution_service import (
    WebhookDeliveryJobExecutionResult,
)
from reliable_webhook_service.delivery_processing_service import (
    WebhookDeliveryProcessingCycleResult,
    WebhookDeliveryProcessingJobResult,
    run_webhook_delivery_processing_cycle,
)
from reliable_webhook_service.models import WebhookDeliveryAttempt, WebhookDeliveryJob

CLAIMED_AT = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
TIMEOUT_SECONDS = 4.5
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5.0
MAX_DELAY_SECONDS = 300.0


class _RecordingSessionFactory:
    def __init__(self, sessions: list[Mock], events: list[str]) -> None:
        self.sessions = sessions
        self.events = events
        self.created: list[Mock] = []

    def __call__(self) -> Session:
        index = len(self.created)
        if index >= len(self.sessions):
            raise AssertionError("Unexpected session creation")
        session = self.sessions[index]
        self.created.append(session)
        self.events.append(f"create:{session._mock_name}")
        return cast(Session, session)


def _session(
    name: str,
    events: list[str],
    *,
    commit_error: Exception | None = None,
) -> Mock:
    session = Mock(spec=Session, name=name)

    def commit() -> None:
        events.append(f"{name}:commit")
        if commit_error is not None:
            raise commit_error

    session.commit.side_effect = commit
    session.rollback.side_effect = lambda: events.append(f"{name}:rollback")
    session.close.side_effect = lambda: events.append(f"{name}:close")
    return session


def _job(job_id: uuid.UUID) -> Mock:
    job = Mock(spec=WebhookDeliveryJob)
    job.id = job_id
    return job


def _execution_result(
    *,
    job_id: uuid.UUID,
    attempt_id: uuid.UUID,
    status: str,
    next_attempt_at: datetime | None,
) -> WebhookDeliveryJobExecutionResult:
    job = Mock(spec=WebhookDeliveryJob)
    job.id = job_id
    job.status = status
    job.next_attempt_at = next_attempt_at
    attempt = Mock(spec=WebhookDeliveryAttempt)
    attempt.id = attempt_id
    return WebhookDeliveryJobExecutionResult(job=job, attempt=attempt)


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
        "claimed_at": CLAIMED_AT,
        "limit": 3,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_attempts": MAX_ATTEMPTS,
        "base_delay_seconds": BASE_DELAY_SECONDS,
        "max_delay_seconds": MAX_DELAY_SECONDS,
        "utc_now": utc_now,
        "decision_now": decision_now,
        "monotonic_ns": monotonic_ns,
    }


def _dependencies() -> tuple[Mock, Mock, Mock, Mock]:
    return (
        Mock(spec=WebhookHttpClient),
        Mock(),
        Mock(),
        Mock(),
    )


def _assert_completion_call(
    completion_mock: Mock,
    index: int,
    *,
    session: Mock,
    job_id: uuid.UUID,
    http_client: Mock,
    utc_now: Mock,
    decision_now: Mock,
    monotonic_ns: Mock,
) -> None:
    assert completion_mock.call_args_list[index] == call(
        session,
        job_id=job_id,
        http_client=http_client,
        timeout_seconds=TIMEOUT_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        base_delay_seconds=BASE_DELAY_SECONDS,
        max_delay_seconds=MAX_DELAY_SECONDS,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": 0},
        {"limit": False},
        {"timeout_seconds": 0.0},
        {"timeout_seconds": math.inf},
        {"claimed_at": datetime(2026, 7, 28, 10, 0)},
    ],
)
def test_rejects_invalid_input_before_creating_session(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    session_factory = Mock()
    claim_mock = Mock()
    completion_mock = Mock()
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()
    arguments = _arguments(
        session_factory=session_factory,
        http_client=http_client,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )
    arguments.update(overrides)

    with pytest.raises(ValueError):
        run_webhook_delivery_processing_cycle(**arguments)

    session_factory.assert_not_called()
    claim_mock.assert_not_called()
    completion_mock.assert_not_called()
    assert http_client.mock_calls == []


def test_empty_cycle_commits_and_closes_claim_without_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim_session = _session("claim", events)
    session_factory = _RecordingSessionFactory([claim_session], events)
    claim_mock = Mock(side_effect=lambda *args, **kwargs: events.append("claim") or [])
    completion_mock = Mock()
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    result = run_webhook_delivery_processing_cycle(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )

    claim_mock.assert_called_once_with(claim_session, claimed_at=CLAIMED_AT, limit=3)
    claim_session.commit.assert_called_once_with()
    claim_session.rollback.assert_not_called()
    claim_session.close.assert_called_once_with()
    completion_mock.assert_not_called()
    assert len(session_factory.created) == 1
    assert events == ["create:claim", "claim", "claim:commit", "claim:close"]
    assert result == WebhookDeliveryProcessingCycleResult(
        claimed_job_ids=(),
        completed_jobs=(),
    )
    assert result.claimed_count == 0
    assert result.completed_count == 0
    assert http_client.mock_calls == []


def test_processes_claimed_jobs_in_order_with_one_fresh_session_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    sessions = [
        _session(name, events) for name in ("claim", "completion-1", "completion-2", "completion-3")
    ]
    claim_session, *completion_sessions = sessions
    session_factory = _RecordingSessionFactory(sessions, events)
    job_ids = [uuid.uuid4() for _ in range(3)]
    attempt_ids = [uuid.uuid4() for _ in range(3)]
    retry_at = datetime(2026, 7, 28, 10, 0, 5, tzinfo=UTC)
    claim_mock = Mock(
        side_effect=lambda *args, **kwargs: (
            events.append("claim") or [_job(job_id) for job_id in job_ids]
        )
    )
    results = [
        _execution_result(
            job_id=job_ids[0],
            attempt_id=attempt_ids[0],
            status="succeeded",
            next_attempt_at=None,
        ),
        _execution_result(
            job_id=job_ids[1],
            attempt_id=attempt_ids[1],
            status="pending",
            next_attempt_at=retry_at,
        ),
        _execution_result(
            job_id=job_ids[2],
            attempt_id=attempt_ids[2],
            status="dead_letter",
            next_attempt_at=None,
        ),
    ]

    def complete(*args: object, **kwargs: object) -> WebhookDeliveryJobExecutionResult:
        job_id = cast(uuid.UUID, kwargs["job_id"])
        events.append(f"complete:{job_id}")
        return results[job_ids.index(job_id)]

    completion_mock = Mock(side_effect=complete)
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    result = run_webhook_delivery_processing_cycle(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )

    assert result.claimed_job_ids == tuple(job_ids)
    assert [item.job_id for item in result.completed_jobs] == job_ids
    assert [item.attempt_id for item in result.completed_jobs] == attempt_ids
    assert [item.status for item in result.completed_jobs] == [
        "succeeded",
        "pending",
        "dead_letter",
    ]
    assert result.completed_jobs[1].next_attempt_at is retry_at
    assert result.claimed_count == result.completed_count == 3
    assert len(session_factory.created) == 4
    claim_session.commit.assert_called_once_with()
    claim_session.rollback.assert_not_called()
    claim_session.close.assert_called_once_with()
    for index, (session, job_id) in enumerate(zip(completion_sessions, job_ids, strict=True)):
        _assert_completion_call(
            completion_mock,
            index,
            session=session,
            job_id=job_id,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
        session.commit.assert_called_once_with()
        session.rollback.assert_not_called()
        session.close.assert_called_once_with()
    assert events == [
        "create:claim",
        "claim",
        "claim:commit",
        "claim:close",
        "create:completion-1",
        f"complete:{job_ids[0]}",
        "completion-1:commit",
        "completion-1:close",
        "create:completion-2",
        f"complete:{job_ids[1]}",
        "completion-2:commit",
        "completion-2:close",
        "create:completion-3",
        f"complete:{job_ids[2]}",
        "completion-3:commit",
        "completion-3:close",
    ]


def test_claim_service_failure_rolls_back_closes_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim_session = _session("claim", events)
    session_factory = _RecordingSessionFactory([claim_session], events)
    error = RuntimeError("claim failed")
    claim_mock = Mock(side_effect=error)
    completion_mock = Mock()
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^claim failed$") as error_info:
        run_webhook_delivery_processing_cycle(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    claim_session.commit.assert_not_called()
    claim_session.rollback.assert_called_once_with()
    claim_session.close.assert_called_once_with()
    completion_mock.assert_not_called()
    assert len(session_factory.created) == 1
    assert http_client.mock_calls == []


def test_claim_commit_failure_rolls_back_closes_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    error = RuntimeError("claim commit failed")
    claim_session = _session("claim", events, commit_error=error)
    session_factory = _RecordingSessionFactory([claim_session], events)
    claim_mock = Mock(return_value=[_job(uuid.uuid4())])
    completion_mock = Mock()
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^claim commit failed$") as error_info:
        run_webhook_delivery_processing_cycle(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    claim_session.commit.assert_called_once_with()
    claim_session.rollback.assert_called_once_with()
    claim_session.close.assert_called_once_with()
    completion_mock.assert_not_called()
    assert len(session_factory.created) == 1
    assert http_client.mock_calls == []


def test_first_completion_failure_rolls_back_current_session_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim_session = _session("claim", events)
    completion_session = _session("completion-1", events)
    session_factory = _RecordingSessionFactory([claim_session, completion_session], events)
    job_ids = [uuid.uuid4(), uuid.uuid4()]
    claim_mock = Mock(return_value=[_job(job_id) for job_id in job_ids])
    error = RuntimeError("completion failed")
    completion_mock = Mock(side_effect=error)
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^completion failed$") as error_info:
        run_webhook_delivery_processing_cycle(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    claim_session.commit.assert_called_once_with()
    claim_session.close.assert_called_once_with()
    claim_session.rollback.assert_not_called()
    completion_session.commit.assert_not_called()
    completion_session.rollback.assert_called_once_with()
    completion_session.close.assert_called_once_with()
    assert len(session_factory.created) == 2
    assert completion_mock.call_count == 1
    assert completion_mock.call_args.kwargs["job_id"] == job_ids[0]


def test_later_completion_failure_preserves_earlier_commit_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    sessions = [_session(name, events) for name in ("claim", "completion-1", "completion-2")]
    claim_session, first_session, second_session = sessions
    session_factory = _RecordingSessionFactory(sessions, events)
    job_ids = [uuid.uuid4() for _ in range(3)]
    claim_mock = Mock(return_value=[_job(job_id) for job_id in job_ids])
    error = RuntimeError("second completion failed")
    first_result = _execution_result(
        job_id=job_ids[0],
        attempt_id=uuid.uuid4(),
        status="succeeded",
        next_attempt_at=None,
    )
    completion_mock = Mock(side_effect=[first_result, error])
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^second completion failed$") as error_info:
        run_webhook_delivery_processing_cycle(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    claim_session.commit.assert_called_once_with()
    claim_session.rollback.assert_not_called()
    first_session.commit.assert_called_once_with()
    first_session.rollback.assert_not_called()
    first_session.close.assert_called_once_with()
    second_session.commit.assert_not_called()
    second_session.rollback.assert_called_once_with()
    second_session.close.assert_called_once_with()
    assert len(session_factory.created) == 3
    assert [item.kwargs["job_id"] for item in completion_mock.call_args_list] == job_ids[:2]


def test_completion_commit_failure_rolls_back_current_session_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim_session = _session("claim", events)
    error = RuntimeError("completion commit failed")
    completion_session = _session("completion-1", events, commit_error=error)
    session_factory = _RecordingSessionFactory([claim_session, completion_session], events)
    job_ids = [uuid.uuid4(), uuid.uuid4()]
    claim_mock = Mock(return_value=[_job(job_id) for job_id in job_ids])
    completion_mock = Mock(
        return_value=_execution_result(
            job_id=job_ids[0],
            attempt_id=uuid.uuid4(),
            status="pending",
            next_attempt_at=CLAIMED_AT + timedelta(seconds=5),
        )
    )
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    with pytest.raises(RuntimeError, match="^completion commit failed$") as error_info:
        run_webhook_delivery_processing_cycle(
            **_arguments(
                session_factory=session_factory,
                http_client=http_client,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        )

    assert error_info.value is error
    claim_session.commit.assert_called_once_with()
    claim_session.rollback.assert_not_called()
    completion_session.commit.assert_called_once_with()
    completion_session.rollback.assert_called_once_with()
    completion_session.close.assert_called_once_with()
    assert len(session_factory.created) == 2
    assert completion_mock.call_count == 1
    assert completion_mock.call_args.kwargs["job_id"] == job_ids[0]


def test_results_are_frozen_immutable_snapshots_without_orm_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    claim_session = _session("claim", events)
    completion_session = _session("completion-1", events)
    session_factory = _RecordingSessionFactory([claim_session, completion_session], events)
    job_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    retry_at = CLAIMED_AT + timedelta(seconds=10)
    claim_mock = Mock(return_value=[_job(job_id)])
    completion_mock = Mock(
        return_value=_execution_result(
            job_id=job_id,
            attempt_id=attempt_id,
            status="pending",
            next_attempt_at=retry_at,
        )
    )
    monkeypatch.setattr(processing_service, "claim_due_webhook_delivery_jobs", claim_mock)
    monkeypatch.setattr(processing_service, "execute_webhook_delivery_job", completion_mock)
    http_client, utc_now, decision_now, monotonic_ns = _dependencies()

    result = run_webhook_delivery_processing_cycle(
        **_arguments(
            session_factory=session_factory,
            http_client=http_client,
            utc_now=utc_now,
            decision_now=decision_now,
            monotonic_ns=monotonic_ns,
        )
    )
    job_result = result.completed_jobs[0]

    assert processing_service.__all__ == [
        "WebhookDeliveryProcessingCycleResult",
        "WebhookDeliveryProcessingJobResult",
        "run_webhook_delivery_processing_cycle",
    ]
    assert is_dataclass(result)
    assert is_dataclass(job_result)
    assert [field.name for field in fields(result)] == ["claimed_job_ids", "completed_jobs"]
    assert [field.name for field in fields(job_result)] == [
        "job_id",
        "attempt_id",
        "status",
        "next_attempt_at",
    ]
    assert isinstance(result.claimed_job_ids, tuple)
    assert isinstance(result.completed_jobs, tuple)
    assert not hasattr(result, "__dict__")
    assert not hasattr(job_result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.claimed_job_ids = ()
    with pytest.raises(FrozenInstanceError):
        job_result.status = "succeeded"
    assert result.claimed_count == len(result.claimed_job_ids) == 1
    assert result.completed_count == len(result.completed_jobs) == 1
    assert job_result == WebhookDeliveryProcessingJobResult(
        job_id=job_id,
        attempt_id=attempt_id,
        status="pending",
        next_attempt_at=retry_at,
    )
    assert all(
        not isinstance(value, (Session, WebhookDeliveryJob, WebhookDeliveryAttempt))
        for value in (
            *result.claimed_job_ids,
            job_result.job_id,
            job_result.attempt_id,
            job_result.status,
            job_result.next_attempt_at,
        )
    )
    assert events[-1] == "completion-1:close"
