from sqlalchemy import text

from reliable_webhook_service.database import SessionFactory


def test_database_connection() -> None:
    with SessionFactory() as session:
        result = session.scalar(text("SELECT 1"))

    assert result == 1
