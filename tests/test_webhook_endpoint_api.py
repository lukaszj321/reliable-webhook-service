import uuid
from datetime import datetime

from fastapi.testclient import TestClient

from reliable_webhook_service.database import SessionFactory
from reliable_webhook_service.main import app
from reliable_webhook_service.models import WebhookEndpoint


def test_create_webhook_endpoint() -> None:
    marker = uuid.uuid4()
    expected_name = f"API test endpoint {marker}"
    request_name = f"  {expected_name}  "
    expected_target_url = f"https://example.com/webhooks/{marker}"
    endpoint_id: uuid.UUID | None = None

    try:
        with TestClient(app) as client:
            response = client.post(
                "/webhook-endpoints",
                json={
                    "name": request_name,
                    "target_url": expected_target_url,
                },
            )

        assert response.status_code == 201

        response_body = response.json()
        assert set(response_body) == {
            "id",
            "name",
            "target_url",
            "is_active",
            "created_at",
            "updated_at",
        }

        endpoint_id = uuid.UUID(response_body["id"])
        assert response_body["name"] == expected_name
        assert isinstance(response_body["target_url"], str)
        assert response_body["target_url"] == expected_target_url
        assert response_body["is_active"] is True

        created_at = datetime.fromisoformat(response_body["created_at"].replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(response_body["updated_at"].replace("Z", "+00:00"))
        assert created_at.tzinfo is not None
        assert created_at.utcoffset() is not None
        assert updated_at.tzinfo is not None
        assert updated_at.utcoffset() is not None
        assert updated_at >= created_at

        with SessionFactory() as session:
            stored_endpoint = session.get(WebhookEndpoint, endpoint_id)

            assert stored_endpoint is not None
            assert stored_endpoint.id == endpoint_id
            assert stored_endpoint.name == expected_name
            assert stored_endpoint.target_url == response_body["target_url"]
            assert stored_endpoint.is_active is True
            assert stored_endpoint.created_at is not None
            assert stored_endpoint.updated_at is not None
            assert stored_endpoint.created_at.tzinfo is not None
            assert stored_endpoint.created_at.utcoffset() is not None
            assert stored_endpoint.updated_at.tzinfo is not None
            assert stored_endpoint.updated_at.utcoffset() is not None
    finally:
        if endpoint_id is not None:
            with SessionFactory() as session:
                stored_endpoint = session.get(WebhookEndpoint, endpoint_id)
                if stored_endpoint is not None:
                    session.delete(stored_endpoint)
                    session.commit()

    assert endpoint_id is not None
    with SessionFactory() as session:
        assert session.get(WebhookEndpoint, endpoint_id) is None
