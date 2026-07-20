from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

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
