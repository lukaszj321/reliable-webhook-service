import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


def _create_endpoint_and_event(
    session: Session,
    marker: uuid.UUID,
) -> tuple[WebhookEndpoint, WebhookEvent]:
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "constraint_test": True,
    }
    endpoint = WebhookEndpoint(
        name=f"Delivery job constraint {marker}",
        target_url=f"https://example.com/delivery-job-constraint/{marker}",
        is_active=True,
    )
    session.add(endpoint)
    session.flush()

    event = WebhookEvent(
        endpoint_id=endpoint.id,
        event_type="delivery.job.constraint",
        payload=payload,
    )
    session.add(event)
    session.flush()

    return endpoint, event


def test_reject_webhook_delivery_job_with_unsupported_status() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id = uuid.uuid4()
    next_attempt_at = datetime(
        2026,
        7,
        27,
        12,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    try:
        with SessionFactory() as session:
            endpoint, event = _create_endpoint_and_event(session, marker)
            endpoint_id = endpoint.id
            event_id = event.id
            session.commit()

            invalid_job = WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status="retrying",
                next_attempt_at=next_attempt_at,
            )
            session.add(invalid_job)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            assert session.get(WebhookDeliveryJob, job_id) is None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

        with SessionFactory() as session:
            assert session.get(WebhookDeliveryJob, job_id) is None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None
    finally:
        with SessionFactory() as session:
            stored_job = session.get(WebhookDeliveryJob, job_id)
            if stored_job is not None:
                session.delete(stored_job)
            session.commit()

            if event_id is not None:
                stored_event = session.get(WebhookEvent, event_id)
                if stored_event is not None:
                    session.delete(stored_event)
            session.commit()

            if endpoint_id is not None:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
            session.commit()

    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, job_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


@pytest.mark.parametrize(
    ("status", "next_attempt_at"),
    [
        pytest.param("pending", None, id="pending-without-schedule"),
        pytest.param("processing", None, id="processing-without-schedule"),
        pytest.param(
            "succeeded",
            datetime(2026, 7, 27, 12, 1, tzinfo=timezone(timedelta(hours=2))),
            id="succeeded-with-schedule",
        ),
        pytest.param(
            "dead_letter",
            datetime(2026, 7, 27, 12, 2, tzinfo=timezone(timedelta(hours=2))),
            id="dead-letter-with-schedule",
        ),
    ],
)
def test_reject_webhook_delivery_job_with_invalid_schedule_state(
    status: str,
    next_attempt_at: datetime | None,
) -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id = uuid.uuid4()

    try:
        with SessionFactory() as session:
            endpoint, event = _create_endpoint_and_event(session, marker)
            endpoint_id = endpoint.id
            event_id = event.id
            session.commit()

            invalid_job = WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status=status,
                next_attempt_at=next_attempt_at,
            )
            session.add(invalid_job)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            assert session.get(WebhookDeliveryJob, job_id) is None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

        with SessionFactory() as session:
            assert session.get(WebhookDeliveryJob, job_id) is None
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None
    finally:
        with SessionFactory() as session:
            stored_job = session.get(WebhookDeliveryJob, job_id)
            if stored_job is not None:
                session.delete(stored_job)
            session.commit()

            if event_id is not None:
                stored_event = session.get(WebhookEvent, event_id)
                if stored_event is not None:
                    session.delete(stored_event)
            session.commit()

            if endpoint_id is not None:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
            session.commit()

    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, job_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


def test_reject_webhook_delivery_job_for_missing_event() -> None:
    missing_event_id = uuid.uuid4()
    job_id = uuid.uuid4()
    next_attempt_at = datetime(
        2026,
        7,
        27,
        12,
        3,
        tzinfo=timezone(timedelta(hours=2)),
    )

    with SessionFactory() as session:
        invalid_job = WebhookDeliveryJob(
            id=job_id,
            event_id=missing_event_id,
            status="pending",
            next_attempt_at=next_attempt_at,
        )
        session.add(invalid_job)

        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        job_ids = set(session.scalars(select(WebhookDeliveryJob.id)).all())
        assert job_id not in job_ids
        assert session.get(WebhookEvent, missing_event_id) is None

    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, job_id) is None
        assert session.get(WebhookEvent, missing_event_id) is None


def test_reject_duplicate_webhook_delivery_job_for_event() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    first_job_id: uuid.UUID | None = None
    duplicate_job_id = uuid.uuid4()
    pending_at = datetime(
        2026,
        7,
        27,
        12,
        4,
        tzinfo=timezone(timedelta(hours=2)),
    )
    processing_at = datetime(
        2026,
        7,
        27,
        12,
        5,
        tzinfo=timezone(timedelta(hours=2)),
    )

    try:
        with SessionFactory() as session:
            endpoint, event = _create_endpoint_and_event(session, marker)
            endpoint_id = endpoint.id
            event_id = event.id

            first_job = WebhookDeliveryJob(
                event_id=event_id,
                status="pending",
                next_attempt_at=pending_at,
            )
            session.add(first_job)
            session.commit()
            session.refresh(first_job)
            first_job_id = first_job.id

            assert isinstance(first_job_id, uuid.UUID)

            duplicate_job = WebhookDeliveryJob(
                id=duplicate_job_id,
                event_id=event_id,
                status="processing",
                next_attempt_at=processing_at,
            )
            session.add(duplicate_job)

            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            assert session.get(WebhookDeliveryJob, first_job_id) is not None
            assert session.get(WebhookDeliveryJob, duplicate_job_id) is None

            matching_job_ids = session.scalars(
                select(WebhookDeliveryJob.id).where(WebhookDeliveryJob.event_id == event_id)
            ).all()
            assert matching_job_ids == [first_job_id]
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None

        with SessionFactory() as session:
            assert session.get(WebhookDeliveryJob, first_job_id) is not None
            assert session.get(WebhookDeliveryJob, duplicate_job_id) is None

            matching_job_ids = session.scalars(
                select(WebhookDeliveryJob.id).where(WebhookDeliveryJob.event_id == event_id)
            ).all()
            assert matching_job_ids == [first_job_id]
            assert session.get(WebhookEvent, event_id) is not None
            assert session.get(WebhookEndpoint, endpoint_id) is not None
    finally:
        with SessionFactory() as session:
            stored_duplicate_job = session.get(
                WebhookDeliveryJob,
                duplicate_job_id,
            )
            if stored_duplicate_job is not None:
                session.delete(stored_duplicate_job)
            session.commit()

            if first_job_id is not None:
                stored_first_job = session.get(WebhookDeliveryJob, first_job_id)
                if stored_first_job is not None:
                    session.delete(stored_first_job)
            session.commit()

            if event_id is not None:
                stored_event = session.get(WebhookEvent, event_id)
                if stored_event is not None:
                    session.delete(stored_event)
            session.commit()

            if endpoint_id is not None:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
            session.commit()

    assert first_job_id is not None
    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, duplicate_job_id) is None
        assert session.get(WebhookDeliveryJob, first_job_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None
