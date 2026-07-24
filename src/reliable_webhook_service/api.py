import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import get_session
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.schemas import (
    WebhookDeliveryAttemptResponse,
    WebhookEndpointCreate,
    WebhookEndpointResponse,
    WebhookEventCreate,
    WebhookEventResponse,
)

SessionDependency = Annotated[Session, Depends(get_session)]

router = APIRouter(
    prefix="/webhook-endpoints",
    tags=["webhook-endpoints"],
)

webhook_event_router = APIRouter(
    prefix="/webhook-events",
    tags=["webhook-events"],
)


@router.post(
    "",
    response_model=WebhookEndpointResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_webhook_endpoint(
    payload: WebhookEndpointCreate,
    session: SessionDependency,
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        name=payload.name,
        target_url=str(payload.target_url),
    )
    session.add(endpoint)
    session.commit()
    session.refresh(endpoint)
    return endpoint


@router.get(
    "",
    response_model=list[WebhookEndpointResponse],
)
def list_webhook_endpoints(
    session: SessionDependency,
) -> list[WebhookEndpoint]:
    statement = select(WebhookEndpoint).order_by(
        WebhookEndpoint.created_at.asc(),
        WebhookEndpoint.id.asc(),
    )
    return list(session.scalars(statement).all())


@webhook_event_router.post(
    "",
    response_model=WebhookEventResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_webhook_event(
    payload: WebhookEventCreate,
    session: SessionDependency,
) -> WebhookEvent:
    endpoint = session.get(WebhookEndpoint, payload.endpoint_id)
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook endpoint not found",
        )

    event = WebhookEvent(
        endpoint_id=payload.endpoint_id,
        event_type=payload.event_type,
        payload=payload.payload,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@webhook_event_router.get(
    "/{event_id}/delivery-attempts",
    response_model=list[WebhookDeliveryAttemptResponse],
)
def list_webhook_delivery_attempts(
    event_id: uuid.UUID,
    session: SessionDependency,
) -> list[WebhookDeliveryAttempt]:
    event = session.get(WebhookEvent, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook event not found",
        )

    statement = (
        select(WebhookDeliveryAttempt)
        .where(WebhookDeliveryAttempt.event_id == event_id)
        .order_by(
            WebhookDeliveryAttempt.attempt_number.asc(),
            WebhookDeliveryAttempt.attempted_at.asc(),
            WebhookDeliveryAttempt.id.asc(),
        )
    )
    return list(session.scalars(statement).all())
