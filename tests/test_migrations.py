import json
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, Integer, String, Text, inspect, text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from reliable_webhook_service.database import engine


def test_webhook_endpoint_migration() -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")

    try:
        command.downgrade(alembic_config, "base")
        assert inspect(engine).has_table("webhook_endpoints") is False

        command.upgrade(alembic_config, "head")
        assert inspect(engine).has_table("webhook_endpoints") is True

        column_names = {
            column["name"] for column in inspect(engine).get_columns("webhook_endpoints")
        }
        assert column_names == {
            "id",
            "name",
            "target_url",
            "is_active",
            "created_at",
            "updated_at",
        }

        command.downgrade(alembic_config, "base")
        assert inspect(engine).has_table("webhook_endpoints") is False
    finally:
        command.upgrade(alembic_config, "head")

    assert inspect(engine).has_table("webhook_endpoints") is True


def test_webhook_event_migration() -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")

    try:
        command.upgrade(alembic_config, "head")

        inspector = inspect(engine)
        assert inspector.has_table("webhook_endpoints") is True
        assert inspector.has_table("webhook_events") is True

        columns = inspector.get_columns("webhook_events")
        assert [column["name"] for column in columns] == [
            "id",
            "endpoint_id",
            "event_type",
            "payload",
            "created_at",
            "idempotency_key",
        ]
        columns_by_name = {column["name"]: column for column in columns}

        id_column = columns_by_name["id"]
        assert isinstance(id_column["type"], UUID)
        assert id_column["nullable"] is False
        assert id_column["default"] is None

        endpoint_id_column = columns_by_name["endpoint_id"]
        assert isinstance(endpoint_id_column["type"], UUID)
        assert endpoint_id_column["nullable"] is False
        assert endpoint_id_column["default"] is None

        event_type_column = columns_by_name["event_type"]
        assert isinstance(event_type_column["type"], String)
        assert event_type_column["type"].length == 255
        assert event_type_column["nullable"] is False
        assert event_type_column["default"] is None

        payload_column = columns_by_name["payload"]
        assert isinstance(payload_column["type"], JSONB)
        assert payload_column["nullable"] is False
        assert payload_column["default"] is None

        created_at_column = columns_by_name["created_at"]
        assert isinstance(created_at_column["type"], DateTime)
        assert created_at_column["type"].timezone is True
        assert created_at_column["nullable"] is False
        assert created_at_column["default"] is not None
        assert "now()" in str(created_at_column["default"]).lower()

        primary_key = inspector.get_pk_constraint("webhook_events")
        assert primary_key["constrained_columns"] == ["id"]

        foreign_keys = inspector.get_foreign_keys("webhook_events")
        assert len(foreign_keys) == 1
        foreign_key = foreign_keys[0]
        assert foreign_key["constrained_columns"] == ["endpoint_id"]
        assert foreign_key["referred_table"] == "webhook_endpoints"
        assert foreign_key["referred_columns"] == ["id"]
        assert foreign_key["options"].get("ondelete") is None

        indexes = inspector.get_indexes("webhook_events")
        endpoint_id_indexes = [
            index for index in indexes if index["name"] == "ix_webhook_events_endpoint_id"
        ]
        assert len(endpoint_id_indexes) == 1
        endpoint_id_index = endpoint_id_indexes[0]
        assert endpoint_id_index["column_names"] == ["endpoint_id"]
        assert endpoint_id_index["unique"] is False

        command.downgrade(alembic_config, "5933ef63fabf")

        downgraded_inspector = inspect(engine)
        assert downgraded_inspector.has_table("webhook_events") is False
        assert downgraded_inspector.has_table("webhook_endpoints") is True
    finally:
        command.upgrade(alembic_config, "head")

        restored_inspector = inspect(engine)
        assert restored_inspector.has_table("webhook_endpoints") is True
        assert restored_inspector.has_table("webhook_events") is True


def test_webhook_delivery_attempt_migration() -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")

    try:
        command.upgrade(alembic_config, "head")

        inspector = inspect(engine)
        assert inspector.has_table("webhook_endpoints") is True
        assert inspector.has_table("webhook_events") is True
        assert inspector.has_table("webhook_delivery_attempts") is True

        columns = inspector.get_columns("webhook_delivery_attempts")
        assert [column["name"] for column in columns] == [
            "id",
            "event_id",
            "attempt_number",
            "outcome",
            "target_url",
            "response_status_code",
            "error_message",
            "duration_ms",
            "attempted_at",
        ]
        columns_by_name = {column["name"]: column for column in columns}

        id_column = columns_by_name["id"]
        assert isinstance(id_column["type"], UUID)
        assert id_column["nullable"] is False
        assert id_column["default"] is None

        event_id_column = columns_by_name["event_id"]
        assert isinstance(event_id_column["type"], UUID)
        assert event_id_column["nullable"] is False
        assert event_id_column["default"] is None

        attempt_number_column = columns_by_name["attempt_number"]
        assert isinstance(attempt_number_column["type"], Integer)
        assert attempt_number_column["nullable"] is False
        assert attempt_number_column["default"] is None

        outcome_column = columns_by_name["outcome"]
        assert isinstance(outcome_column["type"], String)
        assert outcome_column["type"].length == 32
        assert outcome_column["nullable"] is False
        assert outcome_column["default"] is None

        target_url_column = columns_by_name["target_url"]
        assert isinstance(target_url_column["type"], String)
        assert target_url_column["type"].length == 2048
        assert target_url_column["nullable"] is False
        assert target_url_column["default"] is None

        response_status_code_column = columns_by_name["response_status_code"]
        assert isinstance(response_status_code_column["type"], Integer)
        assert response_status_code_column["nullable"] is True
        assert response_status_code_column["default"] is None

        error_message_column = columns_by_name["error_message"]
        assert isinstance(error_message_column["type"], Text)
        assert error_message_column["nullable"] is True
        assert error_message_column["default"] is None

        duration_ms_column = columns_by_name["duration_ms"]
        assert isinstance(duration_ms_column["type"], Integer)
        assert duration_ms_column["nullable"] is False
        assert duration_ms_column["default"] is None

        attempted_at_column = columns_by_name["attempted_at"]
        assert isinstance(attempted_at_column["type"], DateTime)
        assert attempted_at_column["type"].timezone is True
        assert attempted_at_column["nullable"] is False
        assert attempted_at_column["default"] is not None
        assert "now()" in str(attempted_at_column["default"]).lower()

        primary_key = inspector.get_pk_constraint("webhook_delivery_attempts")
        assert primary_key["constrained_columns"] == ["id"]

        foreign_keys = inspector.get_foreign_keys("webhook_delivery_attempts")
        assert len(foreign_keys) == 1
        foreign_key = foreign_keys[0]
        assert foreign_key["constrained_columns"] == ["event_id"]
        assert foreign_key["referred_table"] == "webhook_events"
        assert foreign_key["referred_columns"] == ["id"]
        assert foreign_key["options"].get("ondelete") is None

        indexes = inspector.get_indexes("webhook_delivery_attempts")
        event_id_indexes = [
            index for index in indexes if index["name"] == "ix_webhook_delivery_attempts_event_id"
        ]
        assert len(event_id_indexes) == 1
        event_id_index = event_id_indexes[0]
        assert event_id_index["column_names"] == ["event_id"]
        assert event_id_index["unique"] is False

        unique_constraints = inspector.get_unique_constraints("webhook_delivery_attempts")
        event_attempt_unique_constraints = [
            constraint
            for constraint in unique_constraints
            if constraint["name"] == "uq_webhook_delivery_attempts_event_id_attempt_number"
        ]
        assert len(event_attempt_unique_constraints) == 1
        event_attempt_unique_constraint = event_attempt_unique_constraints[0]
        assert event_attempt_unique_constraint["column_names"] == [
            "event_id",
            "attempt_number",
        ]

        check_constraints = inspector.get_check_constraints("webhook_delivery_attempts")
        normalized_checks = {
            constraint["name"]: " ".join(str(constraint["sqltext"]).lower().split())
            for constraint in check_constraints
        }
        assert set(normalized_checks) == {
            "ck_webhook_delivery_attempts_attempt_number_positive",
            "ck_webhook_delivery_attempts_outcome",
            "ck_webhook_delivery_attempts_response_status_code",
            "ck_webhook_delivery_attempts_duration_ms_non_negative",
        }

        attempt_number_check = normalized_checks[
            "ck_webhook_delivery_attempts_attempt_number_positive"
        ]
        assert "attempt_number" in attempt_number_check
        assert "> 0" in attempt_number_check

        outcome_check = normalized_checks["ck_webhook_delivery_attempts_outcome"]
        assert "outcome" in outcome_check
        assert "succeeded" in outcome_check
        assert "failed" in outcome_check

        response_status_code_check = normalized_checks[
            "ck_webhook_delivery_attempts_response_status_code"
        ]
        assert "response_status_code" in response_status_code_check
        assert "100" in response_status_code_check
        assert "599" in response_status_code_check
        assert "is null" in response_status_code_check
        assert "between 100 and 599" in response_status_code_check or (
            ">= 100" in response_status_code_check and "<= 599" in response_status_code_check
        )

        duration_ms_check = normalized_checks[
            "ck_webhook_delivery_attempts_duration_ms_non_negative"
        ]
        assert "duration_ms" in duration_ms_check
        assert ">= 0" in duration_ms_check

        command.downgrade(alembic_config, "df51b920cf81")

        downgraded_inspector = inspect(engine)
        assert downgraded_inspector.has_table("webhook_delivery_attempts") is False
        assert downgraded_inspector.has_table("webhook_events") is True
        assert downgraded_inspector.has_table("webhook_endpoints") is True

        command.upgrade(alembic_config, "head")

        upgraded_inspector = inspect(engine)
        assert upgraded_inspector.has_table("webhook_delivery_attempts") is True
        assert upgraded_inspector.has_table("webhook_events") is True
        assert upgraded_inspector.has_table("webhook_endpoints") is True
    finally:
        command.upgrade(alembic_config, "head")

    final_inspector = inspect(engine)
    assert final_inspector.has_table("webhook_delivery_attempts") is True
    assert final_inspector.has_table("webhook_events") is True
    assert final_inspector.has_table("webhook_endpoints") is True


def test_webhook_event_idempotency_migration() -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")
    previous_revision = "200628ca5044"
    endpoint_id = uuid.uuid4()
    event_id = uuid.uuid4()
    constraint_name = "uq_webhook_events_endpoint_id_idempotency_key"

    try:
        command.downgrade(alembic_config, previous_revision)

        previous_inspector = inspect(engine)
        assert previous_inspector.has_table("webhook_endpoints") is True
        assert previous_inspector.has_table("webhook_events") is True
        assert previous_inspector.has_table("webhook_delivery_attempts") is True
        assert previous_inspector.has_table("webhook_delivery_jobs") is True
        assert [column["name"] for column in previous_inspector.get_columns("webhook_events")] == [
            "id",
            "endpoint_id",
            "event_type",
            "payload",
            "created_at",
        ]
        assert all(
            constraint["name"] != constraint_name
            for constraint in previous_inspector.get_unique_constraints("webhook_events")
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO webhook_endpoints (
                        id,
                        name,
                        target_url
                    )
                    VALUES (
                        :id,
                        :name,
                        :target_url
                    )
                    """
                ),
                {
                    "id": endpoint_id,
                    "name": f"Idempotency migration endpoint {endpoint_id}",
                    "target_url": f"https://example.test/idempotency-migration/{endpoint_id}",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO webhook_events (
                        id,
                        endpoint_id,
                        event_type,
                        payload
                    )
                    VALUES (
                        :id,
                        :endpoint_id,
                        :event_type,
                        CAST(:payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": event_id,
                    "endpoint_id": endpoint_id,
                    "event_type": "idempotency.migration",
                    "payload": json.dumps({"marker": str(event_id)}),
                },
            )

        command.upgrade(alembic_config, "head")

        upgraded_inspector = inspect(engine)
        columns = upgraded_inspector.get_columns("webhook_events")
        columns_by_name = {column["name"]: column for column in columns}
        idempotency_key_column = columns_by_name["idempotency_key"]
        assert isinstance(idempotency_key_column["type"], String)
        assert idempotency_key_column["type"].length == 255
        assert idempotency_key_column["nullable"] is True
        assert idempotency_key_column["default"] is None

        unique_constraints = upgraded_inspector.get_unique_constraints("webhook_events")
        idempotency_constraints = [
            constraint for constraint in unique_constraints if constraint["name"] == constraint_name
        ]
        assert len(idempotency_constraints) == 1
        assert idempotency_constraints[0]["column_names"] == [
            "endpoint_id",
            "idempotency_key",
        ]

        indexes = upgraded_inspector.get_indexes("webhook_events")
        endpoint_id_indexes = [
            index for index in indexes if index["name"] == "ix_webhook_events_endpoint_id"
        ]
        assert len(endpoint_id_indexes) == 1
        assert endpoint_id_indexes[0]["column_names"] == ["endpoint_id"]
        assert endpoint_id_indexes[0]["unique"] is False
        speculative_idempotency_indexes = [
            index
            for index in indexes
            if "idempotency_key" in index["column_names"]
            and index.get("duplicates_constraint") != constraint_name
        ]
        assert speculative_idempotency_indexes == []

        with engine.connect() as connection:
            stored_idempotency_key = connection.scalar(
                text(
                    """
                    SELECT idempotency_key
                    FROM webhook_events
                    WHERE id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
        assert stored_idempotency_key is None

        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM webhook_events WHERE id = :event_id"),
                {"event_id": event_id},
            )
            connection.execute(
                text("DELETE FROM webhook_endpoints WHERE id = :endpoint_id"),
                {"endpoint_id": endpoint_id},
            )

        command.downgrade(alembic_config, previous_revision)

        downgraded_inspector = inspect(engine)
        assert downgraded_inspector.has_table("webhook_endpoints") is True
        assert downgraded_inspector.has_table("webhook_events") is True
        assert downgraded_inspector.has_table("webhook_delivery_attempts") is True
        assert downgraded_inspector.has_table("webhook_delivery_jobs") is True
        assert [
            column["name"] for column in downgraded_inspector.get_columns("webhook_events")
        ] == [
            "id",
            "endpoint_id",
            "event_type",
            "payload",
            "created_at",
        ]
        assert all(
            constraint["name"] != constraint_name
            for constraint in downgraded_inspector.get_unique_constraints("webhook_events")
        )
        downgraded_indexes = downgraded_inspector.get_indexes("webhook_events")
        assert (
            len(
                [
                    index
                    for index in downgraded_indexes
                    if index["name"] == "ix_webhook_events_endpoint_id"
                    and index["column_names"] == ["endpoint_id"]
                    and index["unique"] is False
                ]
            )
            == 1
        )

        command.upgrade(alembic_config, "head")

        restored_inspector = inspect(engine)
        assert "idempotency_key" in {
            column["name"] for column in restored_inspector.get_columns("webhook_events")
        }
        assert (
            len(
                [
                    constraint
                    for constraint in restored_inspector.get_unique_constraints("webhook_events")
                    if constraint["name"] == constraint_name
                    and constraint["column_names"] == ["endpoint_id", "idempotency_key"]
                ]
            )
            == 1
        )
    finally:
        command.upgrade(alembic_config, "head")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM webhook_delivery_attempts WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
            connection.execute(
                text("DELETE FROM webhook_delivery_jobs WHERE event_id = :event_id"),
                {"event_id": event_id},
            )
            connection.execute(
                text("DELETE FROM webhook_events WHERE id = :event_id"),
                {"event_id": event_id},
            )
            connection.execute(
                text("DELETE FROM webhook_endpoints WHERE id = :endpoint_id"),
                {"endpoint_id": endpoint_id},
            )

    final_inspector = inspect(engine)
    assert "idempotency_key" in {
        column["name"] for column in final_inspector.get_columns("webhook_events")
    }
    assert (
        len(
            [
                constraint
                for constraint in final_inspector.get_unique_constraints("webhook_events")
                if constraint["name"] == constraint_name
                and constraint["column_names"] == ["endpoint_id", "idempotency_key"]
            ]
        )
        == 1
    )


def test_webhook_delivery_job_migration() -> None:
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("path_separator", "os")

    try:
        command.upgrade(alembic_config, "head")

        inspector = inspect(engine)
        assert inspector.has_table("webhook_endpoints") is True
        assert inspector.has_table("webhook_events") is True
        assert inspector.has_table("webhook_delivery_attempts") is True
        assert inspector.has_table("webhook_delivery_jobs") is True

        columns = inspector.get_columns("webhook_delivery_jobs")
        assert [column["name"] for column in columns] == [
            "id",
            "event_id",
            "status",
            "next_attempt_at",
            "created_at",
            "updated_at",
        ]
        columns_by_name = {column["name"]: column for column in columns}

        id_column = columns_by_name["id"]
        assert isinstance(id_column["type"], UUID)
        assert id_column["nullable"] is False
        assert id_column["default"] is None

        event_id_column = columns_by_name["event_id"]
        assert isinstance(event_id_column["type"], UUID)
        assert event_id_column["nullable"] is False
        assert event_id_column["default"] is None

        status_column = columns_by_name["status"]
        assert isinstance(status_column["type"], String)
        assert status_column["type"].length == 32
        assert status_column["nullable"] is False
        assert status_column["default"] is None

        next_attempt_at_column = columns_by_name["next_attempt_at"]
        assert isinstance(next_attempt_at_column["type"], DateTime)
        assert next_attempt_at_column["type"].timezone is True
        assert next_attempt_at_column["nullable"] is True
        assert next_attempt_at_column["default"] is None

        created_at_column = columns_by_name["created_at"]
        assert isinstance(created_at_column["type"], DateTime)
        assert created_at_column["type"].timezone is True
        assert created_at_column["nullable"] is False
        assert created_at_column["default"] is not None
        assert "now()" in str(created_at_column["default"]).lower()

        updated_at_column = columns_by_name["updated_at"]
        assert isinstance(updated_at_column["type"], DateTime)
        assert updated_at_column["type"].timezone is True
        assert updated_at_column["nullable"] is False
        assert updated_at_column["default"] is not None
        assert "now()" in str(updated_at_column["default"]).lower()

        primary_key = inspector.get_pk_constraint("webhook_delivery_jobs")
        assert primary_key["constrained_columns"] == ["id"]

        foreign_keys = inspector.get_foreign_keys("webhook_delivery_jobs")
        assert len(foreign_keys) == 1
        foreign_key = foreign_keys[0]
        assert foreign_key["constrained_columns"] == ["event_id"]
        assert foreign_key["referred_table"] == "webhook_events"
        assert foreign_key["referred_columns"] == ["id"]
        assert foreign_key["options"].get("ondelete") == "CASCADE"

        unique_constraints = inspector.get_unique_constraints("webhook_delivery_jobs")
        event_id_unique_constraints = [
            constraint
            for constraint in unique_constraints
            if constraint["name"] == "uq_webhook_delivery_jobs_event_id"
        ]
        assert len(event_id_unique_constraints) == 1
        event_id_unique_constraint = event_id_unique_constraints[0]
        assert event_id_unique_constraint["column_names"] == ["event_id"]

        check_constraints = inspector.get_check_constraints("webhook_delivery_jobs")
        normalized_checks = {
            constraint["name"]: " ".join(str(constraint["sqltext"]).lower().split())
            for constraint in check_constraints
        }
        assert set(normalized_checks) == {
            "ck_webhook_delivery_jobs_status",
            "ck_webhook_delivery_jobs_status_next_attempt_at",
        }

        status_check = normalized_checks["ck_webhook_delivery_jobs_status"]
        assert "status" in status_check
        assert "pending" in status_check
        assert "processing" in status_check
        assert "succeeded" in status_check
        assert "dead_letter" in status_check

        status_next_attempt_at_check = normalized_checks[
            "ck_webhook_delivery_jobs_status_next_attempt_at"
        ]
        assert "status" in status_next_attempt_at_check
        assert "next_attempt_at" in status_next_attempt_at_check
        assert "pending" in status_next_attempt_at_check
        assert "processing" in status_next_attempt_at_check
        assert "succeeded" in status_next_attempt_at_check
        assert "dead_letter" in status_next_attempt_at_check
        assert "is not null" in status_next_attempt_at_check
        assert "is null" in status_next_attempt_at_check
        assert " or " in status_next_attempt_at_check

        indexes = inspector.get_indexes("webhook_delivery_jobs")
        assert all(index["name"] != "ix_webhook_delivery_jobs_event_id" for index in indexes)
        unexpected_indexes = [
            index
            for index in indexes
            if index.get("duplicates_constraint") != "uq_webhook_delivery_jobs_event_id"
        ]
        assert unexpected_indexes == []

        command.downgrade(alembic_config, "10f4dd620e97")

        downgraded_inspector = inspect(engine)
        assert downgraded_inspector.has_table("webhook_delivery_jobs") is False
        assert downgraded_inspector.has_table("webhook_delivery_attempts") is True
        assert downgraded_inspector.has_table("webhook_events") is True
        assert downgraded_inspector.has_table("webhook_endpoints") is True

        command.upgrade(alembic_config, "head")

        upgraded_inspector = inspect(engine)
        assert upgraded_inspector.has_table("webhook_delivery_jobs") is True
        assert upgraded_inspector.has_table("webhook_delivery_attempts") is True
        assert upgraded_inspector.has_table("webhook_events") is True
        assert upgraded_inspector.has_table("webhook_endpoints") is True
    finally:
        command.upgrade(alembic_config, "head")

    final_inspector = inspect(engine)
    assert final_inspector.has_table("webhook_delivery_jobs") is True
    assert final_inspector.has_table("webhook_delivery_attempts") is True
    assert final_inspector.has_table("webhook_events") is True
    assert final_inspector.has_table("webhook_endpoints") is True
