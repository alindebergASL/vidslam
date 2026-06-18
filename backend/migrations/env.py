"""Alembic environment.

Wired into the app so:
  - DATABASE_URL comes from app.config.get_settings() (and therefore .env),
    not from alembic.ini — so `alembic upgrade head` in CI/prod uses the
    exact same connection string the app does.
  - target_metadata is app.db.Base.metadata after importing app.models,
    so `alembic revision --autogenerate` sees every table.
  - For SQLite, render_as_batch=True is enabled — required because SQLite
    has no native ALTER TABLE for column drops/type-changes.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Allow `from app...` imports when alembic is invoked from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app import models  # noqa: F401,E402  — register tables on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override whatever's in alembic.ini (which we intentionally left blank).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def _is_sqlite() -> bool:
    return get_settings().database_url.startswith("sqlite")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
