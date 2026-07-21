from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from reliable_webhook_service.config import Settings


class Base(DeclarativeBase):
    pass


settings = Settings()

engine: Engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionFactory: sessionmaker[Session] = sessionmaker(
    bind=engine,
    class_=Session,
    expire_on_commit=False,
)


def get_session() -> Iterator[Session]:
    with SessionFactory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
