from dataclasses import FrozenInstanceError, fields
from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service.operations_service import (
    DatabaseReadinessResult,
    WebhookDeliveryJobOperationalCounts,
    WebhookOperationalSummary,
    check_database_readiness,
    get_webhook_operational_summary,
)

GENERATED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _aggregate_row(
    *,
    pending: int = 0,
    processing: int = 0,
    succeeded: int = 0,
    dead_letter: int = 0,
    due_pending: int = 0,
    stale_processing: int = 0,
    oldest_due_pending_at: datetime | None = None,
    oldest_processing_updated_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        pending=pending,
        processing=processing,
        succeeded=succeeded,
        dead_letter=dead_letter,
        due_pending=due_pending,
        stale_processing=stale_processing,
        oldest_due_pending_at=oldest_due_pending_at,
        oldest_processing_updated_at=oldest_processing_updated_at,
    )


def _summary_session(row: SimpleNamespace | None = None) -> Mock:
    session = Mock(spec=Session)
    session.execute.return_value.one.return_value = row or _aggregate_row()
    return session


def _assert_no_session_ownership(session: Mock) -> None:
    for method in (
        "add",
        "add_all",
        "delete",
        "flush",
        "commit",
        "rollback",
        "refresh",
        "close",
        "begin",
        "begin_nested",
    ):
        getattr(session, method).assert_not_called()


def test_readiness_executes_one_minimal_query_and_returns_frozen_result() -> None:
    session = Mock(spec=Session)
    session.scalar.return_value = 1

    result = check_database_readiness(session)

    assert result == DatabaseReadinessResult(database="ok")
    assert [field.name for field in fields(result)] == ["database"]
    assert result.__slots__ == ("database",)
    with pytest.raises(FrozenInstanceError):
        result.database = "ok"  # type: ignore[misc]
    session.scalar.assert_called_once()
    assert str(session.scalar.call_args.args[0]) == "SELECT 1"
    _assert_no_session_ownership(session)


def test_readiness_rejects_unexpected_result() -> None:
    session = Mock(spec=Session)
    session.scalar.return_value = 0

    with pytest.raises(
        RuntimeError,
        match="^Database readiness query returned an unexpected result$",
    ):
        check_database_readiness(session)

    session.scalar.assert_called_once()
    _assert_no_session_ownership(session)


def test_readiness_propagates_database_error_unchanged() -> None:
    session = Mock(spec=Session)
    error = SQLAlchemyError("database unavailable")
    session.scalar.side_effect = error

    with pytest.raises(SQLAlchemyError) as raised:
        check_database_readiness(session)

    assert raised.value is error
    session.scalar.assert_called_once()
    _assert_no_session_ownership(session)


@pytest.mark.parametrize(
    "generated_at",
    [
        datetime(2026, 8, 2, 12, 0),
        None,
        "2026-08-02T12:00:00Z",
        date(2026, 8, 2),
        True,
        object(),
    ],
)
def test_invalid_generated_at_is_rejected_before_sql(generated_at: object) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        ValueError,
        match="^generated_at must be a timezone-aware datetime$",
    ):
        get_webhook_operational_summary(
            session,
            generated_at=cast(Any, generated_at),
            stale_processing_timeout_seconds=300.0,
        )

    session.execute.assert_not_called()


@pytest.mark.parametrize("timeout", [300, 300.5, 0.000001])
def test_valid_timeout_values_are_accepted(timeout: float) -> None:
    session = _summary_session()

    result = get_webhook_operational_summary(
        session,
        generated_at=GENERATED_AT,
        stale_processing_timeout_seconds=timeout,
    )

    assert result.stale_processing_before == GENERATED_AT - timedelta(seconds=timeout)
    session.execute.assert_called_once()


@pytest.mark.parametrize(
    "timeout",
    [
        0,
        -1,
        float("inf"),
        float("-inf"),
        float("nan"),
        True,
        "300",
        None,
        10**1000,
        1e300,
    ],
)
def test_invalid_timeout_is_rejected_before_sql(timeout: object) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        ValueError,
        match=("^stale_processing_timeout_seconds must be a finite number greater than 0$"),
    ):
        get_webhook_operational_summary(
            session,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=cast(Any, timeout),
        )

    session.execute.assert_not_called()


def test_validation_order_checks_generated_at_before_timeout() -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        ValueError,
        match="^generated_at must be a timezone-aware datetime$",
    ):
        get_webhook_operational_summary(
            session,
            generated_at=cast(Any, None),
            stale_processing_timeout_seconds=cast(Any, None),
        )

    session.execute.assert_not_called()


def test_summary_maps_one_aggregate_row_and_normalizes_timestamps() -> None:
    plus_two = timezone(timedelta(hours=2))
    generated_at = datetime(2026, 8, 2, 14, 0, tzinfo=plus_two)
    oldest_due = datetime(2026, 8, 2, 12, 30, tzinfo=plus_two)
    oldest_processing = datetime(2026, 8, 2, 11, 0, tzinfo=plus_two)
    session = _summary_session(
        _aggregate_row(
            pending=3,
            processing=4,
            succeeded=5,
            dead_letter=6,
            due_pending=2,
            stale_processing=1,
            oldest_due_pending_at=oldest_due,
            oldest_processing_updated_at=oldest_processing,
        )
    )

    result = get_webhook_operational_summary(
        session,
        generated_at=generated_at,
        stale_processing_timeout_seconds=300.0,
    )

    assert result == WebhookOperationalSummary(
        generated_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        delivery_jobs=WebhookDeliveryJobOperationalCounts(
            pending=3,
            processing=4,
            succeeded=5,
            dead_letter=6,
            due_pending=2,
            stale_processing=1,
        ),
        oldest_due_pending_at=datetime(2026, 8, 2, 10, 30, tzinfo=UTC),
        oldest_processing_updated_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        stale_processing_before=datetime(2026, 8, 2, 11, 55, tzinfo=UTC),
    )
    assert result.__slots__ == (
        "generated_at",
        "delivery_jobs",
        "oldest_due_pending_at",
        "oldest_processing_updated_at",
        "stale_processing_before",
    )
    with pytest.raises(FrozenInstanceError):
        result.generated_at = GENERATED_AT  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.delivery_jobs.pending = 0  # type: ignore[misc]
    assert not isinstance(result.delivery_jobs, tuple)
    session.execute.assert_called_once()
    _assert_no_session_ownership(session)


def test_empty_summary_maps_zero_counts_and_none_minima() -> None:
    session = _summary_session()

    result = get_webhook_operational_summary(
        session,
        generated_at=GENERATED_AT,
        stale_processing_timeout_seconds=300.0,
    )

    assert result.delivery_jobs == WebhookDeliveryJobOperationalCounts(
        pending=0,
        processing=0,
        succeeded=0,
        dead_letter=0,
        due_pending=0,
        stale_processing=0,
    )
    assert result.oldest_due_pending_at is None
    assert result.oldest_processing_updated_at is None


@pytest.mark.parametrize("invalid_count", [-1, True, 1.5, "1", None])
def test_operational_counts_reject_invalid_values(invalid_count: object) -> None:
    with pytest.raises(
        ValueError,
        match="^Operational counts must be non-negative integers$",
    ):
        WebhookDeliveryJobOperationalCounts(
            pending=cast(Any, invalid_count),
            processing=0,
            succeeded=0,
            dead_letter=0,
            due_pending=0,
            stale_processing=0,
        )


def test_summary_statement_contains_all_aggregates_and_boundaries() -> None:
    session = _summary_session()

    get_webhook_operational_summary(
        session,
        generated_at=GENERATED_AT,
        stale_processing_timeout_seconds=300.0,
    )

    statement = session.execute.call_args.args[0]
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    for job_status in ("pending", "processing", "succeeded", "dead_letter"):
        assert f"status = '{job_status}'" in compiled
    assert "next_attempt_at <=" in compiled
    assert "updated_at <" in compiled
    assert "min(webhook_delivery_jobs.next_attempt_at)" in compiled
    assert "min(webhook_delivery_jobs.updated_at)" in compiled
    assert "FROM webhook_delivery_jobs" in compiled
    assert "FOR UPDATE" not in compiled
    assert "OFFSET" not in compiled
    session.execute.assert_called_once()


def test_summary_propagates_database_error_without_session_ownership() -> None:
    session = Mock(spec=Session)
    error = SQLAlchemyError("aggregate failed")
    session.execute.side_effect = error

    with pytest.raises(SQLAlchemyError) as raised:
        get_webhook_operational_summary(
            session,
            generated_at=GENERATED_AT,
            stale_processing_timeout_seconds=300.0,
        )

    assert raised.value is error
    session.execute.assert_called_once()
    _assert_no_session_ownership(session)
