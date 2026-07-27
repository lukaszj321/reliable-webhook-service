import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_job_execution_service import (
    execute_webhook_delivery_job,
)
from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs

__all__ = [
    "WebhookDeliveryProcessingCycleResult",
    "WebhookDeliveryProcessingJobResult",
    "run_webhook_delivery_processing_cycle",
]


@dataclass(frozen=True, slots=True)
class WebhookDeliveryProcessingJobResult:
    job_id: UUID
    attempt_id: UUID
    status: str
    next_attempt_at: datetime | None


@dataclass(frozen=True, slots=True)
class WebhookDeliveryProcessingCycleResult:
    claimed_job_ids: tuple[UUID, ...]
    completed_jobs: tuple[WebhookDeliveryProcessingJobResult, ...]

    @property
    def claimed_count(self) -> int:
        return len(self.claimed_job_ids)

    @property
    def completed_count(self) -> int:
        return len(self.completed_jobs)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be an integer greater than or equal to 1")


def _validate_claimed_at(claimed_at: datetime) -> None:
    if (
        not isinstance(claimed_at, datetime)
        or claimed_at.tzinfo is None
        or claimed_at.utcoffset() is None
    ):
        raise ValueError("claimed_at must be a timezone-aware datetime")


def _validate_timeout_seconds(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite number greater than 0")


def run_webhook_delivery_processing_cycle(
    *,
    session_factory: Callable[[], Session],
    http_client: WebhookHttpClient,
    claimed_at: datetime,
    limit: int,
    timeout_seconds: float,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    utc_now: Callable[[], datetime] = _utc_now,
    decision_now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
) -> WebhookDeliveryProcessingCycleResult:
    _validate_limit(limit)
    _validate_claimed_at(claimed_at)
    _validate_timeout_seconds(timeout_seconds)

    claim_session = session_factory()
    try:
        claimed_jobs = claim_due_webhook_delivery_jobs(
            claim_session,
            claimed_at=claimed_at,
            limit=limit,
        )
        claimed_job_ids = tuple(job.id for job in claimed_jobs)
        del claimed_jobs
        claim_session.commit()
    except Exception:
        claim_session.rollback()
        raise
    finally:
        claim_session.close()

    completed_jobs: list[WebhookDeliveryProcessingJobResult] = []
    for job_id in claimed_job_ids:
        completion_session = session_factory()
        try:
            result = execute_webhook_delivery_job(
                completion_session,
                job_id=job_id,
                http_client=http_client,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                base_delay_seconds=base_delay_seconds,
                max_delay_seconds=max_delay_seconds,
                utc_now=utc_now,
                decision_now=decision_now,
                monotonic_ns=monotonic_ns,
            )
            snapshot = WebhookDeliveryProcessingJobResult(
                job_id=result.job.id,
                attempt_id=result.attempt.id,
                status=result.job.status,
                next_attempt_at=result.job.next_attempt_at,
            )
            completion_session.commit()
        except Exception:
            completion_session.rollback()
            raise
        else:
            completed_jobs.append(snapshot)
        finally:
            completion_session.close()

    return WebhookDeliveryProcessingCycleResult(
        claimed_job_ids=claimed_job_ids,
        completed_jobs=tuple(completed_jobs),
    )
