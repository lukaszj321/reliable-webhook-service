import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

RetryStatus = Literal["pending", "succeeded", "dead_letter"]

__all__ = [
    "RetryDecision",
    "RetryStatus",
    "calculate_retry_delay_seconds",
    "decide_webhook_retry",
]


@dataclass(frozen=True, slots=True)
class RetryDecision:
    status: RetryStatus
    next_attempt_at: datetime | None


def _validate_positive_integer(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")


def _validate_delay_seconds(value: float, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number greater than 0")

    try:
        normalized_value = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} must be a finite number greater than 0") from error

    if not math.isfinite(normalized_value) or normalized_value <= 0:
        raise ValueError(f"{name} must be a finite number greater than 0")

    return normalized_value


def _validate_delays(
    *,
    base_delay_seconds: float,
    max_delay_seconds: float,
) -> tuple[float, float]:
    validated_base = _validate_delay_seconds(
        base_delay_seconds,
        name="base_delay_seconds",
    )
    validated_max = _validate_delay_seconds(
        max_delay_seconds,
        name="max_delay_seconds",
    )
    if validated_base > validated_max:
        raise ValueError("base_delay_seconds must be less than or equal to max_delay_seconds")

    return validated_base, validated_max


def calculate_retry_delay_seconds(
    *,
    attempt_number: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
) -> float:
    _validate_positive_integer(attempt_number, name="attempt_number")
    validated_base, validated_max = _validate_delays(
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )

    try:
        delay_seconds = math.ldexp(validated_base, attempt_number - 1)
    except OverflowError:
        return validated_max

    return min(delay_seconds, validated_max)


def decide_webhook_retry(
    *,
    outcome: str,
    attempt_number: int,
    decision_at: datetime,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
) -> RetryDecision:
    if outcome not in {"succeeded", "failed"}:
        raise ValueError("outcome must be either 'succeeded' or 'failed'")

    _validate_positive_integer(attempt_number, name="attempt_number")
    _validate_positive_integer(max_attempts, name="max_attempts")

    if (
        not isinstance(decision_at, datetime)
        or decision_at.tzinfo is None
        or decision_at.utcoffset() is None
    ):
        raise ValueError("decision_at must be a timezone-aware datetime")

    _validate_delays(
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )

    if outcome == "succeeded":
        return RetryDecision(status="succeeded", next_attempt_at=None)

    if attempt_number >= max_attempts:
        return RetryDecision(status="dead_letter", next_attempt_at=None)

    delay_seconds = calculate_retry_delay_seconds(
        attempt_number=attempt_number,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    try:
        next_attempt_at = decision_at.astimezone(UTC) + timedelta(seconds=delay_seconds)
    except OverflowError as error:
        raise ValueError("retry delay exceeds the supported datetime range") from error

    return RetryDecision(status="pending", next_attempt_at=next_attempt_at)
