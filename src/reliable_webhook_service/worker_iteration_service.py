import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_job_recovery_service import (
    WebhookDeliveryJobRecoveryResult,
    recover_stale_webhook_delivery_jobs,
)
from reliable_webhook_service.delivery_processing_service import (
    WebhookDeliveryProcessingCycleResult,
    run_webhook_delivery_processing_cycle,
)

__all__ = [
    "WebhookWorkerIterationResult",
    "run_webhook_worker_iteration",
]


@dataclass(frozen=True, slots=True)
class WebhookWorkerIterationResult:
    recovery: WebhookDeliveryJobRecoveryResult
    processing: WebhookDeliveryProcessingCycleResult

    @property
    def recovered_count(self) -> int:
        return self.recovery.recovered_count

    @property
    def claimed_count(self) -> int:
        return self.processing.claimed_count

    @property
    def completed_count(self) -> int:
        return self.processing.completed_count


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_limit(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be an integer greater than or equal to 1")


def _validate_timeout_seconds(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than 0")


def _normalize_datetime(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")

    return value.astimezone(UTC)


def run_webhook_worker_iteration(
    *,
    session_factory: Callable[[], Session],
    http_client: WebhookHttpClient,
    iteration_at: datetime,
    stale_before: datetime,
    recovery_limit: int,
    processing_limit: int,
    timeout_seconds: float,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    utc_now: Callable[[], datetime] = _utc_now,
    decision_now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
) -> WebhookWorkerIterationResult:
    _validate_limit(recovery_limit, name="recovery_limit")
    _validate_limit(processing_limit, name="processing_limit")
    _validate_timeout_seconds(timeout_seconds)
    normalized_iteration_at = _normalize_datetime(iteration_at, name="iteration_at")
    normalized_stale_before = _normalize_datetime(stale_before, name="stale_before")
    if normalized_iteration_at < normalized_stale_before:
        raise ValueError("iteration_at must be greater than or equal to stale_before")

    recovery_session = session_factory()
    try:
        recovery = recover_stale_webhook_delivery_jobs(
            recovery_session,
            stale_before=normalized_stale_before,
            recovered_at=normalized_iteration_at,
            limit=recovery_limit,
        )
        recovery_session.commit()
    except Exception:
        recovery_session.rollback()
        raise
    finally:
        recovery_session.close()

    processing = run_webhook_delivery_processing_cycle(
        session_factory=session_factory,
        http_client=http_client,
        claimed_at=normalized_iteration_at,
        limit=processing_limit,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
        utc_now=utc_now,
        decision_now=decision_now,
        monotonic_ns=monotonic_ns,
    )

    return WebhookWorkerIterationResult(
        recovery=recovery,
        processing=processing,
    )
