from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if not url.startswith("sqlite"):
        # Postgres etc.: pre-ping drops connections a restart left dead.
        return create_engine(url, future=True, pool_pre_ping=True)
    # SQLite is shared by several web workers and the RQ worker. WAL lets
    # readers run alongside a writer, and busy_timeout makes a writer wait
    # for the lock instead of failing at once with "database is locked".
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30}, future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
        finally:
            cur.close()

    return engine


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
