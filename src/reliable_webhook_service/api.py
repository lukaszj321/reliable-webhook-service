from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from reliable_webhook_service.database import get_session
from reliable_webhook_service.models import WebhookEndpoint
from reliable_webhook_service.schemas import WebhookEndpointCreate, WebhookEndpointResponse

SessionDependency = Annotated[Session, Depends(get_session)]

router = APIRouter(
    prefix="/webhook-endpoints",
    tags=["webhook-endpoints"],
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
