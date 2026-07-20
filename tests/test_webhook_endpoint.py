import uuid
from datetime import datetime

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.models import WebhookEndpoint


def test_webhook_endpoint_persistence() -> None:
    endpoint_id: uuid.UUID | None = None

    try:
        with SessionFactory() as session:
            endpoint = WebhookEndpoint(
                name="Primary webhook endpoint",
                target_url="https://example.com/webhooks",
            )
            session.add(endpoint)
            session.commit()
            session.refresh(endpoint)
            endpoint_id = endpoint.id

        with SessionFactory() as session:
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)

            assert stored_endpoint is not None
            assert isinstance(stored_endpoint.id, uuid.UUID)
            assert stored_endpoint.id == endpoint_id
            assert stored_endpoint.name == "Primary webhook endpoint"
            assert stored_endpoint.target_url == "https://example.com/webhooks"
            assert stored_endpoint.is_active is True
            assert isinstance(stored_endpoint.created_at, datetime)
            assert isinstance(stored_endpoint.updated_at, datetime)
            assert stored_endpoint.created_at.tzinfo is not None
            assert stored_endpoint.updated_at.tzinfo is not None
            assert stored_endpoint.created_at.utcoffset() is not None
            assert stored_endpoint.updated_at.utcoffset() is not None
            assert stored_endpoint.updated_at >= stored_endpoint.created_at
    finally:
        if endpoint_id is not None:
            with SessionFactory() as session:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
                    session.commit()
