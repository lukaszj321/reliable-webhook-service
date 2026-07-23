from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, Integer, String, Text, inspect
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
