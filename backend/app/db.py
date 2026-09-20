"""Engine, session factory, and the startup migration.

The engine is created once at import time from `settings.database_url()`, same as every other
module in this service. `/healthz` deliberately never imports anything that touches this
engine: it is an ECS container health check, and a check that fails when the database blips
would cause ECS to kill tasks that are otherwise fine.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.config import settings

engine = create_engine(settings.database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Arbitrary but fixed: every task must agree on the same number for the lock to serialise
# anything. Picked to be unlikely to collide with a lock some other part of the stack takes.
_MIGRATION_LOCK_ID = 0x646F6369


def run_migrations() -> None:
    """Run Alembic to head, holding a PostgreSQL advisory lock for the duration.

    Every ECS task runs this at startup, because the database has no public path and CI
    cannot migrate it (see ARCHITECTURE section 10). Concurrent tasks starting together would
    otherwise all run `alembic upgrade head` at once and fight over the same DDL. The lock
    serialises them: one task migrates, the rest block on `pg_advisory_lock`, then find the
    schema already at head and return immediately.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    alembic_cfg = Config(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "alembic"))

    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": _MIGRATION_LOCK_ID}
        )
        try:
            command.upgrade(alembic_cfg, "head")
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": _MIGRATION_LOCK_ID}
            )


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
