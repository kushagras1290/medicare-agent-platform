from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from medicare_agent.config import Settings, get_settings


class Base(DeclarativeBase):
    pass


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores foreign keys unless each connection opts in; PostgreSQL always enforces."""

    @event.listens_for(engine, "connect")
    def _enable(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def build_engine(settings: Settings) -> Engine:
    is_sqlite = settings.database_url.startswith("sqlite")
    connect_args: dict[str, object] = (
        {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    )
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if is_sqlite:
        enable_sqlite_foreign_keys(engine)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Build the process-wide engine lazily so importing this module has no side effects."""

    return build_engine(get_settings())


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def open_session() -> Session:
    return get_sessionmaker()()


def get_session() -> Generator[Session, None, None]:
    with open_session() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
