from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from reliable_webhook_service.delivery_job_service import claim_due_webhook_delivery_jobs


@pytest.mark.parametrize("limit", [0, -1, True, False])
def test_claim_due_jobs_rejects_invalid_limit_before_database_access(
    limit: int,
) -> None:
    with Session() as session:
        with pytest.raises(ValueError, match="limit"):
            claim_due_webhook_delivery_jobs(
                session,
                claimed_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
                limit=limit,
            )


def test_claim_due_jobs_rejects_naive_claimed_at_before_database_access() -> None:
    with Session() as session:
        with pytest.raises(ValueError, match="timezone-aware datetime"):
            claim_due_webhook_delivery_jobs(
                session,
                claimed_at=datetime(2026, 7, 28, 10, 0),
                limit=1,
            )
