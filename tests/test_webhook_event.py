import uuid
from datetime import datetime

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import JsonValue, WebhookEndpoint, WebhookEvent


def test_webhook_event_persistence() -> None:
    endpoint_id: uuid.UUID | None = None
    event_ids: list[uuid.UUID] = []
    simple_payload: dict[str, JsonValue] = {
        "order_id": "order-123",
        "amount": 149.99,
        "paid": True,
    }
    nested_payload: dict[str, JsonValue] = {
        "customer": {
            "id": "customer-456",
            "name": "Ada Lovelace",
        },
        "tags": ["premium", "newsletter"],
        "metadata": {
            "active": True,
            "score": 42,
            "previous_value": None,
        },
        "addresses": [
            {
                "city": "Warsaw",
                "primary": True,
            },
            {
                "city": "London",
                "primary": False,
            },
        ],
    }

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name="Webhook event persistence endpoint",
                target_url="https://example.com/webhook-event-persistence",
            )
            session.add(endpoint)
            session.commit()
            session.refresh(endpoint)
            endpoint_id = endpoint.id

            assert isinstance(endpoint_id, uuid.UUID)

            simple_event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="order.created",
                payload=simple_payload,
            )
            nested_event = WebhookEvent(
                endpoint_id=endpoint_id,
                event_type="customer.updated",
                payload=nested_payload,
            )
            session.add_all([simple_event, nested_event])
            session.commit()
            session.refresh(simple_event)
            session.refresh(nested_event)
            event_ids.extend([simple_event.id, nested_event.id])

            assert isinstance(simple_event.id, uuid.UUID)
            assert isinstance(nested_event.id, uuid.UUID)
            assert simple_event.id != nested_event.id
            assert simple_event.endpoint_id == endpoint_id
            assert nested_event.endpoint_id == endpoint_id
            assert isinstance(simple_event.created_at, datetime)
            assert simple_event.created_at.tzinfo is not None
            assert simple_event.created_at.utcoffset() is not None
            assert isinstance(nested_event.created_at, datetime)
            assert nested_event.created_at.tzinfo is not None
            assert nested_event.created_at.utcoffset() is not None

        assert len(event_ids) == 2
        with SessionFactory() as session:
            stored_simple_event = session.get(WebhookEvent, event_ids[0])
            stored_nested_event = session.get(WebhookEvent, event_ids[1])

            assert stored_simple_event is not None
            assert stored_simple_event.id == event_ids[0]
            assert stored_simple_event.endpoint_id == endpoint_id
            assert stored_simple_event.event_type == "order.created"
            assert stored_simple_event.payload == simple_payload
            assert isinstance(stored_simple_event.created_at, datetime)
            assert stored_simple_event.created_at.tzinfo is not None
            assert stored_simple_event.created_at.utcoffset() is not None

            assert stored_nested_event is not None
            assert stored_nested_event.id == event_ids[1]
            assert stored_nested_event.endpoint_id == endpoint_id
            assert stored_nested_event.event_type == "customer.updated"
            assert stored_nested_event.payload == nested_payload
            assert stored_nested_event.payload["customer"] == nested_payload["customer"]
            assert stored_nested_event.payload["tags"] == nested_payload["tags"]
            assert stored_nested_event.payload["metadata"] == nested_payload["metadata"]
            assert stored_nested_event.payload["addresses"] == nested_payload["addresses"]
            assert isinstance(stored_nested_event.created_at, datetime)
            assert stored_nested_event.created_at.tzinfo is not None
            assert stored_nested_event.created_at.utcoffset() is not None
    finally:
        with SessionFactory() as session:
            for event_id in event_ids:
                stored_event = session.get(WebhookEvent, event_id)
                if stored_event is not None:
                    session.delete(stored_event)
            session.commit()

            if endpoint_id is not None:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
            session.commit()

    assert endpoint_id is not None
    assert len(event_ids) == 2
    with SessionFactory() as session:
        assert all(session.get(WebhookEvent, event_id) is None for event_id in event_ids)
        assert session.get(WebhookEndpoint, endpoint_id) is None
