import uuid
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

import reliable_webhook_service.delivery_job_recovery_service as recovery_service
from reliable_webhook_service.delivery_job_recovery_service import (
    WebhookDeliveryJobRecoveryResult,
    recover_stale_webhook_delivery_jobs,
)
from reliable_webhook_service.models import WebhookDeliveryJob

STALE_BEFORE = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
RECOVERED_AT = datetime(2026, 7, 28, 11, 0, tzinfo=UTC)


class _FakeScalarResult:
    def __init__(self, jobs: list[WebhookDeliveryJob]) -> None:
        self.jobs = jobs

    def all(self) -> list[WebhookDeliveryJob]:
        return self.jobs


class _FakeSession:
    def __init__(
        self,
        jobs: list[WebhookDeliveryJob] | None = None,
        *,
        selection_error: Exception | None = None,
        flush_error: Exception | None = None,
    ) -> None:
        self.jobs = [] if jobs is None else jobs
        self.selection_error = selection_error
        self.flush_error = flush_error
        self.statements: list[Select[tuple[WebhookDeliveryJob]]] = []
        self.flush_count = 0
        self.add_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def scalars(
        self,
        statement: Select[tuple[WebhookDeliveryJob]],
    ) -> _FakeScalarResult:
        self.statements.append(statement)
        if self.selection_error is not None:
            raise self.selection_error
        return _FakeScalarResult(self.jobs)

    def flush(self) -> None:
        self.flush_count += 1
        if self.flush_error is not None:
            raise self.flush_error

    def add(self, instance: object) -> None:
        self.add_count += 1

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.close_count += 1


def _job(
    *,
    job_id: uuid.UUID | None = None,
    status: str = "processing",
    next_attempt_at: datetime = STALE_BEFORE,
    created_at: datetime = STALE_BEFORE - timedelta(hours=2),
    updated_at: datetime = STALE_BEFORE - timedelta(hours=1),
) -> WebhookDeliveryJob:
    return WebhookDeliveryJob(
        id=uuid.uuid4() if job_id is None else job_id,
        event_id=uuid.uuid4(),
        status=status,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _recover(
    session: _FakeSession,
    *,
    stale_before: datetime = STALE_BEFORE,
    recovered_at: datetime = RECOVERED_AT,
    limit: int = 10,
) -> WebhookDeliveryJobRecoveryResult:
    return recover_stale_webhook_delivery_jobs(
        cast(Session, session),
        stale_before=stale_before,
        recovered_at=recovered_at,
        limit=limit,
    )


def _assert_no_session_lifecycle(session: _FakeSession) -> None:
    assert session.commit_count == 0
    assert session.rollback_count == 0
    assert session.close_count == 0


@pytest.mark.parametrize(
    ("stale_before", "recovered_at", "limit", "expected_argument"),
    [
        (STALE_BEFORE, RECOVERED_AT, True, "limit"),
        (STALE_BEFORE, RECOVERED_AT, 0, "limit"),
        (STALE_BEFORE, RECOVERED_AT, -1, "limit"),
        (STALE_BEFORE, RECOVERED_AT, 1.5, "limit"),
        (datetime(2026, 7, 28, 10, 0), RECOVERED_AT, 1, "stale_before"),
        ("not-a-datetime", RECOVERED_AT, 1, "stale_before"),
        (STALE_BEFORE, datetime(2026, 7, 28, 11, 0), 1, "recovered_at"),
        (STALE_BEFORE, "not-a-datetime", 1, "recovered_at"),
        (RECOVERED_AT, STALE_BEFORE, 1, "recovered_at"),
    ],
)
def test_recovery_validates_arguments_before_query(
    stale_before: object,
    recovered_at: object,
    limit: object,
    expected_argument: str,
) -> None:
    job = _job()
    original_values = (
        job.status,
        job.next_attempt_at,
        job.created_at,
        job.updated_at,
        job.event_id,
    )
    session = _FakeSession([job])

    with pytest.raises(ValueError, match=expected_argument):
        _recover(
            session,
            stale_before=cast(datetime, stale_before),
            recovered_at=cast(datetime, recovered_at),
            limit=cast(int, limit),
        )

    assert session.statements == []
    assert session.flush_count == 0
    assert session.add_count == 0
    _assert_no_session_lifecycle(session)
    assert (
        job.status,
        job.next_attempt_at,
        job.created_at,
        job.updated_at,
        job.event_id,
    ) == original_values


def test_recovery_returns_empty_result_without_flush_or_session_lifecycle() -> None:
    session = _FakeSession()

    result = _recover(session)

    assert len(session.statements) == 1
    assert result.recovered_job_ids == ()
    assert result.recovered_count == 0
    assert session.flush_count == 0
    assert session.add_count == 0
    _assert_no_session_lifecycle(session)


def test_recovery_builds_bounded_locked_deterministic_single_query() -> None:
    requested_limit = 7
    session = _FakeSession()

    _recover(session, limit=requested_limit)

    assert len(session.statements) == 1
    statement = session.statements[0]
    assert statement.column_descriptions[0]["entity"] is WebhookDeliveryJob
    assert statement._limit_clause is not None
    assert statement._limit_clause.value == requested_limit
    assert statement._for_update_arg is not None
    assert statement._for_update_arg.skip_locked is True
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = " ".join(str(compiled).split())
    assert "webhook_delivery_jobs.status = 'processing'" in sql
    assert "webhook_delivery_jobs.updated_at <=" in sql
    assert (
        "ORDER BY webhook_delivery_jobs.updated_at, webhook_delivery_jobs.created_at, "
        "webhook_delivery_jobs.id"
    ) in sql
    assert "LIMIT 7" in sql
    assert sql.endswith("FOR UPDATE SKIP LOCKED")


def test_recovery_transitions_selected_jobs_and_flushes_once() -> None:
    recovered_at = datetime(
        2026,
        7,
        28,
        14,
        30,
        tzinfo=timezone(timedelta(hours=3)),
    )
    jobs = [_job(), _job()]
    snapshots = [(job.id, job.event_id, job.created_at) for job in jobs]
    session = _FakeSession(jobs)

    result = _recover(session, recovered_at=recovered_at)

    assert result.recovered_job_ids == tuple(job.id for job in jobs)
    for job, snapshot in zip(jobs, snapshots, strict=True):
        assert (job.id, job.event_id, job.created_at) == snapshot
        assert job.status == "pending"
        assert job.next_attempt_at == recovered_at.astimezone(UTC)
        assert job.updated_at == recovered_at.astimezone(UTC)
    assert session.flush_count == 1
    assert session.add_count == 0
    _assert_no_session_lifecycle(session)


def test_recovery_preserves_selected_order_in_immutable_result() -> None:
    job_ids = [uuid.uuid4() for _ in range(3)]
    jobs = [_job(job_id=job_id) for job_id in job_ids]
    session = _FakeSession(jobs)

    result = _recover(session)
    jobs.reverse()
    jobs.append(_job())

    assert result.recovered_job_ids == tuple(job_ids)
    assert isinstance(result.recovered_job_ids, tuple)
    assert all(isinstance(job_id, uuid.UUID) for job_id in result.recovered_job_ids)
    assert result.recovered_count == 3
    assert all(not isinstance(value, WebhookDeliveryJob) for value in result.recovered_job_ids)


def test_recovery_keeps_commit_rollback_and_close_owned_by_caller() -> None:
    session = _FakeSession([_job()])

    result = _recover(session)

    assert result.recovered_count == 1
    assert session.flush_count == 1
    _assert_no_session_lifecycle(session)
    assert session.statements


def test_recovery_propagates_selection_failure_without_session_cleanup() -> None:
    error = RuntimeError("selection failed")
    session = _FakeSession(selection_error=error)

    with pytest.raises(RuntimeError, match="^selection failed$") as error_info:
        _recover(session)

    assert error_info.value is error
    assert len(session.statements) == 1
    assert session.flush_count == 0
    _assert_no_session_lifecycle(session)


def test_recovery_propagates_flush_failure_without_session_cleanup() -> None:
    error = RuntimeError("flush failed")
    job = _job()
    session = _FakeSession([job], flush_error=error)

    with pytest.raises(RuntimeError, match="^flush failed$") as error_info:
        _recover(session)

    assert error_info.value is error
    assert len(session.statements) == 1
    assert session.flush_count == 1
    assert job.status == "pending"
    assert job.next_attempt_at == RECOVERED_AT
    assert job.updated_at == RECOVERED_AT
    _assert_no_session_lifecycle(session)


def test_recovery_result_is_frozen_and_contains_no_orm_objects() -> None:
    job_id = uuid.uuid4()
    result = WebhookDeliveryJobRecoveryResult(recovered_job_ids=(job_id,))

    assert recovery_service.__all__ == [
        "WebhookDeliveryJobRecoveryResult",
        "recover_stale_webhook_delivery_jobs",
    ]
    assert is_dataclass(result)
    assert [field.name for field in fields(result)] == ["recovered_job_ids"]
    assert isinstance(result.recovered_job_ids, tuple)
    assert all(isinstance(value, uuid.UUID) for value in result.recovered_job_ids)
    assert result.recovered_count == 1
    assert isinstance(type(result).recovered_count, property)
    assert type(result).recovered_count.fset is None
    assert not hasattr(result, "__dict__")
    assert all(
        not isinstance(value, (Session, WebhookDeliveryJob)) for value in result.recovered_job_ids
    )
    with pytest.raises(FrozenInstanceError):
        result.recovered_job_ids = ()
