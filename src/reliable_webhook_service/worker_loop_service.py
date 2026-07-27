import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.worker_iteration_service import (
    WebhookWorkerIterationResult,
    run_webhook_worker_iteration,
)

__all__ = [
    "WebhookWorkerRunResult",
    "run_webhook_worker",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookWorkerRunResult:
    iterations_started: int
    iterations_completed: int
    total_recovered_count: int
    total_claimed_count: int
    total_completed_count: int
    shutdown_requested: bool
    final_iteration: WebhookWorkerIterationResult | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_positive_number(value: float, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number greater than 0")

    try:
        is_finite = math.isfinite(value)
    except OverflowError:
        is_finite = False

    if not is_finite or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than 0")


def _validate_limit(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")


def _normalize_iteration_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("iteration_now must return a timezone-aware datetime")

    return value.astimezone(UTC)


def _result(
    *,
    iterations_started: int,
    iterations_completed: int,
    total_recovered_count: int,
    total_claimed_count: int,
    total_completed_count: int,
    final_iteration: WebhookWorkerIterationResult | None,
) -> WebhookWorkerRunResult:
    logger.info(
        "Webhook worker stopped gracefully after %d completed iterations",
        iterations_completed,
    )
    return WebhookWorkerRunResult(
        iterations_started=iterations_started,
        iterations_completed=iterations_completed,
        total_recovered_count=total_recovered_count,
        total_claimed_count=total_claimed_count,
        total_completed_count=total_completed_count,
        shutdown_requested=True,
        final_iteration=final_iteration,
    )


def run_webhook_worker(
    *,
    session_factory: Callable[[], Session],
    http_client: WebhookHttpClient,
    poll_interval_seconds: float,
    stale_processing_timeout_seconds: float,
    recovery_limit: int,
    processing_limit: int,
    timeout_seconds: float,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    stop_requested: Callable[[], bool],
    wait: Callable[[float], bool],
    iteration_now: Callable[[], datetime] = _utc_now,
    utc_now: Callable[[], datetime] = _utc_now,
    decision_now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
) -> WebhookWorkerRunResult:
    _validate_positive_number(poll_interval_seconds, name="poll_interval_seconds")
    _validate_positive_number(
        stale_processing_timeout_seconds,
        name="stale_processing_timeout_seconds",
    )
    _validate_limit(recovery_limit, name="recovery_limit")
    _validate_limit(processing_limit, name="processing_limit")
    _validate_positive_number(timeout_seconds, name="timeout_seconds")

    iterations_started = 0
    iterations_completed = 0
    total_recovered_count = 0
    total_claimed_count = 0
    total_completed_count = 0
    final_iteration: WebhookWorkerIterationResult | None = None

    logger.info("Webhook worker loop starting")

    while True:
        if stop_requested():
            logger.info("Webhook worker shutdown requested before iteration")
            return _result(
                iterations_started=iterations_started,
                iterations_completed=iterations_completed,
                total_recovered_count=total_recovered_count,
                total_claimed_count=total_claimed_count,
                total_completed_count=total_completed_count,
                final_iteration=final_iteration,
            )

        normalized_iteration_at = _normalize_iteration_at(iteration_now())
        stale_before = normalized_iteration_at - timedelta(seconds=stale_processing_timeout_seconds)
        iterations_started += 1
        logger.info("Webhook worker iteration %d starting", iterations_started)

        try:
            iteration = run_webhook_worker_iteration(
                session_factory=session_factory,
                http_client=http_client,
                iteration_at=normalized_iteration_at,
                stale_before=stale_before,
                recovery_limit=recovery_limit,
                processing_limit=processing_limit,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
        except Exception as error:
            logger.critical(
                "Webhook worker iteration fatal failure: %s",
                type(error).__name__,
            )
            raise

        iterations_completed += 1
        total_recovered_count += iteration.recovered_count
        total_claimed_count += iteration.claimed_count
        total_completed_count += iteration.completed_count
        final_iteration = iteration
        logger.info(
            ("Webhook worker iteration %d completed: recovered=%d claimed=%d completed=%d"),
            iterations_completed,
            iteration.recovered_count,
            iteration.claimed_count,
            iteration.completed_count,
        )

        if stop_requested():
            logger.info("Webhook worker shutdown requested after iteration")
            return _result(
                iterations_started=iterations_started,
                iterations_completed=iterations_completed,
                total_recovered_count=total_recovered_count,
                total_claimed_count=total_claimed_count,
                total_completed_count=total_completed_count,
                final_iteration=final_iteration,
            )

        if wait(poll_interval_seconds):
            logger.info("Webhook worker shutdown requested during wait")
            return _result(
                iterations_started=iterations_started,
                iterations_completed=iterations_completed,
                total_recovered_count=total_recovered_count,
                total_claimed_count=total_claimed_count,
                total_completed_count=total_completed_count,
                final_iteration=final_iteration,
            )
