import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from reliable_webhook_service.database import get_session
from reliable_webhook_service.dependencies import SettingsDependency
from reliable_webhook_service.operations_service import (
    check_database_readiness,
    get_webhook_operational_summary,
)
from reliable_webhook_service.schemas import (
    ReadinessChecksResponse,
    ReadinessResponse,
    WebhookDeliveryJobOperationalCountsResponse,
    WebhookOperationalSummaryResponse,
)

SessionDependency = Annotated[Session, Depends(get_session)]

router = APIRouter(tags=["operations"])

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "Database unavailable",
        },
    },
)
def readiness(
    response: Response,
    session: SessionDependency,
) -> ReadinessResponse:
    try:
        result = check_database_readiness(session)
    except SQLAlchemyError as error:
        logger.warning(
            "database_readiness_failed error_type=%s",
            type(error).__name__,
        )
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="not_ready",
            checks=ReadinessChecksResponse(database="unavailable"),
        )

    return ReadinessResponse(
        status="ready",
        checks=ReadinessChecksResponse(database=result.database),
    )


@router.get(
    "/operations/summary",
    response_model=WebhookOperationalSummaryResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Operational summary unavailable",
        },
    },
)
def operational_summary(
    session: SessionDependency,
    settings: SettingsDependency,
) -> WebhookOperationalSummaryResponse:
    generated_at = _utc_now()
    try:
        result = get_webhook_operational_summary(
            session,
            generated_at=generated_at,
            stale_processing_timeout_seconds=(
                settings.webhook_worker_stale_processing_timeout_seconds
            ),
        )
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operational summary unavailable",
        ) from error

    return WebhookOperationalSummaryResponse(
        generated_at=result.generated_at,
        delivery_jobs=WebhookDeliveryJobOperationalCountsResponse(
            pending=result.delivery_jobs.pending,
            processing=result.delivery_jobs.processing,
            succeeded=result.delivery_jobs.succeeded,
            dead_letter=result.delivery_jobs.dead_letter,
            due_pending=result.delivery_jobs.due_pending,
            stale_processing=result.delivery_jobs.stale_processing,
        ),
        oldest_due_pending_at=result.oldest_due_pending_at,
        oldest_processing_updated_at=result.oldest_processing_updated_at,
        stale_processing_before=result.stale_processing_before,
    )
