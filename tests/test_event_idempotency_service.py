import uuid
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, fields
from types import TracebackType
from typing import cast

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import reliable_webhook_service.event_service as event_service
from reliable_webhook_service.event_service import (
    WebhookEventCreationResult,
    WebhookEventIdempotencyConflictError,
    WebhookIdempotencyKeyValidationError,
    create_idempotent_webhook_event_with_delivery_job,
    create_webhook_event_with_delivery_job,
    normalize_webhook_idempotency_key,
)
from reliable_webhook_service.models import JsonValue, WebhookEndpoint, WebhookEvent

ENDPOINT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _ScalarResult:
    def one_or_none(self) -> None:
        return None


class _NestedTransaction(AbstractContextManager[None]):
    def __init__(self, session: "_FailingSession") -> None:
        self._session = session

    def __enter__(self) -> None:
        self._session.nested_entered += 1

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._session.nested_exited += 1


class _FailingSession:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.endpoint = WebhookEndpoint(
            id=ENDPOINT_ID,
            name="Idempotency unit endpoint",
            target_url="https://example.test/idempotency-unit",
        )
        self.added_objects: list[object] = []
        self.flush_count = 0
        self.nested_entered = 0
        self.nested_exited = 0
        self.commit_called = False
        self.rollback_called = False
        self.close_called = False

    def get(
        self,
        entity: type[WebhookEndpoint],
        identifier: uuid.UUID,
    ) -> WebhookEndpoint | None:
        assert entity is WebhookEndpoint
        assert identifier == ENDPOINT_ID
        return self.endpoint

    def scalars(self, statement: object) -> _ScalarResult:
        assert statement is not None
        return _ScalarResult()

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction(self)

    def add(self, instance: object) -> None:
        self.added_objects.append(instance)

    def flush(self) -> None:
        self.flush_count += 1
        raise self.error

    def commit(self) -> None:
        self.commit_called = True
        raise AssertionError("Service must not commit")

    def rollback(self) -> None:
        self.rollback_called = True
        raise AssertionError("Service must not roll back the outer transaction")

    def close(self) -> None:
        self.close_called = True
        raise AssertionError("Service must not close the session")


class _Diagnostic:
    def __init__(self, constraint_name: str | None) -> None:
        self.constraint_name = constraint_name


class _DriverIntegrityError(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        super().__init__("driver integrity error")
        self.diag = _Diagnostic(constraint_name)


@pytest.mark.parametrize(
    ("raw_key", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param("key-123", "key-123", id="plain"),
        pytest.param("  key-123  ", "key-123", id="trim"),
        pytest.param("  key  123  ", "key  123", id="internal-whitespace"),
        pytest.param("Order-123", "Order-123", id="case"),
        pytest.param("\t\n key-123 \r\n", "key-123", id="surrounding-control-whitespace"),
        pytest.param("k" * 255, "k" * 255, id="maximum-length"),
    ],
)
def test_normalize_webhook_idempotency_key(
    raw_key: str | None,
    expected: str | None,
) -> None:
    assert normalize_webhook_idempotency_key(raw_key) == expected


@pytest.mark.parametrize(
    "raw_key",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("\t\r\n", id="control-whitespace"),
    ],
)
def test_rejects_empty_normalized_idempotency_key(raw_key: str) -> None:
    with pytest.raises(
        WebhookIdempotencyKeyValidationError,
        match="^Idempotency key must not be empty$",
    ):
        normalize_webhook_idempotency_key(raw_key)


def test_rejects_oversized_key_without_exposing_value() -> None:
    raw_key = "SENSITIVE-" + ("k" * 256)

    with pytest.raises(WebhookIdempotencyKeyValidationError) as raised:
        normalize_webhook_idempotency_key(raw_key)

    assert str(raised.value) == "Idempotency key must not exceed 255 characters"
    assert raw_key not in str(raised.value)
    assert "SENSITIVE" not in str(raised.value)


def test_creation_result_is_frozen_slotted_and_exact() -> None:
    event = WebhookEvent(
        endpoint_id=ENDPOINT_ID,
        event_type="idempotency.result",
        payload={"result": True},
        idempotency_key="result-key",
    )
    result = WebhookEventCreationResult(event=event, created=True)

    assert [field.name for field in fields(WebhookEventCreationResult)] == [
        "event",
        "created",
    ]
    assert WebhookEventCreationResult.__slots__ == ("event", "created")
    assert not hasattr(result, "__dict__")
    assert result.event is event
    assert result.created is True

    with pytest.raises(FrozenInstanceError):
        result.created = False  # type: ignore[misc]


def test_conflict_error_message_contains_no_request_data() -> None:
    idempotency_key = "SENSITIVE-IDEMPOTENCY-KEY"
    event_type = "sensitive.event.type"
    payload: dict[str, JsonValue] = {"secret": "payload-value"}
    error = WebhookEventIdempotencyConflictError(
        "Idempotency key conflicts with an existing webhook event"
    )
    message = str(error)

    assert message == "Idempotency key conflicts with an existing webhook event"
    assert idempotency_key not in message
    assert event_type not in message
    assert str(payload) not in message
    assert "payload-value" not in message


def test_compatible_wrapper_delegates_with_none_and_returns_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = WebhookEvent(
        endpoint_id=ENDPOINT_ID,
        event_type="wrapper.event",
        payload={"wrapper": True},
    )
    calls: list[dict[str, object]] = []

    def fake_create(
        session: Session,
        *,
        endpoint_id: uuid.UUID,
        event_type: str,
        payload: dict[str, JsonValue],
        idempotency_key: str | None,
    ) -> WebhookEventCreationResult:
        calls.append(
            {
                "session": session,
                "endpoint_id": endpoint_id,
                "event_type": event_type,
                "payload": payload,
                "idempotency_key": idempotency_key,
            }
        )
        return WebhookEventCreationResult(event=event, created=True)

    monkeypatch.setattr(
        event_service,
        "create_idempotent_webhook_event_with_delivery_job",
        fake_create,
    )
    session = cast(Session, object())
    payload: dict[str, JsonValue] = {"wrapper": True}

    returned_event = create_webhook_event_with_delivery_job(
        session,
        endpoint_id=ENDPOINT_ID,
        event_type="wrapper.event",
        payload=payload,
    )

    assert returned_event is event
    assert calls == [
        {
            "session": session,
            "endpoint_id": ENDPOINT_ID,
            "event_type": "wrapper.event",
            "payload": payload,
            "idempotency_key": None,
        }
    ]


@pytest.mark.parametrize(
    "driver_error",
    [
        pytest.param(
            _DriverIntegrityError("uq_webhook_delivery_jobs_event_id"),
            id="other-constraint",
        ),
        pytest.param(_DriverIntegrityError(None), id="missing-constraint-name"),
        pytest.param(Exception("driver error without diagnostics"), id="missing-diagnostics"),
    ],
)
def test_unrelated_integrity_error_propagates_without_second_insert(
    driver_error: BaseException,
) -> None:
    integrity_error = IntegrityError("INSERT", {}, driver_error)
    session = _FailingSession(integrity_error)

    with pytest.raises(IntegrityError) as raised:
        create_idempotent_webhook_event_with_delivery_job(
            cast(Session, session),
            endpoint_id=ENDPOINT_ID,
            event_type="idempotency.unrelated-integrity",
            payload={"value": True},
            idempotency_key="unrelated-integrity-key",
        )

    assert raised.value is integrity_error
    assert len(session.added_objects) == 1
    assert isinstance(session.added_objects[0], WebhookEvent)
    assert session.flush_count == 1
    assert session.nested_entered == 1
    assert session.nested_exited == 1
    assert session.commit_called is False
    assert session.rollback_called is False
    assert session.close_called is False


def test_non_integrity_error_propagates_without_second_insert() -> None:
    original_error = RuntimeError("database operation failed")
    session = _FailingSession(original_error)

    with pytest.raises(RuntimeError) as raised:
        create_idempotent_webhook_event_with_delivery_job(
            cast(Session, session),
            endpoint_id=ENDPOINT_ID,
            event_type="idempotency.other-error",
            payload={"value": True},
            idempotency_key="other-error-key",
        )

    assert raised.value is original_error
    assert len(session.added_objects) == 1
    assert session.flush_count == 1
    assert session.nested_entered == 1
    assert session.nested_exited == 1
    assert session.commit_called is False
    assert session.rollback_called is False
    assert session.close_called is False


def test_public_symbols_are_exported() -> None:
    assert set(event_service.__all__) == {
        "WebhookEndpointNotFoundError",
        "WebhookEventCreationResult",
        "WebhookEventIdempotencyConflictError",
        "WebhookIdempotencyKeyValidationError",
        "create_idempotent_webhook_event_with_delivery_job",
        "create_webhook_event_with_delivery_job",
        "normalize_webhook_idempotency_key",
    }
