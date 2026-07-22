import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.main import app
from reliable_webhook_service.models import JsonValue, WebhookEndpoint, WebhookEvent


def test_create_webhook_event() -> None:
    marker = uuid.uuid4()
    endpoint_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    payload: dict[str, JsonValue] = {
        "order": {
            "id": str(marker),
            "amount": 149.99,
            "paid": True,
        },
        "tags": ["api", "nested-json"],
        "metadata": {
            "attempt": 1,
            "optional": None,
        },
        "items": [
            {
                "sku": "SKU-1",
                "quantity": 2,
            },
            {
                "sku": "SKU-2",
                "quantity": 1,
            },
        ],
    }

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name=f"Webhook event API endpoint {marker}",
                target_url=f"https://example.com/webhook-event-api/{marker}",
                is_active=False,
            )
            session.add(endpoint)
            session.commit()
            session.refresh(endpoint)
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)
            assert endpoint.is_active is False

        with TestClient(app) as client:
            response = client.post(
                "/webhook-events",
                json={
                    "endpoint_id": str(endpoint_id),
                    "event_type": "  order.created  ",
                    "payload": payload,
                },
            )

        assert response.status_code == 201

        response_body = response.json()
        assert set(response_body) == {
            "id",
            "endpoint_id",
            "event_type",
            "payload",
            "created_at",
        }

        event_id = uuid.UUID(response_body["id"])
        assert response_body["endpoint_id"] == str(endpoint_id)
        assert response_body["event_type"] == "order.created"
        assert response_body["payload"] == payload
        assert response_body["payload"]["order"] == payload["order"]
        assert response_body["payload"]["tags"] == payload["tags"]
        assert response_body["payload"]["metadata"] == payload["metadata"]
        assert response_body["payload"]["items"] == payload["items"]
        assert isinstance(response_body["payload"]["metadata"]["attempt"], int)
        assert isinstance(response_body["payload"]["order"]["amount"], float)
        assert isinstance(response_body["payload"]["order"]["paid"], bool)
        assert response_body["payload"]["metadata"]["optional"] is None

        created_at = datetime.fromisoformat(response_body["created_at"].replace("Z", "+00:00"))
        assert created_at.tzinfo is not None
        assert created_at.utcoffset() is not None

        with SessionFactory() as session:
            stored_event = session.get(WebhookEvent, event_id)
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)

            assert stored_event is not None
            assert stored_event.id == event_id
            assert stored_event.endpoint_id == endpoint_id
            assert stored_event.event_type == "order.created"
            assert stored_event.payload == payload
            assert isinstance(stored_event.created_at, datetime)
            assert stored_event.created_at.tzinfo is not None
            assert stored_event.created_at.utcoffset() is not None

            assert stored_endpoint is not None
            assert stored_endpoint.is_active is False
    finally:
        with SessionFactory() as session:
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
        assert session.get(WebhookEvent, event_id) is None
        assert session.get(WebhookEndpoint, endpoint_id) is None


@pytest.mark.parametrize(
    ("request_body", "expected_field"),
    [
        (
            {
                "endpoint_id": "not-a-uuid",
                "event_type": "order.created",
                "payload": {},
            },
            "endpoint_id",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "",
                "payload": {},
            },
            "event_type",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "   ",
                "payload": {},
            },
            "event_type",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "x" * 256,
                "payload": {},
            },
            "event_type",
        ),
        (
            {
                "event_type": "order.created",
                "payload": {},
            },
            "endpoint_id",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "payload": {},
            },
            "event_type",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": [],
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": "invalid",
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": 42,
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": 3.14,
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": True,
            },
            "payload",
        ),
        (
            {
                "endpoint_id": str(uuid.uuid4()),
                "event_type": "order.created",
                "payload": None,
            },
            "payload",
        ),
    ],
    ids=[
        "malformed-endpoint-id",
        "empty-event-type",
        "whitespace-event-type",
        "long-event-type",
        "missing-endpoint-id",
        "missing-event-type",
        "missing-payload",
        "top-level-payload-list",
        "top-level-payload-string",
        "top-level-payload-int",
        "top-level-payload-float",
        "top-level-payload-bool",
        "top-level-payload-null",
    ],
)
def test_reject_invalid_webhook_event_request(
    request_body: dict[str, JsonValue],
    expected_field: str,
) -> None:
    with SessionFactory() as session:
        event_ids_before = set(session.scalars(select(WebhookEvent.id)).all())

    with TestClient(app) as client:
        response = client.post("/webhook-events", json=request_body)

    assert response.status_code == 422

    response_body = response.json()
    assert "detail" in response_body
    assert isinstance(response_body["detail"], list)
    assert response_body["detail"]
    assert any(error["loc"][-1] == expected_field for error in response_body["detail"])

    with SessionFactory() as session:
        event_ids_after = set(session.scalars(select(WebhookEvent.id)).all())

    assert event_ids_after == event_ids_before
