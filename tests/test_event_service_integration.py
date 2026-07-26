import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.event_service import (
    WebhookEndpointNotFoundError,
    create_webhook_event_with_delivery_job,
)
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


def _table_counts() -> tuple[int, int, int]:
    with SessionFactory() as session:
        endpoint_count = session.scalar(select(func.count()).select_from(WebhookEndpoint))
        event_count = session.scalar(select(func.count()).select_from(WebhookEvent))
        job_count = session.scalar(select(func.count()).select_from(WebhookDeliveryJob))

    assert endpoint_count is not None
    assert event_count is not None
    assert job_count is not None
    return endpoint_count, event_count, job_count


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


def _create_endpoint(*, marker: uuid.UUID, is_active: bool) -> uuid.UUID:
    with SessionFactory() as session:
        endpoint = WebhookEndpoint(
            name=f"Atomic event integration {marker}",
            target_url=f"https://example.test/atomic-event/{marker}",
            is_active=is_active,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)

        assert isinstance(endpoint.id, uuid.UUID)
        return endpoint.id


def _job_for_event(
    session: Session,
    event_id: uuid.UUID,
) -> WebhookDeliveryJob | None:
    statement = select(WebhookDeliveryJob).where(WebhookDeliveryJob.event_id == event_id)
    return session.scalars(statement).one_or_none()


def _cleanup_records(
    *,
    endpoint_id: uuid.UUID | None,
    event_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    expected_counts: tuple[int, int, int],
) -> None:
    with SessionFactory() as session:
        session.rollback()

        if job_id is not None:
            job = session.get(WebhookDeliveryJob, job_id)
            if job is not None:
                session.delete(job)
        session.commit()

        if event_id is not None:
            event = session.get(WebhookEvent, event_id)
            if event is not None:
                session.delete(event)
        session.commit()

        if endpoint_id is not None:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if endpoint is not None:
                session.delete(endpoint)
        session.commit()

    with SessionFactory() as session:
        if job_id is not None:
            assert session.get(WebhookDeliveryJob, job_id) is None
        if event_id is not None:
            assert session.get(WebhookEvent, event_id) is None
        if endpoint_id is not None:
            assert session.get(WebhookEndpoint, endpoint_id) is None

    assert _table_counts() == expected_counts


def test_creates_event_and_pending_job_in_open_transaction() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    working_session = SessionFactory()

    try:
        endpoint_id = _create_endpoint(marker=marker, is_active=True)
        event_type = f"atomic.open-transaction.{marker}"
        payload: dict[str, JsonValue] = {"marker": str(marker)}

        event = create_webhook_event_with_delivery_job(
            working_session,
            endpoint_id=endpoint_id,
            event_type=event_type,
            payload=payload,
        )
        event_id = event.id
        job = _job_for_event(working_session, event_id)
        assert job is not None
        job_id = job.id

        assert isinstance(event, WebhookEvent)
        assert event.endpoint_id == endpoint_id
        assert event.event_type == event_type
        assert event.payload == payload
        assert isinstance(event.id, uuid.UUID)
        assert _as_utc(event.created_at) == event.created_at.astimezone(UTC)
        assert isinstance(job.id, uuid.UUID)
        assert _as_utc(job.created_at) == job.created_at.astimezone(UTC)
        assert _as_utc(job.updated_at) == job.updated_at.astimezone(UTC)
        assert job.event_id == event.id
        assert job.status == "pending"
        assert job.next_attempt_at is not None
        assert _as_utc(job.next_attempt_at) == _as_utc(event.created_at)
        assert _job_for_event(working_session, event.id) is job

        working_session.rollback()

        with SessionFactory() as verification_session:
            assert verification_session.get(WebhookEvent, event_id) is None
            assert verification_session.get(WebhookDeliveryJob, job_id) is None
            assert verification_session.get(WebhookEndpoint, endpoint_id) is not None
    finally:
        if working_session.in_transaction():
            working_session.rollback()
        working_session.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            job_id=job_id,
            expected_counts=initial_counts,
        )


def test_uncommitted_event_and_job_are_hidden_then_become_visible_together() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    session_a = SessionFactory()

    try:
        endpoint_id = _create_endpoint(marker=marker, is_active=True)
        event = create_webhook_event_with_delivery_job(
            session_a,
            endpoint_id=endpoint_id,
            event_type=f"atomic.visibility.{marker}",
            payload={"marker": str(marker)},
        )
        event_id = event.id
        job = _job_for_event(session_a, event_id)
        assert job is not None
        job_id = job.id

        session_b = SessionFactory()
        try:
            assert session_b.get(WebhookEvent, event_id) is None
            assert session_b.get(WebhookDeliveryJob, job_id) is None
            assert _job_for_event(session_b, event_id) is None
            session_b.rollback()
        finally:
            if session_b.in_transaction():
                session_b.rollback()
            session_b.close()

        session_a.commit()

        with SessionFactory() as session_c:
            stored_event = session_c.get(WebhookEvent, event_id)
            stored_job = session_c.get(WebhookDeliveryJob, job_id)
            event_job = _job_for_event(session_c, event_id)

            assert stored_event is not None
            assert stored_job is not None
            assert event_job is not None
            assert event_job.id == stored_job.id
            assert stored_job.status == "pending"
            assert stored_job.next_attempt_at is not None
            assert _as_utc(stored_job.next_attempt_at) == _as_utc(stored_event.created_at)
    finally:
        if session_a.in_transaction():
            session_a.rollback()
        session_a.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            job_id=job_id,
            expected_counts=initial_counts,
        )


def test_caller_rollback_removes_event_and_delivery_job() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    session_a = SessionFactory()

    try:
        endpoint_id = _create_endpoint(marker=marker, is_active=True)
        event = create_webhook_event_with_delivery_job(
            session_a,
            endpoint_id=endpoint_id,
            event_type=f"atomic.rollback.{marker}",
            payload={"marker": str(marker)},
        )
        event_id = event.id
        job = _job_for_event(session_a, event_id)
        assert job is not None
        job_id = job.id

        session_a.rollback()

        with SessionFactory() as session_b:
            assert session_b.get(WebhookEvent, event_id) is None
            assert session_b.get(WebhookDeliveryJob, job_id) is None
            assert _job_for_event(session_b, event_id) is None
            endpoint = session_b.get(WebhookEndpoint, endpoint_id)
            assert endpoint is not None
    finally:
        if session_a.in_transaction():
            session_a.rollback()
        session_a.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            job_id=job_id,
            expected_counts=initial_counts,
        )


def test_accepts_inactive_endpoint_and_commits_event_with_job() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    caller_session = SessionFactory()

    try:
        endpoint_id = _create_endpoint(marker=marker, is_active=False)
        event = create_webhook_event_with_delivery_job(
            caller_session,
            endpoint_id=endpoint_id,
            event_type=f"atomic.inactive.{marker}",
            payload={"marker": str(marker)},
        )
        event_id = event.id
        job = _job_for_event(caller_session, event_id)
        assert job is not None
        job_id = job.id
        caller_session.commit()

        with SessionFactory() as verification_session:
            stored_endpoint = verification_session.get(WebhookEndpoint, endpoint_id)
            stored_event = verification_session.get(WebhookEvent, event_id)
            stored_job = _job_for_event(verification_session, event_id)

            assert stored_endpoint is not None
            assert stored_endpoint.is_active is False
            assert stored_event is not None
            assert stored_job is not None
            assert stored_job.id == job_id
            assert stored_job.status == "pending"
            assert stored_job.next_attempt_at is not None
            assert _as_utc(stored_job.next_attempt_at) == _as_utc(stored_event.created_at)
    finally:
        if caller_session.in_transaction():
            caller_session.rollback()
        caller_session.close()
        _cleanup_records(
            endpoint_id=endpoint_id,
            event_id=event_id,
            job_id=job_id,
            expected_counts=initial_counts,
        )


def test_missing_endpoint_creates_neither_event_nor_job() -> None:
    initial_counts = _table_counts()
    marker = uuid.uuid4()
    missing_endpoint_id = uuid.uuid4()
    event_type = f"atomic.missing.{marker}"
    session = SessionFactory()

    try:
        assert session.get(WebhookEndpoint, missing_endpoint_id) is None

        with pytest.raises(
            WebhookEndpointNotFoundError,
            match="^Webhook endpoint not found$",
        ):
            create_webhook_event_with_delivery_job(
                session,
                endpoint_id=missing_endpoint_id,
                event_type=event_type,
                payload={"marker": str(marker)},
            )

        assert not session.new
        event_ids = list(
            session.scalars(
                select(WebhookEvent.id).where(
                    WebhookEvent.endpoint_id == missing_endpoint_id,
                    WebhookEvent.event_type == event_type,
                )
            ).all()
        )
        job_ids = list(
            session.scalars(
                select(WebhookDeliveryJob.id)
                .join(WebhookEvent, WebhookDeliveryJob.event_id == WebhookEvent.id)
                .where(
                    WebhookEvent.endpoint_id == missing_endpoint_id,
                    WebhookEvent.event_type == event_type,
                )
            ).all()
        )

        assert event_ids == []
        assert job_ids == []
        session.rollback()
    finally:
        if session.in_transaction():
            session.rollback()
        session.close()
        _cleanup_records(
            endpoint_id=None,
            event_id=None,
            job_id=None,
            expected_counts=initial_counts,
        )
