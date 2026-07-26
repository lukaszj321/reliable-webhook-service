import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


@dataclass(slots=True)
class _CreatedRecords:
    marker: uuid.UUID
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords(marker=uuid.uuid4())

    try:
        yield records
    finally:
        with SessionFactory() as session:
            session.rollback()
            for job_id in records.job_ids:
                job = session.get(WebhookDeliveryJob, job_id)
                if job is not None:
                    session.delete(job)
            session.commit()

            for event_id in records.event_ids:
                event = session.get(WebhookEvent, event_id)
                if event is not None:
                    session.delete(event)
            session.commit()

            for endpoint_id in records.endpoint_ids:
                endpoint = session.get(WebhookEndpoint, endpoint_id)
                if endpoint is not None:
                    session.delete(endpoint)
            session.commit()

        with SessionFactory() as session:
            for job_id in records.job_ids:
                assert session.get(WebhookDeliveryJob, job_id) is None
            for event_id in records.event_ids:
                assert session.get(WebhookEvent, event_id) is None
            for endpoint_id in records.endpoint_ids:
                assert session.get(WebhookEndpoint, endpoint_id) is None


def _add_job(
    session: Session,
    records: _CreatedRecords,
    *,
    label: str,
    status: str,
    next_attempt_at: datetime | None,
    created_at: datetime,
    updated_at: datetime | None = None,
    job_id: uuid.UUID | None = None,
) -> WebhookDeliveryJob:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    resolved_job_id = uuid.uuid4() if job_id is None else job_id
    payload: dict[str, JsonValue] = {
        "claiming_test_marker": str(records.marker),
        "label": label,
    }

    records.endpoint_ids.append(endpoint_id)
    records.event_ids.append(event_id)
    records.job_ids.append(resolved_job_id)

    endpoint = WebhookEndpoint(
        id=endpoint_id,
        name=f"Delivery job claiming integration {records.marker} {label}",
        target_url=f"https://example.test/delivery-job-claiming/{records.marker}/{label}",
        is_active=True,
    )
    event = WebhookEvent(
        id=event_id,
        endpoint_id=endpoint_id,
        event_type="delivery.job.claiming.integration",
        payload=payload,
    )
    job = WebhookDeliveryJob(
        id=resolved_job_id,
        event_id=event_id,
        status=status,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=created_at if updated_at is None else updated_at,
    )
    session.add(endpoint)
    session.flush()
    session.add(event)
    session.flush()
    session.add(job)
    session.flush()
    return job


def _get_job(session: Session, job_id: uuid.UUID) -> WebhookDeliveryJob:
    job = session.get(WebhookDeliveryJob, job_id)
    assert job is not None
    return job


def _as_utc(value: datetime) -> datetime:
    assert value.tzinfo is not None
    assert value.utcoffset() is not None
    return value.astimezone(UTC)


def test_claims_only_due_pending_jobs(
    created_records: _CreatedRecords,
) -> None:
    base_time = datetime(2000, 1, 1, tzinfo=UTC)
    claimed_at = base_time + timedelta(days=10)

    with SessionFactory() as session:
        due_job = _add_job(
            session,
            created_records,
            label="due-pending",
            status="pending",
            next_attempt_at=base_time,
            created_at=base_time,
        )
        future_job = _add_job(
            session,
            created_records,
            label="future-pending",
            status="pending",
            next_attempt_at=claimed_at + timedelta(days=1),
            created_at=base_time + timedelta(seconds=1),
        )
        processing_job = _add_job(
            session,
            created_records,
            label="processing",
            status="processing",
            next_attempt_at=base_time,
            created_at=base_time + timedelta(seconds=2),
        )
        succeeded_job = _add_job(
            session,
            created_records,
            label="succeeded",
            status="succeeded",
            next_attempt_at=None,
            created_at=base_time + timedelta(seconds=3),
        )
        dead_letter_job = _add_job(
            session,
            created_records,
            label="dead-letter",
            status="dead_letter",
            next_attempt_at=None,
            created_at=base_time + timedelta(seconds=4),
        )
        session.commit()

        claimed_jobs = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=claimed_at,
            limit=10,
        )

        assert [job.id for job in claimed_jobs] == [due_job.id]
        assert _get_job(session, due_job.id).status == "processing"
        assert _get_job(session, future_job.id).status == "pending"
        assert _get_job(session, processing_job.id).status == "processing"
        assert _get_job(session, succeeded_job.id).status == "succeeded"
        assert _get_job(session, dead_letter_job.id).status == "dead_letter"
        session.rollback()


def test_returns_empty_list_when_no_job_is_due(
    created_records: _CreatedRecords,
) -> None:
    claimed_at = datetime(1900, 1, 1, tzinfo=UTC)

    with SessionFactory() as session:
        future_job = _add_job(
            session,
            created_records,
            label="empty-future-pending",
            status="pending",
            next_attempt_at=claimed_at + timedelta(days=1),
            created_at=claimed_at,
        )
        session.commit()

        claimed_jobs = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=claimed_at,
            limit=5,
        )

        assert claimed_jobs == []
        assert _get_job(session, future_job.id).status == "pending"


def test_claims_jobs_in_deterministic_order(
    created_records: _CreatedRecords,
) -> None:
    base_time = datetime(1900, 2, 1, tzinfo=UTC)
    first_schedule = base_time
    shared_schedule = base_time + timedelta(days=1)
    first_created = base_time + timedelta(days=2)
    second_created = base_time + timedelta(days=3)
    shared_created = base_time + timedelta(days=4)
    low_id, high_id = sorted((uuid.uuid4(), uuid.uuid4()))

    with SessionFactory() as session:
        earliest_schedule_job = _add_job(
            session,
            created_records,
            label="order-earliest-schedule",
            status="pending",
            next_attempt_at=first_schedule,
            created_at=shared_created,
        )
        earliest_created_job = _add_job(
            session,
            created_records,
            label="order-earliest-created",
            status="pending",
            next_attempt_at=shared_schedule,
            created_at=first_created,
        )
        later_created_job = _add_job(
            session,
            created_records,
            label="order-later-created",
            status="pending",
            next_attempt_at=shared_schedule,
            created_at=second_created,
        )
        lower_id_job = _add_job(
            session,
            created_records,
            label="order-lower-id",
            status="pending",
            next_attempt_at=shared_schedule,
            created_at=shared_created,
            job_id=low_id,
        )
        higher_id_job = _add_job(
            session,
            created_records,
            label="order-higher-id",
            status="pending",
            next_attempt_at=shared_schedule,
            created_at=shared_created,
            job_id=high_id,
        )
        session.commit()

        claimed_jobs = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=shared_schedule + timedelta(days=1),
            limit=5,
        )

        assert [job.id for job in claimed_jobs] == [
            earliest_schedule_job.id,
            earliest_created_job.id,
            later_created_job.id,
            lower_id_job.id,
            higher_id_job.id,
        ]
        session.rollback()


def test_limit_returns_two_disjoint_ordered_batches(
    created_records: _CreatedRecords,
) -> None:
    base_time = datetime(1900, 3, 1, tzinfo=UTC)

    with SessionFactory() as session:
        jobs = [
            _add_job(
                session,
                created_records,
                label=f"limit-{index}",
                status="pending",
                next_attempt_at=base_time + timedelta(seconds=index),
                created_at=base_time,
            )
            for index in range(4)
        ]
        session.commit()

        first_batch = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=base_time + timedelta(days=1),
            limit=2,
        )
        first_ids = [job.id for job in first_batch]

        assert first_ids == [jobs[0].id, jobs[1].id]
        assert [_get_job(session, job.id).status for job in jobs] == [
            "processing",
            "processing",
            "pending",
            "pending",
        ]

        second_batch = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=base_time + timedelta(days=1),
            limit=2,
        )
        second_ids = [job.id for job in second_batch]

        assert second_ids == [jobs[2].id, jobs[3].id]
        assert set(first_ids).isdisjoint(second_ids)
        assert [_get_job(session, job.id).status for job in jobs] == [
            "processing",
            "processing",
            "processing",
            "processing",
        ]
        session.rollback()


def test_claim_updates_only_state_fields_and_flushes(
    created_records: _CreatedRecords,
) -> None:
    next_attempt_at = datetime(1900, 4, 1, 8, 0, tzinfo=UTC)
    created_at = datetime(1900, 4, 1, 9, 0, tzinfo=UTC)
    initial_updated_at = datetime(1900, 4, 1, 10, 0, tzinfo=UTC)
    claimed_at = datetime(
        1900,
        4,
        2,
        12,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    with SessionFactory() as session:
        job = _add_job(
            session,
            created_records,
            label="state-transition",
            status="pending",
            next_attempt_at=next_attempt_at,
            created_at=created_at,
            updated_at=initial_updated_at,
        )
        session.commit()
        original_id = job.id
        original_event_id = job.event_id

        claimed_jobs = claim_due_webhook_delivery_jobs(
            session,
            claimed_at=claimed_at,
            limit=1,
        )

        assert claimed_jobs == [job]
        assert job.id == original_id
        assert job.event_id == original_event_id
        assert _as_utc(job.created_at) == created_at
        assert job.next_attempt_at is not None
        assert _as_utc(job.next_attempt_at) == next_attempt_at
        assert job.status == "processing"
        assert _as_utc(job.updated_at) == claimed_at.astimezone(UTC)

        with session.no_autoflush:
            stored_status, stored_updated_at = session.execute(
                select(
                    WebhookDeliveryJob.status,
                    WebhookDeliveryJob.updated_at,
                ).where(WebhookDeliveryJob.id == original_id)
            ).one()

        assert stored_status == "processing"
        assert _as_utc(stored_updated_at) == claimed_at.astimezone(UTC)
        session.rollback()


def test_claim_does_not_commit_and_rollback_restores_visibility(
    created_records: _CreatedRecords,
) -> None:
    next_attempt_at = datetime(1900, 5, 1, tzinfo=UTC)

    with SessionFactory() as setup_session:
        job = _add_job(
            setup_session,
            created_records,
            label="no-automatic-commit",
            status="pending",
            next_attempt_at=next_attempt_at,
            created_at=next_attempt_at,
        )
        setup_session.commit()
        job_id = job.id

    with SessionFactory() as claiming_session:
        claimed_jobs = claim_due_webhook_delivery_jobs(
            claiming_session,
            claimed_at=next_attempt_at + timedelta(days=1),
            limit=1,
        )

        assert [job.id for job in claimed_jobs] == [job_id]
        assert _get_job(claiming_session, job_id).status == "processing"

        with SessionFactory() as observing_session:
            visible_status = observing_session.scalar(
                select(WebhookDeliveryJob.status).where(WebhookDeliveryJob.id == job_id)
            )

        assert visible_status == "pending"
        claiming_session.rollback()

    with SessionFactory() as verification_session:
        assert _get_job(verification_session, job_id).status == "pending"


def test_claim_rollback_restores_original_values(
    created_records: _CreatedRecords,
) -> None:
    next_attempt_at = datetime(1900, 6, 1, tzinfo=UTC)
    original_updated_at = datetime(1900, 6, 1, 1, 0, tzinfo=UTC)

    with SessionFactory() as setup_session:
        job = _add_job(
            setup_session,
            created_records,
            label="rollback",
            status="pending",
            next_attempt_at=next_attempt_at,
            created_at=next_attempt_at,
            updated_at=original_updated_at,
        )
        setup_session.commit()
        job_id = job.id

    with SessionFactory() as claiming_session:
        claimed_jobs = claim_due_webhook_delivery_jobs(
            claiming_session,
            claimed_at=next_attempt_at + timedelta(days=1),
            limit=1,
        )

        assert [job.id for job in claimed_jobs] == [job_id]
        assert _get_job(claiming_session, job_id).status == "processing"
        claiming_session.rollback()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, job_id)
        assert stored_job.status == "pending"
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == next_attempt_at
        assert _as_utc(stored_job.updated_at) == original_updated_at


def test_caller_commit_persists_claim(
    created_records: _CreatedRecords,
) -> None:
    next_attempt_at = datetime(1900, 7, 1, tzinfo=UTC)
    claimed_at = datetime(
        1900,
        7,
        2,
        12,
        0,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    with SessionFactory() as setup_session:
        job = _add_job(
            setup_session,
            created_records,
            label="commit",
            status="pending",
            next_attempt_at=next_attempt_at,
            created_at=next_attempt_at,
        )
        setup_session.commit()
        job_id = job.id

    with SessionFactory() as claiming_session:
        claimed_jobs = claim_due_webhook_delivery_jobs(
            claiming_session,
            claimed_at=claimed_at,
            limit=1,
        )

        assert [job.id for job in claimed_jobs] == [job_id]
        claiming_session.commit()

    with SessionFactory() as verification_session:
        stored_job = _get_job(verification_session, job_id)
        assert stored_job.status == "processing"
        assert stored_job.next_attempt_at is not None
        assert _as_utc(stored_job.next_attempt_at) == next_attempt_at
        assert _as_utc(stored_job.updated_at) == claimed_at.astimezone(UTC)


def test_concurrent_claimers_skip_locked_jobs(
    created_records: _CreatedRecords,
) -> None:
    base_time = datetime(1900, 8, 1, tzinfo=UTC)
    claimed_at = base_time + timedelta(days=1)
    expected_next_attempts = [base_time + timedelta(seconds=index) for index in range(3)]

    with SessionFactory() as setup_session:
        jobs = [
            _add_job(
                setup_session,
                created_records,
                label=f"concurrent-{label}",
                status="pending",
                next_attempt_at=next_attempt_at,
                created_at=base_time,
            )
            for label, next_attempt_at in zip(
                ("a", "b", "c"),
                expected_next_attempts,
                strict=True,
            )
        ]
        setup_session.commit()
        expected_job_ids = [job.id for job in jobs]

    assert all(next_attempt_at <= claimed_at for next_attempt_at in expected_next_attempts)

    session_a = SessionFactory()
    session_b = SessionFactory()
    try:
        claimed_by_a = claim_due_webhook_delivery_jobs(
            session_a,
            claimed_at=claimed_at,
            limit=1,
        )
        claimed_by_a_ids = [job.id for job in claimed_by_a]

        assert len(claimed_by_a) == 1
        assert claimed_by_a_ids == [expected_job_ids[0]]
        assert _get_job(session_a, expected_job_ids[0]).status == "processing"
        assert session_a.in_transaction()

        session_b.execute(text("SET LOCAL lock_timeout = '1s'"))
        claimed_by_b = claim_due_webhook_delivery_jobs(
            session_b,
            claimed_at=claimed_at,
            limit=2,
        )
        claimed_by_b_ids = [job.id for job in claimed_by_b]

        assert len(claimed_by_b) == 2
        assert claimed_by_b_ids == expected_job_ids[1:]
        assert expected_job_ids[0] not in claimed_by_b_ids
        assert set(claimed_by_a_ids).isdisjoint(claimed_by_b_ids)
        assert set(claimed_by_a_ids + claimed_by_b_ids) == set(expected_job_ids)
        assert [_get_job(session_b, job_id).status for job_id in claimed_by_b_ids] == [
            "processing",
            "processing",
        ]

        visible_status = session_b.scalar(
            select(WebhookDeliveryJob.status).where(WebhookDeliveryJob.id == expected_job_ids[0])
        )
        assert visible_status == "pending"
    finally:
        session_b.rollback()
        session_a.rollback()
        session_b.close()
        session_a.close()

    with SessionFactory() as verification_session:
        for job_id, expected_next_attempt_at in zip(
            expected_job_ids,
            expected_next_attempts,
            strict=True,
        ):
            stored_job = _get_job(verification_session, job_id)
            assert stored_job.status == "pending"
            assert stored_job.next_attempt_at is not None
            assert _as_utc(stored_job.next_attempt_at) == expected_next_attempt_at
