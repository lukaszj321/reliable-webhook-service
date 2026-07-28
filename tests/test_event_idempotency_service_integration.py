import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from queue import Queue
from threading import Event, Thread
from time import monotonic

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from reliable_webhook_service.database import SessionFactory, engine
from reliable_webhook_service.event_service import (
    WebhookEventCreationResult,
    WebhookEventIdempotencyConflictError,
    create_idempotent_webhook_event_with_delivery_job,
    create_webhook_event_with_delivery_job,
)
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


@dataclass(frozen=True, slots=True)
class _RaceOutcome:
    event_id: uuid.UUID | None
    created: bool | None
    error: BaseException | None
    session_usable: bool


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
        if records.endpoint_ids:
            discovered_event_ids = set(
                session.scalars(
                    select(WebhookEvent.id).where(
                        WebhookEvent.endpoint_id.in_(tuple(records.endpoint_ids))
                    )
                ).all()
            )
            records.event_ids.update(discovered_event_ids)

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

    assert _table_counts() == records.initial_counts


@pytest.fixture
def created_records() -> Iterator[_CreatedRecords]:
    records = _CreatedRecords(initial_counts=_table_counts())
    try:
        yield records
    finally:
        _cleanup_records(records)


def _create_endpoint(
    records: _CreatedRecords,
    *,
    label: str,
    is_active: bool = True,
) -> uuid.UUID:
    endpoint_id = uuid.uuid4()
    records.endpoint_ids.add(endpoint_id)
    with SessionFactory() as session:
        endpoint = WebhookEndpoint(
            id=endpoint_id,
            name=f"Idempotent event service {label} {endpoint_id}",
            target_url=f"https://example.test/idempotent-event/{label}/{endpoint_id}",
            is_active=is_active,
        )
        session.add(endpoint)
        session.commit()
    return endpoint_id


def _job_for_event(session: Session, event_id: uuid.UUID) -> WebhookDeliveryJob | None:
    return session.scalar(select(WebhookDeliveryJob).where(WebhookDeliveryJob.event_id == event_id))


def _event_count_for_endpoint(session: Session, endpoint_id: uuid.UUID) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(WebhookEvent)
        .where(WebhookEvent.endpoint_id == endpoint_id)
    )
    assert count is not None
    return count


def _job_count_for_endpoint(session: Session, endpoint_id: uuid.UUID) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(WebhookDeliveryJob)
        .join(WebhookEvent, WebhookDeliveryJob.event_id == WebhookEvent.id)
        .where(WebhookEvent.endpoint_id == endpoint_id)
    )
    assert count is not None
    return count


def _wait_for_transaction_lock(backend_pid: int, timeout_seconds: float = 5.0) -> bool:
    deadline = monotonic() + timeout_seconds
    with SessionFactory() as observer:
        while monotonic() < deadline:
            wait_state = observer.execute(
                text(
                    """
                    SELECT wait_event_type, wait_event
                    FROM pg_stat_activity
                    WHERE pid = :backend_pid
                    """
                ),
                {"backend_pid": backend_pid},
            ).one_or_none()
            observer.rollback()
            if (
                wait_state is not None
                and wait_state[0] == "Lock"
                and wait_state[1] == "transactionid"
            ):
                return True
    return False


def _execute_concurrent_race(
    records: _CreatedRecords,
    *,
    loser_event_type: str,
    loser_payload: dict[str, JsonValue],
) -> tuple[WebhookEventCreationResult, _RaceOutcome, bool]:
    endpoint_id = _create_endpoint(records, label=f"race-{uuid.uuid4()}")
    idempotency_key = f"race-key-{uuid.uuid4()}"
    winner_event_type = "race.event"
    winner_payload: dict[str, JsonValue] = {
        "order_id": "race-order",
        "active": True,
    }
    winner_session = SessionFactory()
    winner_result = create_idempotent_webhook_event_with_delivery_job(
        winner_session,
        endpoint_id=endpoint_id,
        event_type=winner_event_type,
        payload=winner_payload,
        idempotency_key=idempotency_key,
    )
    records.event_ids.add(winner_result.event.id)

    insert_started = Event()
    backend_pid_queue: Queue[int] = Queue()
    outcome_queue: Queue[_RaceOutcome] = Queue()

    def signal_loser_insert(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, parameters, context, executemany
        normalized_statement = " ".join(statement.lower().split())
        if normalized_statement.startswith("insert into webhook_events"):
            insert_started.set()

    def run_loser() -> None:
        loser_session = SessionFactory()
        try:
            backend_pid = loser_session.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(backend_pid, int)
            backend_pid_queue.put(backend_pid)

            try:
                result = create_idempotent_webhook_event_with_delivery_job(
                    loser_session,
                    endpoint_id=endpoint_id,
                    event_type=loser_event_type,
                    payload=loser_payload,
                    idempotency_key=idempotency_key,
                )
            except BaseException as error:
                session_usable = loser_session.scalar(text("SELECT 1")) == 1
                outcome_queue.put(
                    _RaceOutcome(
                        event_id=None,
                        created=None,
                        error=error,
                        session_usable=session_usable,
                    )
                )
            else:
                session_usable = loser_session.scalar(text("SELECT 1")) == 1
                outcome_queue.put(
                    _RaceOutcome(
                        event_id=result.event.id,
                        created=result.created,
                        error=None,
                        session_usable=session_usable,
                    )
                )
        except BaseException as error:
            outcome_queue.put(
                _RaceOutcome(
                    event_id=None,
                    created=None,
                    error=error,
                    session_usable=False,
                )
            )
        finally:
            if loser_session.in_transaction():
                loser_session.rollback()
            loser_session.close()

    sqlalchemy_event.listen(engine, "before_cursor_execute", signal_loser_insert)
    loser_thread = Thread(target=run_loser, name=f"idempotency-race-loser-{uuid.uuid4()}")
    lock_was_observed = False
    try:
        loser_thread.start()
        backend_pid = backend_pid_queue.get(timeout=5.0)
        assert insert_started.wait(timeout=5.0)
        assert loser_thread.is_alive()
        lock_was_observed = _wait_for_transaction_lock(backend_pid)
        assert lock_was_observed

        winner_session.commit()

        loser_thread.join(timeout=10.0)
        assert not loser_thread.is_alive()
        loser_outcome = outcome_queue.get(timeout=1.0)
    finally:
        if winner_session.in_transaction():
            winner_session.rollback()
        winner_session.close()
        if sqlalchemy_event.contains(engine, "before_cursor_execute", signal_loser_insert):
            sqlalchemy_event.remove(engine, "before_cursor_execute", signal_loser_insert)
        if loser_thread.is_alive():
            loser_thread.join(timeout=5.0)

    assert not loser_thread.is_alive()
    assert not sqlalchemy_event.contains(engine, "before_cursor_execute", signal_loser_insert)
    return winner_result, loser_outcome, lock_was_observed


def test_unkeyed_wrapper_creates_two_events_and_jobs(
    created_records: _CreatedRecords,
) -> None:
    endpoint_id = _create_endpoint(created_records, label="unkeyed")
    with SessionFactory() as session:
        first_event = create_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type="unkeyed.first",
            payload={"sequence": 1},
        )
        second_event = create_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type="unkeyed.second",
            payload={"sequence": 2},
        )
        session.commit()
        created_records.event_ids.update((first_event.id, second_event.id))

        assert isinstance(first_event, WebhookEvent)
        assert isinstance(second_event, WebhookEvent)
        assert first_event.id != second_event.id
        assert first_event.idempotency_key is None
        assert second_event.idempotency_key is None
        assert _job_for_event(session, first_event.id) is not None
        assert _job_for_event(session, second_event.id) is not None
        assert _event_count_for_endpoint(session, endpoint_id) == 2
        assert _job_count_for_endpoint(session, endpoint_id) == 2


def test_first_keyed_request_normalizes_key_and_accepts_inactive_endpoint(
    created_records: _CreatedRecords,
) -> None:
    endpoint_id = _create_endpoint(created_records, label="first-keyed", is_active=False)
    with SessionFactory() as session:
        result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type="keyed.first",
            payload={"first": True},
            idempotency_key="\t  Order-123  \n",
        )
        created_records.event_ids.add(result.event.id)
        job = _job_for_event(session, result.event.id)

        assert result.created is True
        assert result.event.idempotency_key == "Order-123"
        assert job is not None
        assert job.status == "pending"
        assert job.next_attempt_at == result.event.created_at
        assert _event_count_for_endpoint(session, endpoint_id) == 1
        assert _job_count_for_endpoint(session, endpoint_id) == 1
        session.commit()


def test_sequential_equivalent_duplicate_reuses_unchanged_event_and_job(
    created_records: _CreatedRecords,
) -> None:
    endpoint_id = _create_endpoint(created_records, label="sequential-equivalent")
    idempotency_key = f"equivalent-{uuid.uuid4()}"
    first_payload: dict[str, JsonValue] = {
        "order": {"id": "order-123", "paid": True},
        "items": [1, 2],
    }
    equivalent_payload: dict[str, JsonValue] = {
        "items": [1, 2],
        "order": {"paid": True, "id": "order-123"},
    }

    with SessionFactory() as first_session:
        first_result = create_idempotent_webhook_event_with_delivery_job(
            first_session,
            endpoint_id=endpoint_id,
            event_type="order.created",
            payload=first_payload,
            idempotency_key=idempotency_key,
        )
        first_session.commit()
        created_records.event_ids.add(first_result.event.id)
        first_job = _job_for_event(first_session, first_result.event.id)
        assert first_job is not None
        original_snapshot = (
            first_result.event.id,
            first_result.event.created_at,
            first_job.id,
            first_job.status,
            first_job.next_attempt_at,
            first_job.created_at,
            first_job.updated_at,
        )

    with SessionFactory() as second_session:
        second_result = create_idempotent_webhook_event_with_delivery_job(
            second_session,
            endpoint_id=endpoint_id,
            event_type="order.created",
            payload=equivalent_payload,
            idempotency_key=f"  {idempotency_key}  ",
        )
        second_job = _job_for_event(second_session, second_result.event.id)
        assert second_job is not None
        reused_snapshot = (
            second_result.event.id,
            second_result.event.created_at,
            second_job.id,
            second_job.status,
            second_job.next_attempt_at,
            second_job.created_at,
            second_job.updated_at,
        )

        assert second_result.created is False
        assert reused_snapshot == original_snapshot
        assert _event_count_for_endpoint(second_session, endpoint_id) == 1
        assert _job_count_for_endpoint(second_session, endpoint_id) == 1


@pytest.mark.parametrize(
    ("loser_event_type", "loser_payload"),
    [
        pytest.param(
            "order.updated",
            {"value": True, "nested": {"number": 1}},
            id="different-event-type",
        ),
        pytest.param(
            "order.created",
            {"value": False, "nested": {"number": 1}},
            id="different-payload",
        ),
        pytest.param(
            "order.created",
            {"value": 1, "nested": {"number": 1}},
            id="json-boolean-versus-number",
        ),
    ],
)
def test_sequential_conflict_preserves_existing_records(
    created_records: _CreatedRecords,
    loser_event_type: str,
    loser_payload: dict[str, JsonValue],
) -> None:
    endpoint_id = _create_endpoint(created_records, label=f"conflict-{uuid.uuid4()}")
    idempotency_key = f"conflict-key-{uuid.uuid4()}"
    winner_payload: dict[str, JsonValue] = {
        "value": True,
        "nested": {"number": 1},
    }

    with SessionFactory() as winner_session:
        winner_result = create_idempotent_webhook_event_with_delivery_job(
            winner_session,
            endpoint_id=endpoint_id,
            event_type="order.created",
            payload=winner_payload,
            idempotency_key=idempotency_key,
        )
        winner_session.commit()
        created_records.event_ids.add(winner_result.event.id)
        winner_job = _job_for_event(winner_session, winner_result.event.id)
        assert winner_job is not None
        original_snapshot = (
            winner_result.event.event_type,
            winner_result.event.payload,
            winner_result.event.created_at,
            winner_job.id,
            winner_job.status,
            winner_job.next_attempt_at,
            winner_job.created_at,
            winner_job.updated_at,
        )

    with SessionFactory() as loser_session:
        with pytest.raises(WebhookEventIdempotencyConflictError) as raised:
            create_idempotent_webhook_event_with_delivery_job(
                loser_session,
                endpoint_id=endpoint_id,
                event_type=loser_event_type,
                payload=loser_payload,
                idempotency_key=idempotency_key,
            )
        assert loser_session.scalar(text("SELECT 1")) == 1
        assert idempotency_key not in str(raised.value)
        assert str(loser_payload) not in str(raised.value)
        assert loser_event_type not in str(raised.value)
        assert _event_count_for_endpoint(loser_session, endpoint_id) == 1
        assert _job_count_for_endpoint(loser_session, endpoint_id) == 1

        stored_event = loser_session.get(WebhookEvent, winner_result.event.id)
        stored_job = _job_for_event(loser_session, winner_result.event.id)
        assert stored_event is not None
        assert stored_job is not None
        assert (
            stored_event.event_type,
            stored_event.payload,
            stored_event.created_at,
            stored_job.id,
            stored_job.status,
            stored_job.next_attempt_at,
            stored_job.created_at,
            stored_job.updated_at,
        ) == original_snapshot


def test_same_key_is_scoped_to_endpoint(created_records: _CreatedRecords) -> None:
    first_endpoint_id = _create_endpoint(created_records, label="scope-first")
    second_endpoint_id = _create_endpoint(created_records, label="scope-second")
    shared_key = f"scope-key-{uuid.uuid4()}"
    payload: dict[str, JsonValue] = {"scope": "endpoint"}

    with SessionFactory() as session:
        first_result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=first_endpoint_id,
            event_type="scope.event",
            payload=payload,
            idempotency_key=shared_key,
        )
        second_result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=second_endpoint_id,
            event_type="scope.event",
            payload=payload,
            idempotency_key=shared_key,
        )
        session.commit()
        created_records.event_ids.update((first_result.event.id, second_result.event.id))

        assert first_result.created is True
        assert second_result.created is True
        assert first_result.event.id != second_result.event.id
        assert _job_for_event(session, first_result.event.id) is not None
        assert _job_for_event(session, second_result.event.id) is not None


def test_idempotency_key_is_case_sensitive(created_records: _CreatedRecords) -> None:
    endpoint_id = _create_endpoint(created_records, label="case-sensitive")
    payload: dict[str, JsonValue] = {"case": True}

    with SessionFactory() as session:
        upper_result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type="case.event",
            payload=payload,
            idempotency_key="Order-123",
        )
        lower_result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=endpoint_id,
            event_type="case.event",
            payload=payload,
            idempotency_key="order-123",
        )
        session.commit()
        created_records.event_ids.update((upper_result.event.id, lower_result.event.id))

        assert upper_result.created is True
        assert lower_result.created is True
        assert upper_result.event.id != lower_result.event.id
        assert _event_count_for_endpoint(session, endpoint_id) == 2
        assert _job_count_for_endpoint(session, endpoint_id) == 2


def test_caller_rollback_removes_keyed_event_and_job(
    created_records: _CreatedRecords,
) -> None:
    endpoint_id = _create_endpoint(created_records, label="caller-rollback")
    caller_session = SessionFactory()
    try:
        result = create_idempotent_webhook_event_with_delivery_job(
            caller_session,
            endpoint_id=endpoint_id,
            event_type="transaction.rollback",
            payload={"rollback": True},
            idempotency_key=f"rollback-{uuid.uuid4()}",
        )
        created_records.event_ids.add(result.event.id)
        job = _job_for_event(caller_session, result.event.id)
        assert job is not None
        job_id = job.id

        with SessionFactory() as observer:
            assert observer.get(WebhookEvent, result.event.id) is None
            assert observer.get(WebhookDeliveryJob, job_id) is None

        caller_session.rollback()

        with SessionFactory() as observer:
            assert observer.get(WebhookEvent, result.event.id) is None
            assert observer.get(WebhookDeliveryJob, job_id) is None
    finally:
        if caller_session.in_transaction():
            caller_session.rollback()
        caller_session.close()


def test_caller_commit_persists_keyed_event_and_job_together(
    created_records: _CreatedRecords,
) -> None:
    endpoint_id = _create_endpoint(created_records, label="caller-commit")
    caller_session = SessionFactory()
    try:
        result = create_idempotent_webhook_event_with_delivery_job(
            caller_session,
            endpoint_id=endpoint_id,
            event_type="transaction.commit",
            payload={"commit": True},
            idempotency_key=f"commit-{uuid.uuid4()}",
        )
        created_records.event_ids.add(result.event.id)
        job = _job_for_event(caller_session, result.event.id)
        assert job is not None
        job_id = job.id

        with SessionFactory() as observer:
            assert observer.get(WebhookEvent, result.event.id) is None
            assert observer.get(WebhookDeliveryJob, job_id) is None

        caller_session.commit()

        with SessionFactory() as observer:
            assert observer.get(WebhookEvent, result.event.id) is not None
            assert observer.get(WebhookDeliveryJob, job_id) is not None
    finally:
        if caller_session.in_transaction():
            caller_session.rollback()
        caller_session.close()


def test_concurrent_equivalent_loser_reuses_winner(
    created_records: _CreatedRecords,
) -> None:
    payload: dict[str, JsonValue] = {
        "active": True,
        "order_id": "race-order",
    }
    winner_result, loser_outcome, lock_was_observed = _execute_concurrent_race(
        created_records,
        loser_event_type="race.event",
        loser_payload=payload,
    )

    assert lock_was_observed is True
    assert winner_result.created is True
    assert loser_outcome.error is None
    assert loser_outcome.created is False
    assert loser_outcome.event_id == winner_result.event.id
    assert loser_outcome.session_usable is True

    with SessionFactory() as session:
        assert _event_count_for_endpoint(session, winner_result.event.endpoint_id) == 1
        assert _job_count_for_endpoint(session, winner_result.event.endpoint_id) == 1


def test_concurrent_conflicting_loser_gets_domain_error(
    created_records: _CreatedRecords,
) -> None:
    conflicting_payload: dict[str, JsonValue] = {
        "active": False,
        "order_id": "race-order",
    }
    winner_result, loser_outcome, lock_was_observed = _execute_concurrent_race(
        created_records,
        loser_event_type="race.event",
        loser_payload=conflicting_payload,
    )

    assert lock_was_observed is True
    assert winner_result.created is True
    assert loser_outcome.event_id is None
    assert loser_outcome.created is None
    assert isinstance(loser_outcome.error, WebhookEventIdempotencyConflictError)
    assert str(loser_outcome.error) == ("Idempotency key conflicts with an existing webhook event")
    assert str(conflicting_payload) not in str(loser_outcome.error)
    assert loser_outcome.session_usable is True

    with SessionFactory() as session:
        assert _event_count_for_endpoint(session, winner_result.event.endpoint_id) == 1
        assert _job_count_for_endpoint(session, winner_result.event.endpoint_id) == 1
