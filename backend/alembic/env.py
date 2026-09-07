"""Alembic environment.

Two things differ from the generated default:

  1. The URL comes from app settings, not alembic.ini, so the DB path is
     defined in exactly one place.
  2. render_as_batch=True — SQLite cannot ALTER TABLE DROP COLUMN or alter a
     column type, so without batch mode any migration beyond "add column"
     fails. Batch mode rewrites the table instead.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.core.db import Base

# Registers every table on Base.metadata.
import app.models  # noqa: F401  isort:skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().sync_database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    settings = get_settings()
    settings.db_file.parent.mkdir(parents=True, exist_ok=True)

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Foreign keys must be OFF for the duration of a migration on SQLite.
        # batch_alter_table rebuilds a table by create-copy-drop-rename, and
        # with enforcement on, DROP TABLE agents fails because tasks references
        # it — the migration then dies partway with the schema half-changed.
        # Integrity is re-checked below once the rebuild is finished; the
        # application's own connections always run with foreign_keys=ON.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLite takes Alembic's non-transactional DDL path, so CREATE TABLE
        # auto-commits but the alembic_version INSERT does not. Without this
        # commit `upgrade head` appears to succeed while leaving the database
        # unstamped, and the next upgrade tries to recreate existing tables.
        connection.commit()

        # Now that the rebuilds are done, prove the migration did not leave a
        # dangling reference behind. Turning enforcement off for the rebuild is
        # standard on SQLite; skipping this check afterwards is not.
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if violations:
            raise RuntimeError(
                f"migration left {len(violations)} foreign key violation(s): {violations[:5]}"
            )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
