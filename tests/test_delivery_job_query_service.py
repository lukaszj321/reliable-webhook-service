import base64
import json
import uuid
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import Mock, call

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_job_query_service import (
    DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    WEBHOOK_DELIVERY_JOB_STATUSES,
    WebhookDeliveryJobCursorValidationError,
    WebhookDeliveryJobEventNotFoundError,
    WebhookDeliveryJobLimitValidationError,
    WebhookDeliveryJobNotFoundError,
    WebhookDeliveryJobPage,
    WebhookDeliveryJobSnapshot,
    WebhookDeliveryJobStatus,
    WebhookDeliveryJobStatusValidationError,
    get_webhook_delivery_job,
    list_webhook_delivery_jobs,
)
from reliable_webhook_service.models import WebhookDeliveryJob, WebhookEvent

BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _job(
    *,
    identifier: int = 1,
    status: WebhookDeliveryJobStatus = "pending",
    updated_at: datetime = BASE_TIME,
) -> WebhookDeliveryJob:
    return WebhookDeliveryJob(
        id=uuid.UUID(int=identifier),
        event_id=uuid.UUID(int=identifier + 100),
        status=status,
        attempt_count=identifier,
        next_attempt_at=updated_at if status in {"pending", "processing"} else None,
        created_at=updated_at - timedelta(hours=1),
        updated_at=updated_at,
    )


def _session_with_jobs(*jobs: WebhookDeliveryJob) -> Mock:
    session = Mock(spec=Session)
    session.scalars.return_value.all.return_value = list(jobs)
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


def _valid_cursor(
    *,
    status: WebhookDeliveryJobStatus | None = None,
) -> str:
    session = _session_with_jobs(_job(identifier=2), _job(identifier=1))
    page = list_webhook_delivery_jobs(
        session,
        status=status,
        limit=1,
        cursor=None,
    )
    assert page.next_cursor is not None
    return page.next_cursor


def _cursor_from_payload(payload: Any) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _cursor_payload(cursor: str) -> dict[str, object]:
    decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    value = json.loads(decoded.decode("utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.mark.parametrize("job_status", sorted(WEBHOOK_DELIVERY_JOB_STATUSES))
def test_lookup_returns_exact_frozen_snapshot_for_each_status(
    job_status: WebhookDeliveryJobStatus,
) -> None:
    event_id = uuid.uuid4()
    event = WebhookEvent(id=event_id)
    job = _job(status=job_status)
    job.event_id = event_id
    session = Mock(spec=Session)
    session.get.return_value = event
    session.scalar.return_value = job

    result = get_webhook_delivery_job(session, event_id=event_id)

    assert result == WebhookDeliveryJobSnapshot(
        id=job.id,
        event_id=event_id,
        status=job_status,
        attempt_count=job.attempt_count,
        next_attempt_at=job.next_attempt_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
    assert not isinstance(result, WebhookDeliveryJob)
    assert [field.name for field in fields(result)] == [
        "id",
        "event_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "created_at",
        "updated_at",
    ]
    with pytest.raises(FrozenInstanceError):
        result.status = "succeeded"  # type: ignore[misc]
    assert session.mock_calls == [
        call.get(WebhookEvent, event_id),
        call.scalar(cast(Any, session.mock_calls[1].args[0])),
    ]
    assert job.status == job_status
    _assert_no_session_ownership(session)


def test_lookup_rejects_missing_event_before_job_query() -> None:
    session = Mock(spec=Session)
    session.get.return_value = None

    with pytest.raises(
        WebhookDeliveryJobEventNotFoundError,
        match="^Webhook event not found$",
    ):
        get_webhook_delivery_job(session, event_id=uuid.uuid4())

    session.scalar.assert_not_called()
    _assert_no_session_ownership(session)


def test_lookup_rejects_missing_job() -> None:
    session = Mock(spec=Session)
    session.get.return_value = WebhookEvent(id=uuid.uuid4())
    session.scalar.return_value = None

    with pytest.raises(
        WebhookDeliveryJobNotFoundError,
        match="^Webhook delivery job not found$",
    ):
        get_webhook_delivery_job(session, event_id=uuid.uuid4())

    session.scalar.assert_called_once()
    _assert_no_session_ownership(session)


@pytest.mark.parametrize("failing_method", ["get", "scalar"])
def test_lookup_propagates_database_errors(failing_method: str) -> None:
    session = Mock(spec=Session)
    session.get.return_value = WebhookEvent(id=uuid.uuid4())
    error = SQLAlchemyError(f"{failing_method} failed")
    getattr(session, failing_method).side_effect = error

    with pytest.raises(SQLAlchemyError) as raised:
        get_webhook_delivery_job(session, event_id=uuid.uuid4())

    assert raised.value is error
    _assert_no_session_ownership(session)


@pytest.mark.parametrize(
    "job_status",
    [None, "pending", "processing", "succeeded", "dead_letter"],
)
def test_list_accepts_each_public_status_and_none(
    job_status: WebhookDeliveryJobStatus | None,
) -> None:
    session = _session_with_jobs()

    result = list_webhook_delivery_jobs(
        session,
        status=job_status,
        limit=DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
        cursor=None,
    )

    assert result == WebhookDeliveryJobPage(items=(), next_cursor=None)
    session.scalars.assert_called_once()
    _assert_no_session_ownership(session)


@pytest.mark.parametrize("job_status", ["", " ", "PENDING", "unknown"])
def test_invalid_status_is_rejected_before_sql(job_status: str) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobStatusValidationError,
        match="^Invalid webhook delivery job status$",
    ):
        list_webhook_delivery_jobs(session, status=job_status, limit=50, cursor=None)

    assert session.mock_calls == []


@pytest.mark.parametrize(
    "limit",
    [1, DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT, MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT],
)
def test_valid_limits_are_accepted(limit: int) -> None:
    session = _session_with_jobs()

    list_webhook_delivery_jobs(session, status=None, limit=limit, cursor=None)

    statement = session.scalars.call_args.args[0]
    assert statement.compile().params["param_1"] == limit + 1


@pytest.mark.parametrize(
    "limit",
    [0, -1, MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT + 1, True, 1.0, "1", None],
)
def test_invalid_limits_are_rejected_before_sql(limit: object) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobLimitValidationError,
        match="^Invalid webhook delivery job limit$",
    ):
        list_webhook_delivery_jobs(
            session,
            status=None,
            limit=cast(Any, limit),
            cursor=None,
        )

    assert session.mock_calls == []


def test_cursor_round_trip_binds_filter_and_applies_keyset_predicate() -> None:
    cursor = _valid_cursor(status="dead_letter")
    session = _session_with_jobs()

    result = list_webhook_delivery_jobs(
        session,
        status="dead_letter",
        limit=10,
        cursor=cursor,
    )

    assert result == WebhookDeliveryJobPage(items=(), next_cursor=None)
    statement_text = str(session.scalars.call_args.args[0])
    assert "webhook_delivery_jobs.status =" in statement_text
    assert "webhook_delivery_jobs.updated_at <" in statement_text
    assert "webhook_delivery_jobs.updated_at =" in statement_text
    assert "webhook_delivery_jobs.id <" in statement_text
    assert "ORDER BY webhook_delivery_jobs.updated_at DESC, webhook_delivery_jobs.id DESC" in (
        statement_text
    )


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "   ",
        "*",
        base64.urlsafe_b64encode(b"\xff").decode("ascii"),
        base64.urlsafe_b64encode(b"not-json").decode("ascii"),
        _cursor_from_payload([]),
    ],
)
def test_malformed_cursors_are_rejected_before_sql(cursor: str) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobCursorValidationError,
        match="^Invalid webhook delivery job cursor$",
    ):
        list_webhook_delivery_jobs(session, status=None, limit=50, cursor=cursor)

    assert session.mock_calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("v", 2),
        ("id", "not-a-uuid"),
        ("updated_at", "not-a-datetime"),
        ("updated_at", "2026-08-01T12:00:00"),
        ("status", "invalid"),
    ],
)
def test_invalid_cursor_fields_are_rejected(field: str, value: object) -> None:
    payload = _cursor_payload(_valid_cursor())
    payload[field] = value
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobCursorValidationError,
        match="^Invalid webhook delivery job cursor$",
    ):
        list_webhook_delivery_jobs(
            session,
            status=None,
            limit=50,
            cursor=_cursor_from_payload(payload),
        )

    assert session.mock_calls == []


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_cursor_requires_exact_payload_fields(mutation: str) -> None:
    payload = _cursor_payload(_valid_cursor())
    if mutation == "missing":
        del payload["id"]
    else:
        payload["extra"] = True
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobCursorValidationError,
        match="^Invalid webhook delivery job cursor$",
    ):
        list_webhook_delivery_jobs(
            session,
            status=None,
            limit=50,
            cursor=_cursor_from_payload(payload),
        )

    assert session.mock_calls == []


@pytest.mark.parametrize(
    ("cursor_status", "request_status"),
    [
        ("dead_letter", "succeeded"),
        ("dead_letter", None),
        (None, "dead_letter"),
    ],
)
def test_cursor_filter_mismatch_is_rejected(
    cursor_status: WebhookDeliveryJobStatus | None,
    request_status: WebhookDeliveryJobStatus | None,
) -> None:
    session = Mock(spec=Session)

    with pytest.raises(
        WebhookDeliveryJobCursorValidationError,
        match="^Invalid webhook delivery job cursor$",
    ):
        list_webhook_delivery_jobs(
            session,
            status=request_status,
            limit=50,
            cursor=_valid_cursor(status=cursor_status),
        )

    assert session.mock_calls == []


@pytest.mark.parametrize(
    ("jobs", "limit", "expected_ids", "has_cursor"),
    [
        ((), 2, (), False),
        ((_job(identifier=3),), 2, (3,), False),
        ((_job(identifier=3), _job(identifier=2)), 2, (3, 2), False),
        ((_job(identifier=3), _job(identifier=2), _job(identifier=1)), 2, (3, 2), True),
    ],
)
def test_page_boundaries_use_limit_plus_one_and_immutable_items(
    jobs: tuple[WebhookDeliveryJob, ...],
    limit: int,
    expected_ids: tuple[int, ...],
    has_cursor: bool,
) -> None:
    session = _session_with_jobs(*jobs)

    page = list_webhook_delivery_jobs(session, status=None, limit=limit, cursor=None)

    assert isinstance(page.items, tuple)
    assert tuple(item.id.int for item in page.items) == expected_ids
    assert (page.next_cursor is not None) is has_cursor
    statement = session.scalars.call_args.args[0]
    assert statement.compile().params["param_1"] == limit + 1
    if page.next_cursor is not None:
        payload = _cursor_payload(page.next_cursor)
        assert payload["id"] == str(page.items[-1].id)
        assert payload["status"] is None


def test_list_propagates_database_error_without_session_ownership() -> None:
    session = Mock(spec=Session)
    error = SQLAlchemyError("select failed")
    session.scalars.side_effect = error

    with pytest.raises(SQLAlchemyError) as raised:
        list_webhook_delivery_jobs(session, status=None, limit=50, cursor=None)

    assert raised.value is error
    _assert_no_session_ownership(session)
