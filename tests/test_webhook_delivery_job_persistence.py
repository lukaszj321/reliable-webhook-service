import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


@pytest.mark.parametrize(
    ("status", "next_attempt_at"),
    [
        pytest.param(
            "pending",
            datetime(2026, 7, 26, 12, 0, tzinfo=timezone(timedelta(hours=2))),
            id="pending",
        ),
        pytest.param(
            "processing",
            datetime(2026, 7, 26, 12, 1, tzinfo=timezone(timedelta(hours=2))),
            id="processing",
        ),
        pytest.param("succeeded", None, id="succeeded"),
        pytest.param("dead_letter", None, id="dead-letter"),
    ],
)
def test_persist_webhook_delivery_job(
    status: str,
    next_attempt_at: datetime | None,
) -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "persistence_test": True,
    }
    target_url = f"https://example.com/delivery-job/{marker}"

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery job persistence {marker}",
                target_url=target_url,
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.job.persistence",
                payload=payload,
            )
            session.add(event)
            session.flush()
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            job = WebhookDeliveryJob(
                event_id=event_id,
                status=status,
                next_attempt_at=next_attempt_at,
            )
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()
            session.refresh(job)

            assert isinstance(job_id, uuid.UUID)
            assert job.event_id == event_id
            assert job.status == status
            if next_attempt_at is None:
                assert job.next_attempt_at is None
            else:
                assert isinstance(job.next_attempt_at, datetime)
                assert job.next_attempt_at.tzinfo is not None
                assert job.next_attempt_at.utcoffset() is not None
                assert job.next_attempt_at.astimezone(UTC) == next_attempt_at.astimezone(UTC)
            assert isinstance(job.created_at, datetime)
            assert job.created_at.tzinfo is not None
            assert job.created_at.utcoffset() is not None
            assert isinstance(job.updated_at, datetime)
            assert job.updated_at.tzinfo is not None
            assert job.updated_at.utcoffset() is not None

        with SessionFactory() as session:
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
            stored_event = session.get(WebhookEvent, event_id)
            stored_job = session.get(WebhookDeliveryJob, job_id)

            assert stored_endpoint is not None
            assert stored_event is not None
            assert stored_job is not None

            assert stored_event.endpoint_id == stored_endpoint.id
            assert stored_event.payload["marker"] == str(marker)
            assert stored_job.event_id == stored_event.id
            assert stored_job.status == status
            if next_attempt_at is None:
                assert stored_job.next_attempt_at is None
            else:
                assert isinstance(stored_job.next_attempt_at, datetime)
                assert stored_job.next_attempt_at.tzinfo is not None
                assert stored_job.next_attempt_at.utcoffset() is not None
                assert stored_job.next_attempt_at.astimezone(UTC) == next_attempt_at.astimezone(UTC)
            assert isinstance(stored_job.created_at, datetime)
            assert stored_job.created_at.tzinfo is not None
            assert stored_job.created_at.utcoffset() is not None
            assert isinstance(stored_job.updated_at, datetime)
            assert stored_job.updated_at.tzinfo is not None
            assert stored_job.updated_at.utcoffset() is not None
    finally:
        with SessionFactory() as session:
            if job_id is not None:
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

    assert job_id is not None
    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, job_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


def test_delete_webhook_event_cascades_delivery_job() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    payload: dict[str, JsonValue] = {
        "marker": str(marker),
        "cascade_test": True,
    }
    next_attempt_at = datetime(
        2026,
        7,
        26,
        12,
        2,
        tzinfo=timezone(timedelta(hours=2)),
    )

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Delivery job cascade {marker}",
                target_url=f"https://example.com/delivery-job-cascade/{marker}",
                is_active=True,
            )
            session.add(endpoint)
            session.flush()
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="delivery.job.cascade",
                payload=payload,
            )
            session.add(event)
            session.flush()
            event_id = event.id

            assert isinstance(event_id, uuid.UUID)

            job = WebhookDeliveryJob(
                event_id=event_id,
                status="pending",
                next_attempt_at=next_attempt_at,
            )
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()
            session.refresh(job)

            assert isinstance(job_id, uuid.UUID)

        with SessionFactory() as session:
            stored_event = session.get(WebhookEvent, event_id)

            assert stored_event is not None

            session.delete(stored_event)
            session.commit()

        with SessionFactory() as session:
            assert session.get(WebhookEvent, event_id) is None
            assert session.get(WebhookDeliveryJob, job_id) is None
            assert session.get(WebhookEndpoint, endpoint_id) is not None
    finally:
        with SessionFactory() as session:
            if job_id is not None:
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

    assert job_id is not None
    assert event_id is not None
    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookDeliveryJob, job_id) is None
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None
