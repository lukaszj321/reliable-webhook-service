import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import WebhookDeliveryJob

__all__ = [
    "DatabaseReadinessResult",
    "WebhookDeliveryJobOperationalCounts",
    "WebhookOperationalSummary",
    "check_database_readiness",
    "get_webhook_operational_summary",
]


@dataclass(frozen=True, slots=True)
class DatabaseReadinessResult:
    database: Literal["ok"]


@dataclass(frozen=True, slots=True)
class WebhookDeliveryJobOperationalCounts:
    pending: int
    processing: int
    succeeded: int
    dead_letter: int
    due_pending: int
    stale_processing: int

    def __post_init__(self) -> None:
        values = (
            self.pending,
            self.processing,
            self.succeeded,
            self.dead_letter,
            self.due_pending,
            self.stale_processing,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("Operational counts must be non-negative integers")


@dataclass(frozen=True, slots=True)
class WebhookOperationalSummary:
    generated_at: datetime
    delivery_jobs: WebhookDeliveryJobOperationalCounts
    oldest_due_pending_at: datetime | None
    oldest_processing_updated_at: datetime | None
    stale_processing_before: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "generated_at",
            _normalize_result_timestamp(self.generated_at, name="generated_at"),
        )
        object.__setattr__(
            self,
            "oldest_due_pending_at",
            _normalize_optional_result_timestamp(
                self.oldest_due_pending_at,
                name="oldest_due_pending_at",
            ),
        )
        object.__setattr__(
            self,
            "oldest_processing_updated_at",
            _normalize_optional_result_timestamp(
                self.oldest_processing_updated_at,
                name="oldest_processing_updated_at",
            ),
        )
        object.__setattr__(
            self,
            "stale_processing_before",
            _normalize_result_timestamp(
                self.stale_processing_before,
                name="stale_processing_before",
            ),
        )


def _normalize_generated_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _stale_processing_delta(value: float) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stale_processing_timeout_seconds must be a finite number greater than 0")
    try:
        is_finite = math.isfinite(value)
    except OverflowError:
        is_finite = False
    if not is_finite or value <= 0:
        raise ValueError("stale_processing_timeout_seconds must be a finite number greater than 0")
    try:
        return timedelta(seconds=value)
    except OverflowError:
        raise ValueError(
            "stale_processing_timeout_seconds must be a finite number greater than 0"
        ) from None


def _normalize_result_timestamp(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _normalize_optional_result_timestamp(
    value: datetime | None,
    *,
    name: str,
) -> datetime | None:
    if value is None:
        return None
    return _normalize_result_timestamp(value, name=name)


def check_database_readiness(
    session: Session,
) -> DatabaseReadinessResult:
    result = session.scalar(select(1))
    if result != 1:
        raise RuntimeError("Database readiness query returned an unexpected result")
    return DatabaseReadinessResult(database="ok")


def get_webhook_operational_summary(
    session: Session,
    *,
    generated_at: datetime,
    stale_processing_timeout_seconds: float,
) -> WebhookOperationalSummary:
    normalized_generated_at = _normalize_generated_at(generated_at)
    stale_processing_before = normalized_generated_at - _stale_processing_delta(
        stale_processing_timeout_seconds
    )

    pending = WebhookDeliveryJob.status == "pending"
    processing = WebhookDeliveryJob.status == "processing"
    succeeded = WebhookDeliveryJob.status == "succeeded"
    dead_letter = WebhookDeliveryJob.status == "dead_letter"
    due_pending = pending & (WebhookDeliveryJob.next_attempt_at <= normalized_generated_at)
    stale_processing = processing & (WebhookDeliveryJob.updated_at < stale_processing_before)

    statement = select(
        func.count().filter(pending).label("pending"),
        func.count().filter(processing).label("processing"),
        func.count().filter(succeeded).label("succeeded"),
        func.count().filter(dead_letter).label("dead_letter"),
        func.count().filter(due_pending).label("due_pending"),
        func.count().filter(stale_processing).label("stale_processing"),
        func.min(WebhookDeliveryJob.next_attempt_at)
        .filter(due_pending)
        .label("oldest_due_pending_at"),
        func.min(WebhookDeliveryJob.updated_at)
        .filter(processing)
        .label("oldest_processing_updated_at"),
    ).select_from(WebhookDeliveryJob)

    row = session.execute(statement).one()
    counts = WebhookDeliveryJobOperationalCounts(
        pending=int(row.pending),
        processing=int(row.processing),
        succeeded=int(row.succeeded),
        dead_letter=int(row.dead_letter),
        due_pending=int(row.due_pending),
        stale_processing=int(row.stale_processing),
    )
    return WebhookOperationalSummary(
        generated_at=normalized_generated_at,
        delivery_jobs=counts,
        oldest_due_pending_at=_normalize_optional_result_timestamp(
            row.oldest_due_pending_at,
            name="oldest_due_pending_at",
        ),
        oldest_processing_updated_at=_normalize_optional_result_timestamp(
            row.oldest_processing_updated_at,
            name="oldest_processing_updated_at",
        ),
        stale_processing_before=stale_processing_before,
    )
