import uuid
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import reliable_webhook_service.replay_service as replay_service
from reliable_webhook_service.models import (
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.replay_service import (
    WebhookReplayDeliveryJobNotFoundError,
    WebhookReplayDeliveryJobNotReplayableError,
    WebhookReplayEndpointInactiveError,
    WebhookReplayEndpointNotFoundError,
    WebhookReplayEventNotFoundError,
    WebhookReplayResult,
    replay_webhook_event,
)

EVENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
ENDPOINT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
JOB_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CREATED_AT = datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
UPDATED_AT = datetime(2026, 7, 29, 8, 1, tzinfo=UTC)


def _event() -> WebhookEvent:
    return WebhookEvent(
        id=EVENT_ID,
        endpoint_id=ENDPOINT_ID,
        event_type="replay.contract",
        payload={"contract": True},
        created_at=CREATED_AT,
    )


def _endpoint(*, is_active: bool = True) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=ENDPOINT_ID,
        name="Replay contract endpoint",
        target_url="https://example.test/replay-contract",
        is_active=is_active,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _job(
    *,
    status: str = "succeeded",
    attempt_count: int = 4,
) -> WebhookDeliveryJob:
    return WebhookDeliveryJob(
        id=JOB_ID,
        event_id=EVENT_ID,
        status=status,
        next_attempt_at=None if status in {"succeeded", "dead_letter"} else UPDATED_AT,
        attempt_count=attempt_count,
        created_at=CREATED_AT,
        updated_at=UPDATED_AT,
    )


def _session_returning(
    *,
    event: WebhookEvent | None = None,
    endpoint: WebhookEndpoint | None = None,
    job: WebhookDeliveryJob | None = None,
) -> Mock:
    session = Mock(spec=Session)

    def get(model: type[object], identifier: uuid.UUID) -> object | None:
        if model is WebhookEvent:
            assert identifier == EVENT_ID
            return event
        if model is WebhookEndpoint:
            assert identifier == ENDPOINT_ID
            return endpoint
        raise AssertionError(f"Unexpected model lookup: {model}")

    session.get.side_effect = get
    session.scalar.return_value = job
    return session


def _assert_caller_owns_transaction(session: Mock) -> None:
    session.commit.assert_not_called()
    session.rollback.assert_not_called()
    session.refresh.assert_not_called()
    session.close.assert_not_called()


def _assert_no_creation_or_external_work(session: Mock) -> None:
    session.add.assert_not_called()
    assert "execute_webhook_delivery" not in replay_service.__dict__
    assert "claim_due_webhook_delivery_jobs" not in replay_service.__dict__
    assert "WebhookDeliveryAttempt" not in replay_service.__dict__


def test_rejects_naive_replayed_at_before_sql() -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        ValueError,
        match="^replayed_at must be a timezone-aware datetime$",
    ):
        replay_webhook_event(
            session,
            event_id=EVENT_ID,
            replayed_at=datetime(2026, 7, 29, 10, 0),
        )

    session.get.assert_not_called()
    session.scalar.assert_not_called()
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


@pytest.mark.parametrize("terminal_status", ["succeeded", "dead_letter"])
def test_replays_terminal_job_and_normalizes_timestamp(
    terminal_status: str,
) -> None:
    event = _event()
    endpoint = _endpoint()
    job = _job(status=terminal_status)
    session = _session_returning(event=event, endpoint=endpoint, job=job)
    replayed_at = datetime(
        2026,
        7,
        29,
        12,
        30,
        tzinfo=timezone(timedelta(hours=2)),
    )
    expected_replayed_at = datetime(2026, 7, 29, 10, 30, tzinfo=UTC)
    original_identity = (job.id, job.event_id, job.created_at)

    result = replay_webhook_event(
        session,
        event_id=EVENT_ID,
        replayed_at=replayed_at,
    )

    assert job.status == "pending"
    assert job.next_attempt_at == expected_replayed_at
    assert job.updated_at == expected_replayed_at
    assert job.attempt_count == 0
    assert (job.id, job.event_id, job.created_at) == original_identity
    assert result == WebhookReplayResult(
        event_id=EVENT_ID,
        delivery_job_id=JOB_ID,
        status="pending",
        next_attempt_at=expected_replayed_at,
    )
    assert not isinstance(result.event_id, WebhookEvent)
    assert not isinstance(result.delivery_job_id, WebhookDeliveryJob)
    session.flush.assert_called_once_with()
    _assert_caller_owns_transaction(session)
    _assert_no_creation_or_external_work(session)

    statement = session.scalar.call_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "webhook_delivery_jobs.event_id" in compiled


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_rejects_active_job_without_flush(status: str) -> None:
    job = _job(status=status)
    original_values = (
        job.id,
        job.event_id,
        job.status,
        job.next_attempt_at,
        job.attempt_count,
        job.created_at,
        job.updated_at,
    )
    session = _session_returning(event=_event(), endpoint=_endpoint(), job=job)

    with pytest.raises(
        WebhookReplayDeliveryJobNotReplayableError,
        match="^Webhook delivery job is not replayable$",
    ):
        replay_webhook_event(
            session,
            event_id=EVENT_ID,
            replayed_at=UPDATED_AT,
        )

    assert (
        job.id,
        job.event_id,
        job.status,
        job.next_attempt_at,
        job.attempt_count,
        job.created_at,
        job.updated_at,
    ) == original_values
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_rejects_missing_event() -> None:
    session = _session_returning()

    with pytest.raises(
        WebhookReplayEventNotFoundError,
        match="^Webhook event not found$",
    ):
        replay_webhook_event(session, event_id=EVENT_ID, replayed_at=UPDATED_AT)

    session.scalar.assert_not_called()
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_rejects_missing_endpoint() -> None:
    session = _session_returning(event=_event())

    with pytest.raises(
        WebhookReplayEndpointNotFoundError,
        match="^Webhook endpoint not found$",
    ):
        replay_webhook_event(session, event_id=EVENT_ID, replayed_at=UPDATED_AT)

    session.scalar.assert_not_called()
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_rejects_inactive_endpoint() -> None:
    session = _session_returning(event=_event(), endpoint=_endpoint(is_active=False))

    with pytest.raises(
        WebhookReplayEndpointInactiveError,
        match="^Webhook endpoint is inactive$",
    ):
        replay_webhook_event(session, event_id=EVENT_ID, replayed_at=UPDATED_AT)

    session.scalar.assert_not_called()
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_rejects_missing_delivery_job() -> None:
    session = _session_returning(event=_event(), endpoint=_endpoint())

    with pytest.raises(
        WebhookReplayDeliveryJobNotFoundError,
        match="^Webhook delivery job not found$",
    ):
        replay_webhook_event(session, event_id=EVENT_ID, replayed_at=UPDATED_AT)

    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)


def test_result_is_frozen_slotted_and_contains_only_scalar_snapshot() -> None:
    result = WebhookReplayResult(
        event_id=EVENT_ID,
        delivery_job_id=JOB_ID,
        status="pending",
        next_attempt_at=UPDATED_AT,
    )

    assert [field.name for field in fields(WebhookReplayResult)] == [
        "event_id",
        "delivery_job_id",
        "status",
        "next_attempt_at",
    ]
    assert not hasattr(result, "__dict__")
    assert not any(
        isinstance(value, (WebhookEvent, WebhookEndpoint, WebhookDeliveryJob))
        for value in (
            result.event_id,
            result.delivery_job_id,
            result.status,
            result.next_attempt_at,
        )
    )
    with pytest.raises(FrozenInstanceError):
        result.status = "succeeded"  # type: ignore[misc]


def test_database_error_propagates_unchanged() -> None:
    session = Mock(spec=Session)
    database_error = SQLAlchemyError("database unavailable")
    session.get.side_effect = database_error

    with pytest.raises(SQLAlchemyError) as error_info:
        replay_webhook_event(session, event_id=EVENT_ID, replayed_at=UPDATED_AT)

    assert error_info.value is database_error
    session.flush.assert_not_called()
    _assert_caller_owns_transaction(session)
    _assert_no_creation_or_external_work(session)
