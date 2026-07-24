import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from reliable_webhook_service.models import WebhookDeliveryAttempt
from reliable_webhook_service.schemas import WebhookDeliveryAttemptResponse


def test_webhook_delivery_attempt_response_serializes_orm_model() -> None:
    attempt_id = uuid.uuid4()
    event_id = uuid.uuid4()
    target_url = "https://example.com/deliver?token=abc%2F123&source=webhook"
    attempted_at = datetime(2026, 7, 24, 12, 34, 56, tzinfo=UTC)
    attempt = WebhookDeliveryAttempt(
        id=attempt_id,
        event_id=event_id,
        attempt_number=2,
        outcome="failed",
        target_url=target_url,
        response_status_code=503,
        error_message="Service unavailable",
        duration_ms=321,
        attempted_at=attempted_at,
    )

    response = WebhookDeliveryAttemptResponse.model_validate(attempt)

    assert response.id == attempt_id
    assert response.event_id == event_id
    assert response.attempt_number == 2
    assert response.outcome == "failed"
    assert response.target_url == target_url
    assert response.response_status_code == 503
    assert response.error_message == "Service unavailable"
    assert response.duration_ms == 321
    assert response.attempted_at == attempted_at

    serialized = response.model_dump(mode="json")

    assert serialized["id"] == str(attempt_id)
    assert serialized["event_id"] == str(event_id)
    assert serialized["target_url"] == target_url
    serialized_attempted_at = serialized["attempted_at"]
    assert isinstance(serialized_attempted_at, str)
    parsed_attempted_at = datetime.fromisoformat(serialized_attempted_at.replace("Z", "+00:00"))
    assert parsed_attempted_at.tzinfo is not None
    assert parsed_attempted_at.utcoffset() is not None


def test_webhook_delivery_attempt_response_supports_nullable_fields() -> None:
    attempt = WebhookDeliveryAttempt(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        attempt_number=1,
        outcome="failed",
        target_url="https://example.com/deliver?network=unavailable",
        response_status_code=None,
        error_message=None,
        duration_ms=0,
        attempted_at=datetime(2026, 7, 24, 13, 0, tzinfo=UTC),
    )

    response = WebhookDeliveryAttemptResponse.model_validate(attempt)

    assert response.response_status_code is None
    assert response.error_message is None

    serialized = response.model_dump(mode="json")

    assert serialized["response_status_code"] is None
    assert serialized["error_message"] is None


def test_webhook_delivery_attempt_response_rejects_naive_timestamp() -> None:
    attempt = WebhookDeliveryAttempt(
        id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        attempt_number=1,
        outcome="succeeded",
        target_url="https://example.com/deliver",
        response_status_code=200,
        error_message=None,
        duration_ms=25,
        attempted_at=datetime(2026, 7, 24, 14, 0),
    )

    with pytest.raises(ValidationError):
        WebhookDeliveryAttemptResponse.model_validate(attempt)
