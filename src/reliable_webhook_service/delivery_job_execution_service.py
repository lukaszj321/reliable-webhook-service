import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_http import WebhookHttpClient
from reliable_webhook_service.delivery_service import execute_webhook_delivery
from reliable_webhook_service.models import WebhookDeliveryAttempt, WebhookDeliveryJob
from reliable_webhook_service.retry_policy import RetryDecision, decide_webhook_retry

__all__ = [
    "WebhookDeliveryJobExecutionResult",
    "WebhookDeliveryJobNotFoundError",
    "WebhookDeliveryJobNotProcessingError",
    "execute_webhook_delivery_job",
]


@dataclass(frozen=True, slots=True)
class WebhookDeliveryJobExecutionResult:
    job: WebhookDeliveryJob
    attempt: WebhookDeliveryAttempt


class WebhookDeliveryJobNotFoundError(RuntimeError):
    pass


class WebhookDeliveryJobNotProcessingError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def execute_webhook_delivery_job(
    session: Session,
    *,
    job_id: uuid.UUID,
    http_client: WebhookHttpClient,
    timeout_seconds: float,
    max_attempts: int,
    base_delay_seconds: float,
    max_delay_seconds: float,
    utc_now: Callable[[], datetime] = _utc_now,
    decision_now: Callable[[], datetime] = _utc_now,
    monotonic_ns: Callable[[], int] = time.perf_counter_ns,
) -> WebhookDeliveryJobExecutionResult:
    job = session.get(WebhookDeliveryJob, job_id)
    if job is None:
        raise WebhookDeliveryJobNotFoundError("Webhook delivery job not found")

    if job.status != "processing":
        raise WebhookDeliveryJobNotProcessingError("Webhook delivery job is not processing")

    cycle_attempt_number = job.attempt_count + 1
    attempt = execute_webhook_delivery(
        session,
        event_id=job.event_id,
        http_client=http_client,
        timeout_seconds=timeout_seconds,
        utc_now=utc_now,
        monotonic_ns=monotonic_ns,
    )
    decision_at = decision_now()
    decision: RetryDecision = decide_webhook_retry(
        outcome=attempt.outcome,
        attempt_number=cycle_attempt_number,
        decision_at=decision_at,
        max_attempts=max_attempts,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
    )
    normalized_decision_at = decision_at.astimezone(UTC)

    job.attempt_count = cycle_attempt_number
    job.status = decision.status
    job.next_attempt_at = decision.next_attempt_at
    job.updated_at = normalized_decision_at
    session.flush()

    return WebhookDeliveryJobExecutionResult(
        job=job,
        attempt=attempt,
    )
