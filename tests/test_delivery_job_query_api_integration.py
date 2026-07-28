import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from reliable_webhook_service import main
from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.dependencies import (
    get_settings,
    get_webhook_http_client,
)
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)


class _FakeRawHttpClient:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass


@dataclass(slots=True)
class _DatabaseScope:
    initial_counts: tuple[int, int, int, int]
    endpoint_ids: list[uuid.UUID] = field(default_factory=list)
    event_ids: list[uuid.UUID] = field(default_factory=list)
    job_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PersistedJob:
    endpoint_id: uuid.UUID
    event_id: uuid.UUID
    job_id: uuid.UUID
    status: str
    attempt_count: int
    next_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _table_counts(session: Session) -> tuple[int, int, int, int]:
    values = tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (
            WebhookEndpoint,
            WebhookEvent,
            WebhookDeliveryJob,
            WebhookDeliveryAttempt,
        )
    )
    assert all(value is not None for value in values)
    return tuple(int(value) for value in values)


@pytest.fixture
def database_scope() -> Iterator[_DatabaseScope]:
    with SessionFactory() as session:
        scope = _DatabaseScope(initial_counts=_table_counts(session))

    try:
        yield scope
    finally:
        with SessionFactory() as session:
            session.rollback()
            for job_id in scope.job_ids:
                job = session.get(WebhookDeliveryJob, job_id)
                if job is not None:
                    session.delete(job)
            session.commit()
            for event_id in scope.event_ids:
                event = session.get(WebhookEvent, event_id)
                if event is not None:
                    session.delete(event)
            session.commit()
            for endpoint_id in scope.endpoint_ids:
                endpoint = session.get(WebhookEndpoint, endpoint_id)
                if endpoint is not None:
                    session.delete(endpoint)
            session.commit()

        with SessionFactory() as session:
            assert _table_counts(session) == scope.initial_counts
            idle_transactions = session.scalar(
                text(
                    """
                    SELECT COUNT(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND state = 'idle in transaction'
                    """
                )
            )
            assert idle_transactions == 0


@pytest.fixture
def application(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(main.httpx2, "Client", _FakeRawHttpClient)
    application = main.create_app()

    def fail_dependency() -> None:
        raise AssertionError("Delivery job GET must not resolve HTTP or settings dependencies")

    application.dependency_overrides[get_webhook_http_client] = fail_dependency
    application.dependency_overrides[get_settings] = fail_dependency
    yield application
    application.dependency_overrides.clear()


def _persist_event(
    scope: _DatabaseScope,
    *,
    label: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    scope.endpoint_ids.append(endpoint_id)
    scope.event_ids.append(event_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Delivery job API {label} {endpoint_id}",
                target_url=f"https://unreachable.example.test/{endpoint_id}",
                is_active=True,
            )
        )
        session.flush()
        session.add(
            WebhookEvent(
                id=event_id,
                endpoint_id=endpoint_id,
                event_type=f"job.api.{label}",
                payload={"secret": "must-not-be-returned", "label": label},
                idempotency_key=f"private-{event_id}",
            )
        )
        session.commit()
    return endpoint_id, event_id


def _persist_job(
    scope: _DatabaseScope,
    *,
    label: str,
    status: str,
    updated_at: datetime,
    identifier: int,
) -> _PersistedJob:
    endpoint_id, event_id = _persist_event(scope, label=label)
    job_id = uuid.UUID(int=identifier)
    attempt_count = identifier
    next_attempt_at = updated_at if status in {"pending", "processing"} else None
    created_at = updated_at - timedelta(hours=1)
    scope.job_ids.append(job_id)
    with SessionFactory() as session:
        session.add(
            WebhookDeliveryJob(
                id=job_id,
                event_id=event_id,
                status=status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
        session.commit()
    return _PersistedJob(
        endpoint_id=endpoint_id,
        event_id=event_id,
        job_id=job_id,
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=next_attempt_at,
        created_at=created_at,
        updated_at=updated_at,
    )


def _expected_body(record: _PersistedJob) -> dict[str, object]:
    return {
        "id": str(record.job_id),
        "event_id": str(record.event_id),
        "status": record.status,
        "attempt_count": record.attempt_count,
        "next_attempt_at": (
            record.next_attempt_at.isoformat().replace("+00:00", "Z")
            if record.next_attempt_at is not None
            else None
        ),
        "created_at": record.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": record.updated_at.isoformat().replace("+00:00", "Z"),
    }


def _job_state(job_id: uuid.UUID) -> tuple[object, ...]:
    with SessionFactory() as session:
        job = session.get(WebhookDeliveryJob, job_id)
        assert job is not None
        return (
            job.status,
            job.attempt_count,
            job.next_attempt_at,
            job.created_at,
            job.updated_at,
        )


@pytest.mark.parametrize(
    ("job_status", "identifier"),
    [
        ("pending", 101),
        ("processing", 102),
        ("succeeded", 103),
        ("dead_letter", 104),
    ],
)
def test_event_scoped_api_returns_each_status_without_side_effects(
    database_scope: _DatabaseScope,
    application: FastAPI,
    job_status: str,
    identifier: int,
) -> None:
    record = _persist_job(
        database_scope,
        label=job_status,
        status=job_status,
        updated_at=datetime(2099, 5, 1, 12, identifier % 60, tzinfo=UTC),
        identifier=identifier,
    )
    state_before = _job_state(record.job_id)
    with SessionFactory() as session:
        counts_before = _table_counts(session)

    with TestClient(application) as client:
        response = client.get(f"/webhook-events/{record.event_id}/delivery-job")

    assert response.status_code == 200
    assert response.json() == _expected_body(record)
    assert set(response.json()) == {
        "id",
        "event_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "created_at",
        "updated_at",
    }
    assert _job_state(record.job_id) == state_before
    with SessionFactory() as session:
        assert _table_counts(session) == counts_before


def test_event_scoped_api_errors_are_precise(
    database_scope: _DatabaseScope,
    application: FastAPI,
) -> None:
    _, event_id = _persist_event(database_scope, label="missing-job")

    with TestClient(application) as client:
        missing_event = client.get(f"/webhook-events/{uuid.uuid4()}/delivery-job")
        missing_job = client.get(f"/webhook-events/{event_id}/delivery-job")
        invalid_uuid = client.get("/webhook-events/not-a-uuid/delivery-job")

    assert missing_event.status_code == 404
    assert missing_event.json() == {"detail": "Webhook event not found"}
    assert missing_job.status_code == 409
    assert missing_job.json() == {"detail": "Webhook delivery job not found"}
    assert invalid_uuid.status_code == 422


def test_collection_api_filters_orders_and_paginates_without_mutation(
    database_scope: _DatabaseScope,
    application: FastAPI,
) -> None:
    base = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
    records = [
        _persist_job(
            database_scope,
            label=f"collection-{identifier}",
            status=job_status,
            updated_at=updated_at,
            identifier=identifier,
        )
        for identifier, job_status, updated_at in (
            (201, "pending", base),
            (202, "processing", base + timedelta(minutes=1)),
            (203, "succeeded", base + timedelta(minutes=2)),
            (204, "dead_letter", base + timedelta(minutes=3)),
            (205, "dead_letter", base + timedelta(minutes=3)),
            (206, "dead_letter", base + timedelta(minutes=4)),
        )
    ]
    states_before = {record.job_id: _job_state(record.job_id) for record in records}

    with TestClient(application) as client:
        first = client.get("/webhook-delivery-jobs?status=dead_letter&limit=2")
        assert first.status_code == 200
        first_body = first.json()
        assert [item["id"] for item in first_body["items"]] == [
            str(uuid.UUID(int=206)),
            str(uuid.UUID(int=205)),
        ]
        assert first_body["next_cursor"] is not None

        second = client.get(
            "/webhook-delivery-jobs",
            params={
                "status": "dead_letter",
                "limit": 2,
                "cursor": first_body["next_cursor"],
            },
        )
        assert second.status_code == 200
        second_body = second.json()
        assert second_body == {
            "items": [_expected_body(records[3])],
            "next_cursor": None,
        }

        processing = client.get("/webhook-delivery-jobs?status=processing")
        assert processing.status_code == 200
        processing_items = [
            item for item in processing.json()["items"] if item["id"] == str(uuid.UUID(int=202))
        ]
        assert processing_items == [_expected_body(records[1])]

        default_page = client.get("/webhook-delivery-jobs")
        assert default_page.status_code == 200
        returned_test_ids = [
            uuid.UUID(item["id"])
            for item in default_page.json()["items"]
            if uuid.UUID(item["id"]) in states_before
        ]
        assert returned_test_ids == [
            uuid.UUID(int=206),
            uuid.UUID(int=205),
            uuid.UUID(int=204),
            uuid.UUID(int=203),
            uuid.UUID(int=202),
            uuid.UUID(int=201),
        ]

    assert {record.job_id: _job_state(record.job_id) for record in records} == states_before


def test_collection_api_rejects_invalid_queries_and_cursor_filter_mismatch(
    database_scope: _DatabaseScope,
    application: FastAPI,
) -> None:
    base = datetime(2099, 7, 1, 12, 0, tzinfo=UTC)
    for identifier in (301, 302):
        _persist_job(
            database_scope,
            label=f"cursor-{identifier}",
            status="dead_letter",
            updated_at=base + timedelta(minutes=identifier),
            identifier=identifier,
        )

    with TestClient(application) as client:
        first = client.get("/webhook-delivery-jobs?status=dead_letter&limit=1")
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        mismatch = client.get(
            "/webhook-delivery-jobs",
            params={"status": "succeeded", "cursor": cursor},
        )
        malformed = client.get("/webhook-delivery-jobs?cursor=not-valid-base64*")
        invalid_status = client.get("/webhook-delivery-jobs?status=DEAD_LETTER")
        invalid_low = client.get("/webhook-delivery-jobs?limit=0")
        invalid_high = client.get("/webhook-delivery-jobs?limit=101")

    assert mismatch.status_code == 422
    assert mismatch.json() == {"detail": "Invalid webhook delivery job cursor"}
    assert malformed.status_code == 422
    assert malformed.json() == {"detail": "Invalid webhook delivery job cursor"}
    assert invalid_status.status_code == 422
    assert invalid_low.status_code == 422
    assert invalid_high.status_code == 422


def test_get_snapshot_does_not_lock_or_prevent_replay_race(
    database_scope: _DatabaseScope,
    application: FastAPI,
) -> None:
    record = _persist_job(
        database_scope,
        label="race",
        status="dead_letter",
        updated_at=datetime(2099, 8, 1, 12, 0, tzinfo=UTC),
        identifier=401,
    )

    with TestClient(application) as client:
        historical = client.get(f"/webhook-events/{record.event_id}/delivery-job")
        replay = client.post(f"/webhook-events/{record.event_id}/replay")
        current = client.get(f"/webhook-events/{record.event_id}/delivery-job")
        second_replay = client.post(f"/webhook-events/{record.event_id}/replay")

    assert historical.status_code == 200
    assert historical.json()["status"] == "dead_letter"
    assert replay.status_code == 202
    assert current.status_code == 200
    assert current.json()["status"] == "pending"
    assert current.json()["attempt_count"] == 0
    assert second_replay.status_code == 409
    assert second_replay.json() == {"detail": "Webhook delivery job is not replayable"}
