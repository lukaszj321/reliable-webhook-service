import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import String, UniqueConstraint, delete, func, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


@dataclass
class _CreatedRecords:
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: set[uuid.UUID] = field(default_factory=set)
    event_ids: set[uuid.UUID] = field(default_factory=set)


def _table_counts() -> tuple[int, int, int, int]:
    with SessionFactory() as session:
        endpoint_count = session.scalar(select(func.count()).select_from(WebhookEndpoint))
        event_count = session.scalar(select(func.count()).select_from(WebhookEvent))
        attempt_count = session.scalar(select(func.count()).select_from(WebhookDeliveryAttempt))
        job_count = session.scalar(select(func.count()).select_from(WebhookDeliveryJob))

    assert endpoint_count is not None
    assert event_count is not None
    assert attempt_count is not None
    assert job_count is not None
    return endpoint_count, event_count, attempt_count, job_count


def _cleanup_records(records: _CreatedRecords) -> None:
    with SessionFactory() as session:
        session.rollback()

        if records.event_ids:
            event_ids = tuple(records.event_ids)
            session.execute(
                delete(WebhookDeliveryAttempt).where(WebhookDeliveryAttempt.event_id.in_(event_ids))
            )
            session.execute(
                delete(WebhookDeliveryJob).where(WebhookDeliveryJob.event_id.in_(event_ids))
            )
            session.execute(delete(WebhookEvent).where(WebhookEvent.id.in_(event_ids)))

        if records.endpoint_ids:
            session.execute(
                delete(WebhookEndpoint).where(WebhookEndpoint.id.in_(tuple(records.endpoint_ids)))
            )

        session.commit()

    with SessionFactory() as session:
        assert all(session.get(WebhookEvent, event_id) is None for event_id in records.event_ids)
        assert all(
            session.get(WebhookEndpoint, endpoint_id) is None
            for endpoint_id in records.endpoint_ids
        )

    assert _table_counts() == records.initial_counts


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords(initial_counts=_table_counts())
    try:
        yield records
    finally:
        _cleanup_records(records)


def _create_endpoint(
    session: Session,
    records: _CreatedRecords,
    *,
    label: str,
) -> uuid.UUID:
    endpoint_id = uuid.uuid4()
    records.endpoint_ids.add(endpoint_id)
    endpoint = WebhookEndpoint(
        id=endpoint_id,
        name=f"Idempotency persistence {label} {endpoint_id}",
        target_url=f"https://example.test/idempotency/{label}/{endpoint_id}",
    )
    session.add(endpoint)
    session.flush()
    return endpoint_id


def _new_event(
    records: _CreatedRecords,
    *,
    endpoint_id: uuid.UUID,
    event_type: str,
    idempotency_key: str,
) -> WebhookEvent:
    event_id = uuid.uuid4()
    records.event_ids.add(event_id)
    payload: dict[str, JsonValue] = {"marker": str(event_id)}
    return WebhookEvent(
        id=event_id,
        endpoint_id=endpoint_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )


def test_webhook_event_idempotency_metadata() -> None:
    table = WebhookEvent.__table__
    idempotency_key_column = table.c.idempotency_key

    assert list(table.columns.keys()) == [
        "id",
        "endpoint_id",
        "event_type",
        "payload",
        "idempotency_key",
        "created_at",
    ]
    assert isinstance(idempotency_key_column.type, String)
    assert idempotency_key_column.type.length == 255
    assert idempotency_key_column.nullable is True
    assert idempotency_key_column.default is None
    assert idempotency_key_column.server_default is None

    constraint = next(
        item
        for item in table.constraints
        if item.name == "uq_webhook_events_endpoint_id_idempotency_key"
    )
    assert isinstance(constraint, UniqueConstraint)
    assert [column.name for column in constraint.columns] == [
        "endpoint_id",
        "idempotency_key",
    ]
    assert {index.name for index in table.indexes} == {"ix_webhook_events_endpoint_id"}


def test_allows_multiple_null_idempotency_keys_for_one_endpoint(
    created_records: _CreatedRecords,
) -> None:
    with SessionFactory() as session:
        endpoint_id = _create_endpoint(session, created_records, label="nullable")
        first_event_id = uuid.uuid4()
        second_event_id = uuid.uuid4()
        created_records.event_ids.update((first_event_id, second_event_id))
        first_event = WebhookEvent(
            id=first_event_id,
            endpoint_id=endpoint_id,
            event_type="idempotency.null.first",
            payload={"marker": str(first_event_id)},
        )
        second_event = WebhookEvent(
            id=second_event_id,
            endpoint_id=endpoint_id,
            event_type="idempotency.null.second",
            payload={"marker": str(second_event_id)},
        )
        session.add_all([first_event, second_event])
        session.commit()

    with SessionFactory() as session:
        stored_events = list(
            session.scalars(
                select(WebhookEvent)
                .where(WebhookEvent.id.in_((first_event_id, second_event_id)))
                .order_by(WebhookEvent.event_type)
            ).all()
        )

    assert len(stored_events) == 2
    assert all(event.idempotency_key is None for event in stored_events)


def test_persists_idempotency_key_and_accepts_255_characters(
    created_records: _CreatedRecords,
) -> None:
    short_key = "persisted-key"
    maximum_key = "k" * 255

    with SessionFactory() as session:
        endpoint_id = _create_endpoint(session, created_records, label="persisted")
        short_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.persisted.short",
            idempotency_key=short_key,
        )
        maximum_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.persisted.maximum",
            idempotency_key=maximum_key,
        )
        session.add_all([short_event, maximum_event])
        session.commit()
        short_event_id = short_event.id
        maximum_event_id = maximum_event.id

    with SessionFactory() as session:
        stored_short_event = session.get(WebhookEvent, short_event_id)
        stored_maximum_event = session.get(WebhookEvent, maximum_event_id)

    assert stored_short_event is not None
    assert stored_short_event.idempotency_key == short_key
    assert stored_maximum_event is not None
    assert stored_maximum_event.idempotency_key == maximum_key
    assert len(stored_maximum_event.idempotency_key) == 255


def test_rejects_duplicate_key_for_same_endpoint(
    created_records: _CreatedRecords,
) -> None:
    duplicate_key = "duplicate-key"

    with SessionFactory() as session:
        endpoint_id = _create_endpoint(session, created_records, label="duplicate")
        first_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.duplicate.first",
            idempotency_key=duplicate_key,
        )
        session.add(first_event)
        session.commit()

        duplicate_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.duplicate.second",
            idempotency_key=duplicate_key,
        )
        session.add(duplicate_event)

        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        assert session.scalar(text("SELECT 1")) == 1
        matching_count = session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.endpoint_id == endpoint_id,
                WebhookEvent.idempotency_key == duplicate_key,
            )
        )
        assert matching_count == 1
        assert session.get(WebhookEvent, first_event.id) is not None
        assert session.get(WebhookEvent, duplicate_event.id) is None


def test_allows_same_key_for_different_endpoints(
    created_records: _CreatedRecords,
) -> None:
    shared_key = "shared-endpoint-key"

    with SessionFactory() as session:
        first_endpoint_id = _create_endpoint(session, created_records, label="shared-first")
        second_endpoint_id = _create_endpoint(session, created_records, label="shared-second")
        first_event = _new_event(
            created_records,
            endpoint_id=first_endpoint_id,
            event_type="idempotency.shared.first",
            idempotency_key=shared_key,
        )
        second_event = _new_event(
            created_records,
            endpoint_id=second_endpoint_id,
            event_type="idempotency.shared.second",
            idempotency_key=shared_key,
        )
        session.add_all([first_event, second_event])
        session.commit()

    with SessionFactory() as session:
        stored_events = list(
            session.scalars(
                select(WebhookEvent).where(WebhookEvent.id.in_((first_event.id, second_event.id)))
            ).all()
        )

    assert len(stored_events) == 2
    assert {event.endpoint_id for event in stored_events} == {
        first_endpoint_id,
        second_endpoint_id,
    }
    assert {event.idempotency_key for event in stored_events} == {shared_key}


def test_idempotency_key_is_case_sensitive(
    created_records: _CreatedRecords,
) -> None:
    with SessionFactory() as session:
        endpoint_id = _create_endpoint(session, created_records, label="case-sensitive")
        upper_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.case.upper",
            idempotency_key="Order-123",
        )
        lower_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.case.lower",
            idempotency_key="order-123",
        )
        session.add_all([upper_event, lower_event])
        session.commit()

    with SessionFactory() as session:
        stored_keys = set(
            session.scalars(
                select(WebhookEvent.idempotency_key).where(
                    WebhookEvent.id.in_((upper_event.id, lower_event.id))
                )
            ).all()
        )

    assert stored_keys == {"Order-123", "order-123"}


def test_rejects_idempotency_key_longer_than_255_characters(
    created_records: _CreatedRecords,
) -> None:
    oversized_key = "k" * 256

    with SessionFactory() as session:
        endpoint_id = _create_endpoint(session, created_records, label="oversized")
        oversized_event = _new_event(
            created_records,
            endpoint_id=endpoint_id,
            event_type="idempotency.oversized",
            idempotency_key=oversized_key,
        )
        session.add(oversized_event)

        with pytest.raises(DataError):
            session.commit()
        session.rollback()

        assert session.scalar(text("SELECT 1")) == 1
        assert session.get(WebhookEvent, oversized_event.id) is None

    with SessionFactory() as session:
        assert session.get(WebhookEvent, oversized_event.id) is None
