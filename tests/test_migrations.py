from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, String, inspect
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
