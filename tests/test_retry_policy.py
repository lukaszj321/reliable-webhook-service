from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest

from reliable_webhook_service.retry_policy import (
    RetryDecision,
    calculate_retry_delay_seconds,
    decide_webhook_retry,
)


def test_retry_decision_is_frozen_slotted_dataclass_with_exact_fields() -> None:
    decision = RetryDecision(status="succeeded", next_attempt_at=None)

    assert is_dataclass(RetryDecision)
    assert [field.name for field in fields(RetryDecision)] == [
        "status",
        "next_attempt_at",
    ]
    assert not hasattr(decision, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(decision, "status", "pending")


@pytest.mark.parametrize(
    ("attempt_number", "expected_delay"),
    [
        (1, 5.0),
        (2, 10.0),
        (3, 20.0),
        (4, 40.0),
    ],
)
def test_calculate_retry_delay_uses_exponential_backoff(
    attempt_number: int,
    expected_delay: float,
) -> None:
    delay = calculate_retry_delay_seconds(
        attempt_number=attempt_number,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert delay == expected_delay
    assert isinstance(delay, float)


def test_calculate_retry_delay_applies_cap() -> None:
    assert (
        calculate_retry_delay_seconds(
            attempt_number=7,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )
        == 300.0
    )


def test_calculate_retry_delay_returns_cap_when_base_equals_maximum() -> None:
    assert (
        calculate_retry_delay_seconds(
            attempt_number=1,
            base_delay_seconds=300.0,
            max_delay_seconds=300.0,
        )
        == 300.0
    )


def test_calculate_retry_delay_handles_very_large_attempt_number() -> None:
    delay = calculate_retry_delay_seconds(
        attempt_number=10**9,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert delay == 300.0
    assert isinstance(delay, float)


def test_calculate_retry_delay_is_deterministic() -> None:
    arguments = {
        "attempt_number": 4,
        "base_delay_seconds": 5.0,
        "max_delay_seconds": 300.0,
    }

    first_result = calculate_retry_delay_seconds(**arguments)
    second_result = calculate_retry_delay_seconds(**arguments)

    assert first_result == second_result


@pytest.mark.parametrize("attempt_number", [0, -1, True, False])
def test_calculate_retry_delay_rejects_invalid_attempt_number(
    attempt_number: int,
) -> None:
    with pytest.raises(ValueError, match="attempt_number"):
        calculate_retry_delay_seconds(
            attempt_number=attempt_number,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )


@pytest.mark.parametrize(
    "base_delay_seconds",
    [0.0, -1.0, float("nan"), float("inf"), -float("inf"), True, False],
)
def test_calculate_retry_delay_rejects_invalid_base_delay(
    base_delay_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="base_delay_seconds"):
        calculate_retry_delay_seconds(
            attempt_number=1,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=300.0,
        )


@pytest.mark.parametrize(
    "max_delay_seconds",
    [0.0, -1.0, float("nan"), float("inf"), -float("inf"), True, False],
)
def test_calculate_retry_delay_rejects_invalid_max_delay(
    max_delay_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="max_delay_seconds"):
        calculate_retry_delay_seconds(
            attempt_number=1,
            base_delay_seconds=5.0,
            max_delay_seconds=max_delay_seconds,
        )


def test_calculate_retry_delay_rejects_base_above_maximum() -> None:
    with pytest.raises(ValueError, match="base_delay_seconds"):
        calculate_retry_delay_seconds(
            attempt_number=1,
            base_delay_seconds=10.0,
            max_delay_seconds=5.0,
        )


def test_decide_webhook_retry_returns_succeeded() -> None:
    decision = decide_webhook_retry(
        outcome="succeeded",
        attempt_number=3,
        decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        max_attempts=5,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert decision == RetryDecision(status="succeeded", next_attempt_at=None)


def test_decide_webhook_retry_returns_pending_below_attempt_limit() -> None:
    decision = decide_webhook_retry(
        outcome="failed",
        attempt_number=2,
        decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        max_attempts=5,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert decision.status == "pending"
    assert decision.next_attempt_at == datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    assert decision.next_attempt_at is not None
    assert decision.next_attempt_at.tzinfo is UTC
    assert decision.next_attempt_at.utcoffset() == timedelta(0)


def test_decide_webhook_retry_returns_dead_letter_at_attempt_limit() -> None:
    decision = decide_webhook_retry(
        outcome="failed",
        attempt_number=5,
        decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        max_attempts=5,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert decision == RetryDecision(status="dead_letter", next_attempt_at=None)


def test_decide_webhook_retry_returns_dead_letter_above_attempt_limit() -> None:
    decision = decide_webhook_retry(
        outcome="failed",
        attempt_number=6,
        decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        max_attempts=5,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert decision == RetryDecision(status="dead_letter", next_attempt_at=None)


def test_decide_webhook_retry_normalizes_non_utc_datetime() -> None:
    decision = decide_webhook_retry(
        outcome="failed",
        attempt_number=2,
        decision_at=datetime(
            2026,
            1,
            1,
            14,
            0,
            tzinfo=timezone(timedelta(hours=2)),
        ),
        max_attempts=5,
        base_delay_seconds=5.0,
        max_delay_seconds=300.0,
    )

    assert decision.next_attempt_at == datetime(2026, 1, 1, 12, 0, 10, tzinfo=UTC)
    assert decision.next_attempt_at is not None
    assert decision.next_attempt_at.tzinfo is UTC


def test_decide_webhook_retry_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware datetime"):
        decide_webhook_retry(
            outcome="failed",
            attempt_number=1,
            decision_at=datetime(2026, 1, 1, 12, 0),
            max_attempts=5,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )


@pytest.mark.parametrize("outcome", ["unknown", "processing", ""])
def test_decide_webhook_retry_rejects_unsupported_outcome(outcome: str) -> None:
    with pytest.raises(ValueError, match="outcome"):
        decide_webhook_retry(
            outcome=outcome,
            attempt_number=1,
            decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            max_attempts=5,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )


@pytest.mark.parametrize("attempt_number", [0, -1, True, False])
def test_decide_webhook_retry_rejects_invalid_attempt_number(
    attempt_number: int,
) -> None:
    with pytest.raises(ValueError, match="attempt_number"):
        decide_webhook_retry(
            outcome="failed",
            attempt_number=attempt_number,
            decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            max_attempts=5,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )


@pytest.mark.parametrize("max_attempts", [0, -1, True, False])
def test_decide_webhook_retry_rejects_invalid_max_attempts(
    max_attempts: int,
) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        decide_webhook_retry(
            outcome="succeeded",
            attempt_number=1,
            decision_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            max_attempts=max_attempts,
            base_delay_seconds=5.0,
            max_delay_seconds=300.0,
        )


def test_decide_webhook_retry_rejects_delay_outside_datetime_range() -> None:
    with pytest.raises(ValueError, match="datetime range"):
        decide_webhook_retry(
            outcome="failed",
            attempt_number=1,
            decision_at=datetime.max.replace(tzinfo=UTC),
            max_attempts=2,
            base_delay_seconds=1.0,
            max_delay_seconds=1.0,
        )


def test_decide_webhook_retry_is_deterministic() -> None:
    arguments = {
        "outcome": "failed",
        "attempt_number": 3,
        "decision_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        "max_attempts": 5,
        "base_delay_seconds": 5.0,
        "max_delay_seconds": 300.0,
    }

    first_decision = decide_webhook_retry(**arguments)
    second_decision = decide_webhook_retry(**arguments)

    assert first_decision == second_decision
