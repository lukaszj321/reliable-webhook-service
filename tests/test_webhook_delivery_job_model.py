import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import CheckConstraint, DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from reliable_webhook_service.models import WebhookDeliveryJob


def test_delivery_job_table_has_expected_columns() -> None:
    table = WebhookDeliveryJob.__table__

    assert table.name == "webhook_delivery_jobs"
    assert set(table.columns.keys()) == {
        "id",
        "event_id",
        "status",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }


def test_delivery_job_primary_key_uses_uuid_default() -> None:
    table = WebhookDeliveryJob.__table__
    id_column = table.c.id

    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert id_column.nullable is False
    assert isinstance(id_column.type, UUID)
    assert id_column.type.as_uuid is True
    assert id_column.default is not None
    assert id_column.default.is_callable

    first_id = id_column.default.arg(None)
    second_id = id_column.default.arg(None)
    assert isinstance(first_id, uuid.UUID)
    assert isinstance(second_id, uuid.UUID)
    assert first_id != second_id


def test_delivery_job_event_foreign_key_cascades_on_delete() -> None:
    event_id_column = WebhookDeliveryJob.__table__.c.event_id
    foreign_keys = list(event_id_column.foreign_keys)

    assert event_id_column.nullable is False
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "webhook_events.id"
    assert foreign_keys[0].ondelete == "CASCADE"


def test_delivery_job_event_id_is_unique() -> None:
    constraint = next(
        item
        for item in WebhookDeliveryJob.__table__.constraints
        if item.name == "uq_webhook_delivery_jobs_event_id"
    )

    assert isinstance(constraint, UniqueConstraint)
    assert set(constraint.columns.keys()) == {"event_id"}
    assert WebhookDeliveryJob.__table__.c.event_id.index is not True


def test_delivery_job_status_constraint_has_exact_values() -> None:
    status_column = WebhookDeliveryJob.__table__.c.status
    constraint = next(
        item
        for item in WebhookDeliveryJob.__table__.constraints
        if item.name == "ck_webhook_delivery_jobs_status"
    )

    assert isinstance(status_column.type, String)
    assert status_column.type.length == 32
    assert status_column.nullable is False
    assert isinstance(constraint, CheckConstraint)

    sql = str(constraint.sqltext).lower()
    assert "status" in sql
    assert " in " in sql
    assert set(re.findall(r"'([^']+)'", sql)) == {
        "pending",
        "processing",
        "succeeded",
        "dead_letter",
    }


def test_delivery_job_status_controls_next_attempt_at_nullability() -> None:
    next_attempt_at_column = WebhookDeliveryJob.__table__.c.next_attempt_at
    constraint = next(
        item
        for item in WebhookDeliveryJob.__table__.constraints
        if item.name == "ck_webhook_delivery_jobs_status_next_attempt_at"
    )

    assert isinstance(next_attempt_at_column.type, DateTime)
    assert next_attempt_at_column.type.timezone is True
    assert next_attempt_at_column.nullable is True
    assert isinstance(constraint, CheckConstraint)

    sql = " ".join(str(constraint.sqltext).lower().split())
    assert "status in ('pending', 'processing')" in sql
    assert "next_attempt_at is not null" in sql
    assert "status in ('succeeded', 'dead_letter')" in sql
    assert "next_attempt_at is null" in sql
    assert set(re.findall(r"'([^']+)'", sql)) == {
        "pending",
        "processing",
        "succeeded",
        "dead_letter",
    }


def test_delivery_job_timestamps_have_server_defaults_without_updates() -> None:
    table = WebhookDeliveryJob.__table__

    for column_name in ("created_at", "updated_at"):
        column = table.c[column_name]
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is True
        assert column.nullable is False
        assert column.server_default is not None

    updated_at_column = table.c.updated_at
    assert updated_at_column.onupdate is None
    assert updated_at_column.server_onupdate is None


@pytest.mark.parametrize(
    ("status", "next_attempt_at"),
    [
        pytest.param(
            "pending",
            datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
            id="pending",
        ),
        pytest.param(
            "processing",
            datetime(2026, 7, 26, 10, 1, tzinfo=UTC),
            id="processing",
        ),
        pytest.param("succeeded", None, id="succeeded"),
        pytest.param("dead_letter", None, id="dead-letter"),
    ],
)
def test_construct_delivery_job(
    status: str,
    next_attempt_at: datetime | None,
) -> None:
    event_id = uuid.uuid4()

    job = WebhookDeliveryJob(
        event_id=event_id,
        status=status,
        next_attempt_at=next_attempt_at,
    )

    assert job.event_id == event_id
    assert job.status == status
    assert job.next_attempt_at == next_attempt_at
