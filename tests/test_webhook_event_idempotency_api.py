import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.main import app
from reliable_webhook_service.models import (
    JsonValue,
    WebhookDeliveryAttempt,
    WebhookDeliveryJob,
    WebhookEndpoint,
    WebhookEvent,
)

RESPONSE_FIELDS = {
    "id",
    "endpoint_id",
    "event_type",
    "payload",
    "created_at",
}
CONFLICT_DETAIL = "Idempotency key conflicts with an existing webhook event"

type TableCounts = tuple[int, int, int, int]
type EventSnapshot = tuple[
    uuid.UUID,
    uuid.UUID,
    str,
    dict[str, JsonValue],
    str | None,
    datetime,
]
type JobSnapshot = tuple[uuid.UUID, uuid.UUID, str, datetime | None, datetime, datetime]


@dataclass
class DatabaseScope:
    initial_counts: TableCounts
    endpoint_ids: set[uuid.UUID] = field(default_factory=set)
    event_ids: set[uuid.UUID] = field(default_factory=set)


def _table_counts() -> TableCounts:
    with SessionFactory() as session:
        counts = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                WebhookEndpoint,
                WebhookEvent,
                WebhookDeliveryJob,
                WebhookDeliveryAttempt,
            )
        )
    assert all(count is not None for count in counts)
    return (
        int(counts[0]),
        int(counts[1]),
        int(counts[2]),
        int(counts[3]),
    )


def _cleanup_scope(scope: DatabaseScope) -> None:
    with SessionFactory() as session:
        discovered_event_ids = set(
            session.scalars(
                select(WebhookEvent.id).where(WebhookEvent.endpoint_id.in_(scope.endpoint_ids))
            ).all()
        )
        event_ids = scope.event_ids | discovered_event_ids

        attempt_ids = set(
            session.scalars(
                select(WebhookDeliveryAttempt.id).where(
                    WebhookDeliveryAttempt.event_id.in_(event_ids)
                )
            ).all()
        )
        for attempt_id in attempt_ids:
            attempt = session.get(WebhookDeliveryAttempt, attempt_id)
            if attempt is not None:
                session.delete(attempt)
        session.commit()

        job_ids = set(
            session.scalars(
                select(WebhookDeliveryJob.id).where(WebhookDeliveryJob.event_id.in_(event_ids))
            ).all()
        )
        for job_id in job_ids:
            job = session.get(WebhookDeliveryJob, job_id)
            if job is not None:
                session.delete(job)
        session.commit()

        for event_id in event_ids:
            event = session.get(WebhookEvent, event_id)
            if event is not None:
                session.delete(event)
        session.commit()

        for endpoint_id in scope.endpoint_ids:
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if endpoint is not None:
                session.delete(endpoint)
        session.commit()


@pytest.fixture
def database_scope() -> Iterator[DatabaseScope]:
    scope = DatabaseScope(initial_counts=_table_counts())
    try:
        yield scope
    finally:
        _cleanup_scope(scope)
        assert _table_counts() == scope.initial_counts
        assert app.dependency_overrides == {}


def _create_endpoint(scope: DatabaseScope, *, is_active: bool = True) -> uuid.UUID:
    marker = uuid.uuid4()
    endpoint_id = uuid.uuid4()
    scope.endpoint_ids.add(endpoint_id)
    with SessionFactory() as session:
        session.add(
            WebhookEndpoint(
                id=endpoint_id,
                name=f"Idempotency API endpoint {marker}",
                target_url=f"https://example.test/idempotency-api/{marker}",
                is_active=is_active,
            )
        )
        session.commit()
    return endpoint_id


def _request_body(
    endpoint_id: uuid.UUID,
    *,
    event_type: str = "order.created",
    payload: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return {
        "endpoint_id": str(endpoint_id),
        "event_type": event_type,
        "payload": payload if payload is not None else {"order_id": str(uuid.uuid4())},
    }


def _record_response_event(scope: DatabaseScope, response_body: dict[str, JsonValue]) -> uuid.UUID:
    event_id_value = response_body["id"]
    assert isinstance(event_id_value, str)
    event_id = uuid.UUID(event_id_value)
    scope.event_ids.add(event_id)
    return event_id


def _event_snapshot(event_id: uuid.UUID) -> EventSnapshot:
    with SessionFactory() as session:
        event = session.get(WebhookEvent, event_id)
        assert event is not None
        return (
            event.id,
            event.endpoint_id,
            event.event_type,
            event.payload,
            event.idempotency_key,
            event.created_at,
        )


def _jobs_for_events(event_ids: set[uuid.UUID]) -> list[WebhookDeliveryJob]:
    with SessionFactory() as session:
        return list(
            session.scalars(
                select(WebhookDeliveryJob)
                .where(WebhookDeliveryJob.event_id.in_(event_ids))
                .order_by(WebhookDeliveryJob.id)
            ).all()
        )


def _job_snapshot(event_id: uuid.UUID) -> JobSnapshot:
    jobs = _jobs_for_events({event_id})
    assert len(jobs) == 1
    job = jobs[0]
    return (
        job.id,
        job.event_id,
        job.status,
        job.next_attempt_at,
        job.created_at,
        job.updated_at,
    )


def _attempt_count(event_ids: set[uuid.UUID]) -> int:
    with SessionFactory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(WebhookDeliveryAttempt)
            .where(WebhookDeliveryAttempt.event_id.in_(event_ids))
        )
    assert count is not None
    return count


def _event_count(endpoint_ids: set[uuid.UUID]) -> int:
    with SessionFactory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(WebhookEvent.endpoint_id.in_(endpoint_ids))
        )
    assert count is not None
    return count


def _assert_response_schema(response_body: dict[str, JsonValue]) -> None:
    assert set(response_body) == RESPONSE_FIELDS
    assert "idempotency_key" not in response_body
    assert "created" not in response_body
    assert "job" not in response_body
    assert "attempt" not in response_body


def test_unkeyed_requests_remain_non_idempotent(database_scope: DatabaseScope) -> None:
    endpoint_id = _create_endpoint(database_scope)
    request_body = _request_body(
        endpoint_id,
        payload={"order": {"id": str(uuid.uuid4()), "paid": True}},
    )

    with TestClient(app) as client:
        first_response = client.post("/webhook-events", json=request_body)
        second_response = client.post("/webhook-events", json=request_body)

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first_body = first_response.json()
    second_body = second_response.json()
    _assert_response_schema(first_body)
    _assert_response_schema(second_body)
    first_event_id = _record_response_event(database_scope, first_body)
    second_event_id = _record_response_event(database_scope, second_body)
    assert first_event_id != second_event_id

    with SessionFactory() as session:
        events = list(
            session.scalars(
                select(WebhookEvent).where(WebhookEvent.endpoint_id == endpoint_id)
            ).all()
        )
    assert {event.id for event in events} == {first_event_id, second_event_id}
    assert all(event.idempotency_key is None for event in events)
    jobs = _jobs_for_events({first_event_id, second_event_id})
    assert len(jobs) == 2
    assert all(job.status == "pending" for job in jobs)
    assert _attempt_count({first_event_id, second_event_id}) == 0


def test_first_keyed_request_creates_event_and_pending_job(
    database_scope: DatabaseScope,
) -> None:
    endpoint_id = _create_endpoint(database_scope, is_active=False)
    payload: dict[str, JsonValue] = {"order_id": str(uuid.uuid4()), "paid": False}

    with TestClient(app) as client:
        response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id, event_type="  order.created  ", payload=payload),
            headers={"Idempotency-Key": "  First-Key  "},
        )

    assert response.status_code == 201
    response_body = response.json()
    _assert_response_schema(response_body)
    event_id = _record_response_event(database_scope, response_body)
    assert response_body["endpoint_id"] == str(endpoint_id)
    assert response_body["event_type"] == "order.created"
    assert response_body["payload"] == payload

    with SessionFactory() as session:
        event = session.get(WebhookEvent, event_id)
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        assert event is not None
        assert endpoint is not None
        assert endpoint.is_active is False
        assert event.idempotency_key == "First-Key"
        assert event.created_at is not None
        event_created_at = event.created_at

    jobs = _jobs_for_events({event_id})
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].next_attempt_at == event_created_at
    assert _attempt_count({event_id}) == 0


def test_equivalent_keyed_request_reuses_event_and_job(
    database_scope: DatabaseScope,
) -> None:
    endpoint_id = _create_endpoint(database_scope)
    idempotency_key = f"equivalent-{uuid.uuid4()}"
    first_payload: dict[str, JsonValue] = {
        "order": {"id": str(uuid.uuid4()), "paid": True},
        "metadata": {"source": "api", "attempt": 1},
    }
    second_payload: dict[str, JsonValue] = {
        "metadata": {"attempt": 1, "source": "api"},
        "order": {"paid": True, "id": first_payload["order"]["id"]},  # type: ignore[index]
    }

    with TestClient(app) as client:
        first_response = client.post(
            "/webhook-events",
            json=_request_body(
                endpoint_id,
                event_type="  order.created  ",
                payload=first_payload,
            ),
            headers={"Idempotency-Key": idempotency_key},
        )
        assert first_response.status_code == 201
        first_body = first_response.json()
        _assert_response_schema(first_body)
        event_id = _record_response_event(database_scope, first_body)
        event_before = _event_snapshot(event_id)
        job_before = _job_snapshot(event_id)
        counts_before = (
            _event_count({endpoint_id}),
            len(_jobs_for_events({event_id})),
        )

        second_response = client.post(
            "/webhook-events",
            json=_request_body(
                endpoint_id,
                event_type="order.created",
                payload=second_payload,
            ),
            headers={"Idempotency-Key": idempotency_key},
        )

    assert second_response.status_code == 200
    second_body = second_response.json()
    _assert_response_schema(second_body)
    assert second_body == first_body
    assert second_body["id"] == str(event_id)
    assert second_body["created_at"] == first_body["created_at"]
    assert _event_snapshot(event_id) == event_before
    assert _job_snapshot(event_id) == job_before
    assert (
        _event_count({endpoint_id}),
        len(_jobs_for_events({event_id})),
    ) == counts_before
    assert _attempt_count({event_id}) == 0


@pytest.mark.parametrize(
    ("first_event_type", "first_payload", "second_event_type", "second_payload"),
    [
        pytest.param(
            "order.created",
            {"value": "same"},
            "order.updated",
            {"value": "same"},
            id="different-event-type",
        ),
        pytest.param(
            "order.created",
            {"value": "first"},
            "order.created",
            {"value": "second"},
            id="different-payload",
        ),
        pytest.param(
            "order.created",
            {"value": True},
            "order.created",
            {"value": 1},
            id="json-boolean-vs-number",
        ),
    ],
)
def test_conflicting_keyed_request_returns_409_without_mutation(
    database_scope: DatabaseScope,
    first_event_type: str,
    first_payload: dict[str, JsonValue],
    second_event_type: str,
    second_payload: dict[str, JsonValue],
) -> None:
    endpoint_id = _create_endpoint(database_scope)
    marker = str(uuid.uuid4())
    idempotency_key = f"conflict-{marker}"

    with TestClient(app) as client:
        first_response = client.post(
            "/webhook-events",
            json=_request_body(
                endpoint_id,
                event_type=first_event_type,
                payload=first_payload,
            ),
            headers={"Idempotency-Key": idempotency_key},
        )
        assert first_response.status_code == 201
        first_body = first_response.json()
        event_id = _record_response_event(database_scope, first_body)
        event_before = _event_snapshot(event_id)
        job_before = _job_snapshot(event_id)

        second_response = client.post(
            "/webhook-events",
            json=_request_body(
                endpoint_id,
                event_type=second_event_type,
                payload=second_payload,
            ),
            headers={"Idempotency-Key": idempotency_key},
        )

    assert second_response.status_code == 409
    assert second_response.json() == {"detail": CONFLICT_DETAIL}
    assert idempotency_key not in second_response.text
    assert marker not in second_response.text
    assert first_event_type not in second_response.text
    assert second_event_type not in second_response.text
    assert _event_count({endpoint_id}) == 1
    assert len(_jobs_for_events({event_id})) == 1
    assert _event_snapshot(event_id) == event_before
    assert _job_snapshot(event_id) == job_before
    assert _attempt_count({event_id}) == 0


def test_idempotency_key_is_scoped_to_endpoint(database_scope: DatabaseScope) -> None:
    first_endpoint_id = _create_endpoint(database_scope)
    second_endpoint_id = _create_endpoint(database_scope)
    idempotency_key = f"scoped-{uuid.uuid4()}"
    payload: dict[str, JsonValue] = {"order_id": str(uuid.uuid4())}

    with TestClient(app) as client:
        first_response = client.post(
            "/webhook-events",
            json=_request_body(first_endpoint_id, payload=payload),
            headers={"Idempotency-Key": idempotency_key},
        )
        second_response = client.post(
            "/webhook-events",
            json=_request_body(second_endpoint_id, payload=payload),
            headers={"Idempotency-Key": idempotency_key},
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first_event_id = _record_response_event(database_scope, first_response.json())
    second_event_id = _record_response_event(database_scope, second_response.json())
    assert first_event_id != second_event_id

    with SessionFactory() as session:
        first_event = session.get(WebhookEvent, first_event_id)
        second_event = session.get(WebhookEvent, second_event_id)
        assert first_event is not None
        assert second_event is not None
        assert first_event.endpoint_id == first_endpoint_id
        assert second_event.endpoint_id == second_endpoint_id
        assert first_event.idempotency_key == idempotency_key
        assert second_event.idempotency_key == idempotency_key

    jobs = _jobs_for_events({first_event_id, second_event_id})
    assert len(jobs) == 2
    assert {job.event_id for job in jobs} == {first_event_id, second_event_id}
    assert all(job.status == "pending" for job in jobs)
    assert _attempt_count({first_event_id, second_event_id}) == 0


def test_idempotency_key_value_is_case_sensitive(database_scope: DatabaseScope) -> None:
    endpoint_id = _create_endpoint(database_scope)
    payload: dict[str, JsonValue] = {"order_id": str(uuid.uuid4())}

    with TestClient(app) as client:
        upper_response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id, payload=payload),
            headers={"Idempotency-Key": "Order-123"},
        )
        lower_response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id, payload=payload),
            headers={"Idempotency-Key": "order-123"},
        )

    assert upper_response.status_code == 201
    assert lower_response.status_code == 201
    upper_id = _record_response_event(database_scope, upper_response.json())
    lower_id = _record_response_event(database_scope, lower_response.json())
    assert upper_id != lower_id
    assert _event_snapshot(upper_id)[4] == "Order-123"
    assert _event_snapshot(lower_id)[4] == "order-123"
    assert len(_jobs_for_events({upper_id, lower_id})) == 2
    assert _attempt_count({upper_id, lower_id}) == 0


@pytest.mark.parametrize(
    ("idempotency_key", "expected_detail"),
    [
        pytest.param("", "Idempotency key must not be empty", id="empty"),
        pytest.param("   ", "Idempotency key must not be empty", id="whitespace-only"),
        pytest.param(
            "x" * 256,
            "Idempotency key must not exceed 255 characters",
            id="too-long",
        ),
    ],
)
def test_invalid_idempotency_key_returns_422_without_records(
    database_scope: DatabaseScope,
    idempotency_key: str,
    expected_detail: str,
) -> None:
    endpoint_id = _create_endpoint(database_scope)
    counts_before = _table_counts()

    with TestClient(app) as client:
        response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id),
            headers={"Idempotency-Key": idempotency_key},
        )

    assert response.status_code == 422
    assert response.json() == {"detail": expected_detail}
    if idempotency_key.strip():
        assert idempotency_key not in response.text
    assert _table_counts() == counts_before
    assert _event_count({endpoint_id}) == 0


def test_255_character_idempotency_key_is_accepted(database_scope: DatabaseScope) -> None:
    endpoint_id = _create_endpoint(database_scope)
    idempotency_key = "x" * 255

    with TestClient(app) as client:
        response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id),
            headers={"Idempotency-Key": idempotency_key},
        )

    assert response.status_code == 201
    response_body = response.json()
    _assert_response_schema(response_body)
    event_id = _record_response_event(database_scope, response_body)
    assert _event_snapshot(event_id)[4] == idempotency_key
    assert len(_jobs_for_events({event_id})) == 1
    assert _attempt_count({event_id}) == 0


def test_missing_endpoint_preserves_key_validation_order(
    database_scope: DatabaseScope,
) -> None:
    missing_endpoint_id = uuid.uuid4()
    counts_before = _table_counts()

    with TestClient(app) as client:
        missing_response = client.post(
            "/webhook-events",
            json=_request_body(missing_endpoint_id),
            headers={"Idempotency-Key": f"missing-{uuid.uuid4()}"},
        )
        invalid_response = client.post(
            "/webhook-events",
            json=_request_body(missing_endpoint_id),
            headers={"Idempotency-Key": "   "},
        )

    assert missing_response.status_code == 404
    assert missing_response.json() == {"detail": "Webhook endpoint not found"}
    assert invalid_response.status_code == 422
    assert invalid_response.json() == {"detail": "Idempotency key must not be empty"}
    assert _table_counts() == counts_before


def test_lowercase_header_name_is_recognized(database_scope: DatabaseScope) -> None:
    endpoint_id = _create_endpoint(database_scope)
    idempotency_key = "Case-Preserved"

    with TestClient(app) as client:
        response = client.post(
            "/webhook-events",
            json=_request_body(endpoint_id),
            headers={"idempotency-key": idempotency_key},
        )

    assert response.status_code == 201
    event_id = _record_response_event(database_scope, response.json())
    assert _event_snapshot(event_id)[4] == idempotency_key
    assert len(_jobs_for_events({event_id})) == 1
    assert _attempt_count({event_id}) == 0


def test_openapi_documents_idempotent_event_creation_contract() -> None:
    operation = app.openapi()["paths"]["/webhook-events"]["post"]
    header_parameters = [
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    ]

    assert len(header_parameters) == 1
    header_parameter = header_parameters[0]
    assert header_parameter["required"] is False
    header_schema = header_parameter["schema"]
    assert {schema.get("type") for schema in header_schema["anyOf"]} == {"string", "null"}

    responses = operation["responses"]
    assert {"200", "201", "409", "422"} <= set(responses)
    for response_code in ("200", "201"):
        assert (
            responses[response_code]["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/WebhookEventResponse"
        )
    assert (
        operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/WebhookEventCreate"
    )
