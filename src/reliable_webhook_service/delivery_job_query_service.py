import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from reliable_webhook_service.models import WebhookDeliveryJob, WebhookEvent

__all__ = [
    "DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT",
    "MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT",
    "WEBHOOK_DELIVERY_JOB_STATUSES",
    "WebhookDeliveryJobCursorValidationError",
    "WebhookDeliveryJobEventNotFoundError",
    "WebhookDeliveryJobLimitValidationError",
    "WebhookDeliveryJobNotFoundError",
    "WebhookDeliveryJobPage",
    "WebhookDeliveryJobSnapshot",
    "WebhookDeliveryJobStatus",
    "WebhookDeliveryJobStatusValidationError",
    "get_webhook_delivery_job",
    "list_webhook_delivery_jobs",
]

type WebhookDeliveryJobStatus = Literal[
    "pending",
    "processing",
    "succeeded",
    "dead_letter",
]

WEBHOOK_DELIVERY_JOB_STATUSES: frozenset[WebhookDeliveryJobStatus] = frozenset(
    {
        "pending",
        "processing",
        "succeeded",
        "dead_letter",
    }
)

DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT = 50
MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT = 100

_CURSOR_VERSION = 1
_CURSOR_FIELDS = {"v", "updated_at", "id", "status"}


@dataclass(frozen=True, slots=True)
class WebhookDeliveryJobSnapshot:
    id: uuid.UUID
    event_id: uuid.UUID
    status: WebhookDeliveryJobStatus
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDeliveryJobPage:
    items: tuple[WebhookDeliveryJobSnapshot, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _CursorPosition:
    updated_at: datetime
    id: uuid.UUID


class WebhookDeliveryJobEventNotFoundError(RuntimeError):
    pass


class WebhookDeliveryJobNotFoundError(RuntimeError):
    pass


class WebhookDeliveryJobStatusValidationError(ValueError):
    pass


class WebhookDeliveryJobLimitValidationError(ValueError):
    pass


class WebhookDeliveryJobCursorValidationError(ValueError):
    pass


def _validate_status(
    status: WebhookDeliveryJobStatus | str | None,
) -> WebhookDeliveryJobStatus | None:
    if status is None:
        return None
    if status == "pending":
        return "pending"
    if status == "processing":
        return "processing"
    if status == "succeeded":
        return "succeeded"
    if status == "dead_letter":
        return "dead_letter"
    raise WebhookDeliveryJobStatusValidationError("Invalid webhook delivery job status")


def _validate_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT:
        raise WebhookDeliveryJobLimitValidationError("Invalid webhook delivery job limit")
    return limit


def _snapshot(job: WebhookDeliveryJob) -> WebhookDeliveryJobSnapshot:
    status = _validate_status(job.status)
    assert status is not None
    return WebhookDeliveryJobSnapshot(
        id=job.id,
        event_id=job.event_id,
        status=status,
        attempt_count=job.attempt_count,
        next_attempt_at=job.next_attempt_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _format_cursor_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _encode_cursor(
    snapshot: WebhookDeliveryJobSnapshot,
    *,
    status: WebhookDeliveryJobStatus | None,
) -> str:
    payload = {
        "id": str(snapshot.id),
        "status": status,
        "updated_at": _format_cursor_timestamp(snapshot.updated_at),
        "v": _CURSOR_VERSION,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(serialized).decode("ascii").rstrip("=")


def _decode_cursor_payload(
    cursor: str,
    *,
    status: WebhookDeliveryJobStatus | None,
) -> _CursorPosition:
    if not isinstance(cursor, str) or not cursor:
        raise ValueError

    padding = "=" * (-len(cursor) % 4)
    decoded = base64.b64decode(
        (cursor + padding).encode("ascii"),
        altchars=b"-_",
        validate=True,
    )
    payload = json.loads(decoded.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != _CURSOR_FIELDS:
        raise ValueError
    if type(payload["v"]) is not int or payload["v"] != _CURSOR_VERSION:
        raise ValueError
    if not isinstance(payload["updated_at"], str) or not isinstance(payload["id"], str):
        raise ValueError

    cursor_status = payload["status"]
    if cursor_status is not None and cursor_status not in WEBHOOK_DELIVERY_JOB_STATUSES:
        raise ValueError
    if cursor_status != status:
        raise ValueError

    timestamp_text = payload["updated_at"]
    if timestamp_text.endswith("Z"):
        timestamp_text = f"{timestamp_text[:-1]}+00:00"
    updated_at = datetime.fromisoformat(timestamp_text)
    if updated_at.tzinfo is None or updated_at.utcoffset() is None:
        raise ValueError

    return _CursorPosition(
        updated_at=updated_at.astimezone(UTC),
        id=uuid.UUID(payload["id"]),
    )


def _decode_cursor(
    cursor: str,
    *,
    status: WebhookDeliveryJobStatus | None,
) -> _CursorPosition:
    try:
        return _decode_cursor_payload(cursor, status=status)
    except (
        UnicodeEncodeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        raise WebhookDeliveryJobCursorValidationError(
            "Invalid webhook delivery job cursor"
        ) from None


def get_webhook_delivery_job(
    session: Session,
    *,
    event_id: uuid.UUID,
) -> WebhookDeliveryJobSnapshot:
    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise WebhookDeliveryJobEventNotFoundError("Webhook event not found")

    statement = select(WebhookDeliveryJob).where(WebhookDeliveryJob.event_id == event_id)
    job = session.scalar(statement)
    if job is None:
        raise WebhookDeliveryJobNotFoundError("Webhook delivery job not found")
    return _snapshot(job)


def list_webhook_delivery_jobs(
    session: Session,
    *,
    status: WebhookDeliveryJobStatus | str | None,
    limit: int,
    cursor: str | None,
) -> WebhookDeliveryJobPage:
    validated_status = _validate_status(status)
    validated_limit = _validate_limit(limit)
    cursor_position = None if cursor is None else _decode_cursor(cursor, status=validated_status)

    statement = select(WebhookDeliveryJob)
    if validated_status is not None:
        statement = statement.where(WebhookDeliveryJob.status == validated_status)
    if cursor_position is not None:
        statement = statement.where(
            or_(
                WebhookDeliveryJob.updated_at < cursor_position.updated_at,
                and_(
                    WebhookDeliveryJob.updated_at == cursor_position.updated_at,
                    WebhookDeliveryJob.id < cursor_position.id,
                ),
            )
        )
    statement = statement.order_by(
        WebhookDeliveryJob.updated_at.desc(),
        WebhookDeliveryJob.id.desc(),
    ).limit(validated_limit + 1)

    jobs = tuple(session.scalars(statement).all())
    has_more = len(jobs) > validated_limit
    items = tuple(_snapshot(job) for job in jobs[:validated_limit])
    next_cursor = _encode_cursor(items[-1], status=validated_status) if has_more and items else None
    return WebhookDeliveryJobPage(items=items, next_cursor=next_cursor)
