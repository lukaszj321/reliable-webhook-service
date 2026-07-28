import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from reliable_webhook_service.database import get_session
from reliable_webhook_service.delivery_job_query_service import (
    DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    WebhookDeliveryJobCursorValidationError,
    WebhookDeliveryJobEventNotFoundError,
    WebhookDeliveryJobLimitValidationError,
    WebhookDeliveryJobNotFoundError,
    WebhookDeliveryJobStatus,
    WebhookDeliveryJobStatusValidationError,
    get_webhook_delivery_job,
    list_webhook_delivery_jobs,
)
from reliable_webhook_service.delivery_service import (
    InactiveWebhookEndpointError,
    WebhookEndpointNotFoundError,
    WebhookEventNotFoundError,
    execute_webhook_delivery,
)
from reliable_webhook_service.dependencies import (
    SettingsDependency,
    WebhookHttpClientDependency,
)
from reliable_webhook_service.event_service import (
    WebhookEndpointNotFoundError as EventWebhookEndpointNotFoundError,
)
from reliable_webhook_service.event_service import (
    WebhookEventIdempotencyConflictError,
    WebhookIdempotencyKeyValidationError,
    create_idempotent_webhook_event_with_delivery_job,
)
from reliable_webhook_service.models import (
    WebhookDeliveryAttempt,
    WebhookEndpoint,
    WebhookEvent,
)
from reliable_webhook_service.replay_service import (
    WebhookReplayDeliveryJobNotFoundError,
    WebhookReplayDeliveryJobNotReplayableError,
    WebhookReplayEndpointInactiveError,
    WebhookReplayEndpointNotFoundError,
    WebhookReplayEventNotFoundError,
    replay_webhook_event,
)
from reliable_webhook_service.schemas import (
    WebhookDeliveryAttemptResponse,
    WebhookDeliveryJobListResponse,
    WebhookDeliveryJobResponse,
    WebhookEndpointCreate,
    WebhookEndpointResponse,
    WebhookEventCreate,
    WebhookEventResponse,
    WebhookReplayResponse,
)

SessionDependency = Annotated[Session, Depends(get_session)]
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
DeliveryJobStatusQuery = Annotated[
    WebhookDeliveryJobStatus | None,
    Query(alias="status"),
]
DeliveryJobLimitQuery = Annotated[
    int,
    Query(ge=1, le=MAX_WEBHOOK_DELIVERY_JOB_LIST_LIMIT),
]
DeliveryJobCursorQuery = Annotated[str | None, Query()]

router = APIRouter(
    prefix="/webhook-endpoints",
    tags=["webhook-endpoints"],
)

webhook_event_router = APIRouter(
    prefix="/webhook-events",
    tags=["webhook-events"],
)

webhook_delivery_job_router = APIRouter(
    prefix="/webhook-delivery-jobs",
    tags=["webhook-delivery-jobs"],
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    responses={
        status.HTTP_200_OK: {
            "model": WebhookEventResponse,
            "description": "Equivalent webhook event reused",
        },
        status.HTTP_409_CONFLICT: {
            "description": "Idempotency key conflicts with an existing webhook event",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "Invalid request or idempotency key",
        },
    },
)
def create_webhook_event(
    payload: WebhookEventCreate,
    response: Response,
    session: SessionDependency,
    idempotency_key: IdempotencyKeyHeader = None,
) -> WebhookEvent:
    try:
        result = create_idempotent_webhook_event_with_delivery_job(
            session,
            endpoint_id=payload.endpoint_id,
            event_type=payload.event_type,
            payload=payload.payload,
            idempotency_key=idempotency_key,
        )
    except EventWebhookEndpointNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except WebhookIdempotencyKeyValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    except WebhookEventIdempotencyConflictError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    session.commit()
    session.refresh(result.event)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result.event


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


@webhook_event_router.get(
    "/{event_id}/delivery-job",
    response_model=WebhookDeliveryJobResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Webhook event not found"},
        status.HTTP_409_CONFLICT: {"description": "Webhook delivery job not found"},
    },
)
def get_webhook_delivery_job_route(
    event_id: uuid.UUID,
    session: SessionDependency,
) -> WebhookDeliveryJobResponse:
    try:
        result = get_webhook_delivery_job(session, event_id=event_id)
    except WebhookDeliveryJobEventNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except WebhookDeliveryJobNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return WebhookDeliveryJobResponse.model_validate(result)


@webhook_delivery_job_router.get(
    "",
    response_model=WebhookDeliveryJobListResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid status, limit, or cursor"},
    },
)
def list_webhook_delivery_jobs_route(
    session: SessionDependency,
    job_status: DeliveryJobStatusQuery = None,
    limit: DeliveryJobLimitQuery = DEFAULT_WEBHOOK_DELIVERY_JOB_LIST_LIMIT,
    cursor: DeliveryJobCursorQuery = None,
) -> WebhookDeliveryJobListResponse:
    try:
        page = list_webhook_delivery_jobs(
            session,
            status=job_status,
            limit=limit,
            cursor=cursor,
        )
    except (
        WebhookDeliveryJobStatusValidationError,
        WebhookDeliveryJobLimitValidationError,
        WebhookDeliveryJobCursorValidationError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error
    return WebhookDeliveryJobListResponse(
        items=[WebhookDeliveryJobResponse.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@webhook_event_router.post(
    "/{event_id}/replay",
    response_model=WebhookReplayResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def replay_webhook_event_route(
    event_id: uuid.UUID,
    session: SessionDependency,
) -> WebhookReplayResponse:
    replayed_at = _utc_now()
    try:
        result = replay_webhook_event(
            session,
            event_id=event_id,
            replayed_at=replayed_at,
        )
    except WebhookReplayEventNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (
        WebhookReplayEndpointNotFoundError,
        WebhookReplayEndpointInactiveError,
        WebhookReplayDeliveryJobNotFoundError,
        WebhookReplayDeliveryJobNotReplayableError,
    ) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    session.commit()
    return WebhookReplayResponse(
        event_id=result.event_id,
        delivery_job_id=result.delivery_job_id,
        status="pending",
        next_attempt_at=result.next_attempt_at,
    )


@webhook_event_router.post(
    "/{event_id}/delivery-attempts",
    response_model=WebhookDeliveryAttemptResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_webhook_delivery_attempt(
    event_id: uuid.UUID,
    session: SessionDependency,
    http_client: WebhookHttpClientDependency,
    settings: SettingsDependency,
) -> WebhookDeliveryAttempt:
    try:
        attempt = execute_webhook_delivery(
            session,
            event_id=event_id,
            http_client=http_client,
            timeout_seconds=settings.webhook_delivery_timeout_seconds,
        )
    except WebhookEventNotFoundError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(error),
        ) from error
    except (WebhookEndpointNotFoundError, InactiveWebhookEndpointError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    session.commit()
    session.refresh(attempt)
    return attempt
