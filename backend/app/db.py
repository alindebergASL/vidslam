from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args, future=True)


settings = get_settings()
engine = _make_engine(settings.database_url)
# SessionLocal is a sessionmaker shared by reference across importers (workers,
# seed, get_db). reset_engine() recreates the engine and rebinds it *in place*
# via .configure(), so every holder picks up the new engine without reassignment.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def reset_engine(database_url: str | None = None) -> None:
    """Rebind the engine + SessionLocal to a (possibly new) database URL. Primarily
    used by tests for per-test isolation; safe to call at runtime too."""
    global engine
    url = database_url or get_settings().database_url
    try:
        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    engine = _make_engine(url)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    # Importing models registers them on Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
