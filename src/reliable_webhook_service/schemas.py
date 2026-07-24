import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    HttpUrl,
    StringConstraints,
    UrlConstraints,
)

from reliable_webhook_service.models import JsonValue

EndpointName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
    ),
]

EndpointUrl = Annotated[
    HttpUrl,
    UrlConstraints(max_length=2048),
]

EventType = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
    ),
]


class WebhookEndpointCreate(BaseModel):
    name: EndpointName
    target_url: EndpointUrl


class WebhookEndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    target_url: EndpointUrl
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WebhookEventCreate(BaseModel):
    endpoint_id: uuid.UUID
    event_type: EventType
    payload: dict[str, JsonValue]


class WebhookEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    payload: dict[str, JsonValue]
    created_at: datetime


class WebhookDeliveryAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    attempt_number: int
    outcome: Literal["succeeded", "failed"]
    target_url: str
    response_status_code: int | None
    error_message: str | None
    duration_ms: int
    attempted_at: AwareDatetime
