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
            # Enforce foreign keys like Postgres does, so dev/tests catch
            # orphaned-row bugs before production (SQLite's default is off).
            cur.execute("PRAGMA foreign_keys=ON")
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


# Arbitrary constant for pg_advisory_lock: serializes boot-time migrations
# when several web workers and the RQ worker start at the same moment.
_MIGRATION_LOCK_ID = 7_263_001


def _alembic_config():
    from pathlib import Path

    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent  # backend/
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["configure_logger"] = False  # keep the app's logging setup
    # ConfigParser treats "%" as interpolation (URL-encoded passwords/paths).
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    return cfg


def migrate() -> str:
    """Bring a non-SQLite database to the latest Alembic revision.

    - Fresh database             → `alembic upgrade head` (creates everything)
    - Already under Alembic      → `alembic upgrade head` (applies new revisions)
    - Tables but no version table (created by an older build's create_all)
                                 → create any missing tables, then stamp head
    Returns what happened, for the boot log."""
    from alembic import command
    from sqlalchemy import inspect, text

    from . import models  # noqa: F401

    cfg = _alembic_config()
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_ID})
        try:
            tables = set(inspect(conn).get_table_names())
            if tables and "alembic_version" not in tables:
                Base.metadata.create_all(bind=conn)
                conn.commit()
                command.stamp(cfg, "head")
                return "stamped existing schema at head"
            command.upgrade(cfg, "head")
            return "upgraded to head"
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_ID})
            conn.commit()


def init_db() -> None:
    # Importing models registers them on Base.metadata.
    from . import models  # noqa: F401

    if engine.dialect.name == "postgresql":
        # Production path: real migrations, never create_all drift.
        import logging

        logging.getLogger("avs.db").info("database: %s", migrate())
        return
    # SQLite (dev / tests): create_all is fast and fine for a single file.
    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
