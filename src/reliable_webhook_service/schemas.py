import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
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


class WebhookReplayResponse(BaseModel):
    event_id: uuid.UUID
    delivery_job_id: uuid.UUID
    status: Literal["pending"]
    next_attempt_at: AwareDatetime


class WebhookDeliveryJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: uuid.UUID
    status: Literal[
        "pending",
        "processing",
        "succeeded",
        "dead_letter",
    ]
    attempt_count: int
    next_attempt_at: AwareDatetime | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WebhookDeliveryJobListResponse(BaseModel):
    items: list[WebhookDeliveryJobResponse]
    next_cursor: str | None


class ReadinessChecksResponse(BaseModel):
    database: Literal["ok", "unavailable"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecksResponse


class WebhookDeliveryJobOperationalCountsResponse(BaseModel):
    pending: int = Field(ge=0)
    processing: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    dead_letter: int = Field(ge=0)
    due_pending: int = Field(ge=0)
    stale_processing: int = Field(ge=0)


class WebhookOperationalSummaryResponse(BaseModel):
    generated_at: AwareDatetime
    delivery_jobs: WebhookDeliveryJobOperationalCountsResponse
    oldest_due_pending_at: AwareDatetime | None
    oldest_processing_updated_at: AwareDatetime | None
    stale_processing_before: AwareDatetime


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
