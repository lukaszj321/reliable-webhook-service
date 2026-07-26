import uuid
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.orm import Session

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

ENDPOINT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
EVENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
JOB_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
EVENT_CREATED_AT = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
JOB_CREATED_AT = datetime(2026, 7, 26, 10, 0, 1, tzinfo=UTC)
JOB_UPDATED_AT = datetime(2026, 7, 26, 10, 0, 2, tzinfo=UTC)


class RecordingSession:
    def __init__(self, endpoint: WebhookEndpoint | None) -> None:
        self.endpoint = endpoint
        self.get_calls: list[tuple[type[WebhookEndpoint], uuid.UUID]] = []
        self.added_objects: list[WebhookEvent | WebhookDeliveryJob] = []
        self.operations: list[str] = []
        self.flush_count = 0
        self.commit_called = False
        self.rollback_called = False
        self.close_called = False

    def get(
        self,
        entity: type[WebhookEndpoint],
        identifier: uuid.UUID,
    ) -> WebhookEndpoint | None:
        self.get_calls.append((entity, identifier))
        self.operations.append("get")
        return self.endpoint

    def add(self, instance: WebhookEvent | WebhookDeliveryJob) -> None:
        self.added_objects.append(instance)
        if isinstance(instance, WebhookEvent):
            self.operations.append("add:event")
        elif isinstance(instance, WebhookDeliveryJob):
            self.operations.append("add:job")
        else:
            raise AssertionError(f"Unexpected object type: {type(instance).__name__}")

    def flush(self) -> None:
        self.flush_count += 1
        self.operations.append("flush")

        if self.flush_count == 1:
            event = next(
                instance for instance in self.added_objects if isinstance(instance, WebhookEvent)
            )
            event.id = EVENT_ID
            event.created_at = EVENT_CREATED_AT
        elif self.flush_count == 2:
            job = next(
                instance
                for instance in self.added_objects
                if isinstance(instance, WebhookDeliveryJob)
            )
            job.id = JOB_ID
            job.created_at = JOB_CREATED_AT
            job.updated_at = JOB_UPDATED_AT
        else:
            raise AssertionError("Unexpected flush")

    def commit(self) -> None:
        self.commit_called = True
        raise AssertionError("Service must not commit")

    def rollback(self) -> None:
        self.rollback_called = True
        raise AssertionError("Service must not roll back")

    def close(self) -> None:
        self.close_called = True
        raise AssertionError("Service must not close the session")


def _endpoint(*, is_active: bool) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=ENDPOINT_ID,
        name="Event service endpoint",
        target_url="https://example.test/event-service",
        is_active=is_active,
        created_at=EVENT_CREATED_AT,
        updated_at=EVENT_CREATED_AT,
    )


def _jobs(session: RecordingSession) -> list[WebhookDeliveryJob]:
    return [
        instance for instance in session.added_objects if isinstance(instance, WebhookDeliveryJob)
    ]


def test_rejects_missing_webhook_endpoint_before_mutation() -> None:
    session = RecordingSession(endpoint=None)
    payload: dict[str, JsonValue] = {"event": "missing-endpoint"}

    with pytest.raises(
        WebhookEndpointNotFoundError,
        match="^Webhook endpoint not found$",
    ):
        create_webhook_event_with_delivery_job(
            cast(Session, session),
            endpoint_id=ENDPOINT_ID,
            event_type="order.created",
            payload=payload,
        )

    assert session.get_calls == [(WebhookEndpoint, ENDPOINT_ID)]
    assert session.operations == ["get"]
    assert session.added_objects == []
    assert session.flush_count == 0
    assert session.commit_called is False
    assert session.rollback_called is False
    assert session.close_called is False


def test_creates_event_and_pending_delivery_job_with_two_flushes() -> None:
    session = RecordingSession(endpoint=_endpoint(is_active=True))
    payload: dict[str, JsonValue] = {
        "order_id": "order-123",
        "nested": {"paid": True},
    }

    event = create_webhook_event_with_delivery_job(
        cast(Session, session),
        endpoint_id=ENDPOINT_ID,
        event_type="order.created",
        payload=payload,
    )

    events = [instance for instance in session.added_objects if isinstance(instance, WebhookEvent)]
    jobs = _jobs(session)

    assert session.get_calls == [(WebhookEndpoint, ENDPOINT_ID)]
    assert session.operations == ["get", "add:event", "flush", "add:job", "flush"]
    assert len(events) == 1
    assert len(jobs) == 1
    assert event is events[0]
    assert event.endpoint_id == ENDPOINT_ID
    assert event.event_type == "order.created"
    assert event.payload is payload
    assert event.id == EVENT_ID
    assert event.created_at == EVENT_CREATED_AT
    assert event.created_at.tzinfo is not None
    assert event.created_at.utcoffset() is not None
    assert jobs[0].event_id == event.id
    assert jobs[0].status == "pending"
    assert jobs[0].next_attempt_at == event.created_at
    assert session.flush_count == 2
    assert session.commit_called is False
    assert session.rollback_called is False
    assert session.close_called is False


def test_accepts_inactive_endpoint_without_changing_creation_behavior() -> None:
    session = RecordingSession(endpoint=_endpoint(is_active=False))
    payload: dict[str, JsonValue] = {"event": "inactive-endpoint"}

    event = create_webhook_event_with_delivery_job(
        cast(Session, session),
        endpoint_id=ENDPOINT_ID,
        event_type="customer.updated",
        payload=payload,
    )

    jobs = _jobs(session)

    assert event.endpoint_id == ENDPOINT_ID
    assert event.payload is payload
    assert len(jobs) == 1
    assert jobs[0].event_id == event.id
    assert jobs[0].status == "pending"
    assert jobs[0].next_attempt_at == event.created_at
    assert session.operations == ["get", "add:event", "flush", "add:job", "flush"]
    assert session.flush_count == 2
    assert session.commit_called is False
    assert session.rollback_called is False
    assert session.close_called is False
