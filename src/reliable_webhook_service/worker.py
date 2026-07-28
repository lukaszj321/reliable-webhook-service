import logging
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event
from types import FrameType

import httpx2
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from reliable_webhook_service.config import Settings
from reliable_webhook_service.delivery_http import Httpx2WebhookHttpClient
from reliable_webhook_service.worker_loop_service import (
    WebhookWorkerRunResult,
    run_webhook_worker,
)

__all__ = [
    "main",
    "run_worker_process",
]

logger = logging.getLogger(__name__)


@contextmanager
def _installed_shutdown_handlers(shutdown_event: Event) -> Iterator[None]:
    def request_shutdown(_signal_number: int, _frame: FrameType | None) -> None:
        shutdown_event.set()

    signals_to_install = [signal.SIGINT]
    if hasattr(signal, "SIGTERM"):
        signals_to_install.append(signal.SIGTERM)

    previous_handlers: list[
        tuple[
            signal.Signals,
            signal.Handlers | int | None | Callable[[int, FrameType | None], object],
        ]
    ] = []
    try:
        for signal_number in signals_to_install:
            previous_handler = signal.getsignal(signal_number)
            signal.signal(signal_number, request_shutdown)
            previous_handlers.append((signal_number, previous_handler))
    except BaseException:
        for signal_number, previous_handler in reversed(previous_handlers):
            signal.signal(signal_number, previous_handler)
        raise

    try:
        yield
    finally:
        for signal_number, previous_handler in reversed(previous_handlers):
            signal.signal(signal_number, previous_handler)


def run_worker_process() -> WebhookWorkerRunResult:
    settings = Settings()
    local_engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        local_session_factory: sessionmaker[Session] = sessionmaker(
            bind=local_engine,
            class_=Session,
            expire_on_commit=False,
        )
        shutdown_event = Event()
        with _installed_shutdown_handlers(shutdown_event):
            owned_http_client = httpx2.Client()
            http_context_entered = False
            try:
                with owned_http_client as raw_http_client:
                    http_context_entered = True
                    http_client = Httpx2WebhookHttpClient(raw_http_client)
                    return run_webhook_worker(
                        session_factory=local_session_factory,
                        http_client=http_client,
                        poll_interval_seconds=settings.webhook_worker_poll_interval_seconds,
                        stale_processing_timeout_seconds=(
                            settings.webhook_worker_stale_processing_timeout_seconds
                        ),
                        recovery_limit=settings.webhook_worker_recovery_limit,
                        processing_limit=settings.webhook_worker_processing_limit,
                        timeout_seconds=settings.webhook_delivery_timeout_seconds,
                        max_attempts=settings.webhook_delivery_max_attempts,
                        base_delay_seconds=settings.webhook_delivery_retry_base_seconds,
                        max_delay_seconds=settings.webhook_delivery_retry_max_seconds,
                        stop_requested=shutdown_event.is_set,
                        wait=shutdown_event.wait,
                    )
            finally:
                if not http_context_entered:
                    owned_http_client.close()
    finally:
        local_engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        result = run_worker_process()
    except Exception as error:
        logger.critical(
            "Webhook worker process failed: %s",
            type(error).__name__,
        )
        return 1

    logger.info(
        (
            "Webhook worker process completed: iterations_completed=%d "
            "total_recovered=%d total_claimed=%d total_completed=%d "
            "shutdown_requested=%s"
        ),
        result.iterations_completed,
        result.total_recovered_count,
        result.total_claimed_count,
        result.total_completed_count,
        result.shutdown_requested,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
