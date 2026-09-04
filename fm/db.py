# fm/db.py
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


class _Session(Session):
    """Session that commits on clean context-manager exit and rolls back on
    exception, then closes. Lets `with SessionLocal() as s:` blocks persist
    their work without an explicit `s.commit()`."""

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            super().__exit__(exc_type, exc_val, exc_tb)


# A module-level sessionmaker, initially unbound. `init_engine` reconfigures
# it in place (via `.configure`) so callers that did `from fm.db import
# SessionLocal` keep a live reference after the engine is created.
SessionLocal = sessionmaker(class_=_Session, future=True, expire_on_commit=False)


def init_engine(url: str) -> Engine:
    """Create the SQLAlchemy engine for ``url``, create all tables, and bind
    the module-level ``SessionLocal`` sessionmaker to it.

    Returns the engine so callers may dispose it explicitly.
    """
    engine = create_engine(url, future=True, connect_args={"timeout": 30})
    Base.metadata.create_all(engine)
    _migrate(engine)
    SessionLocal.configure(bind=engine)
    return engine


def _migrate(engine: Engine) -> None:
    """Add columns to existing tables and run safe schema migration."""
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        # sync_targets.cloud_id — added for Orca Cloud resource UUID tracking
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_targets)"))}
        if "cloud_id" not in cols:
            conn.execute(text("ALTER TABLE sync_targets ADD COLUMN cloud_id VARCHAR(64)"))
            conn.commit()

        # sync_targets.printer_db_id — "99xxx" printer DB row this target mirrors
        if "printer_db_id" not in cols:
            conn.execute(text("ALTER TABLE sync_targets ADD COLUMN printer_db_id VARCHAR(16)"))
            conn.commit()

        # Migrate printer_db sync_targets: copy the Orca Cloud UUID from the
        # sibling orca_preset target and the printer_db_id from the profile so
        # the printer DB → cloud mapping is retained.
        conn.execute(text("""
            UPDATE sync_targets
            SET cloud_id = (
                SELECT o.cloud_id FROM sync_targets o
                WHERE o.profile_id = sync_targets.profile_id AND o.kind = 'orca_preset'
            ),
            printer_db_id = (
                SELECT p.printer_db_id FROM profiles p
                WHERE p.id = sync_targets.profile_id
            )
            WHERE kind = 'printer_db' AND (cloud_id IS NULL OR printer_db_id IS NULL)
        """))
        conn.commit()

        # spools.cloud_resource_id — points at profile_cache so spools keep their
        # name/metadata even after a preset is soft-deleted from the cloud.
        spool_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(spools)"))}
        if "cloud_resource_id" not in spool_cols:
            conn.execute(text("ALTER TABLE spools ADD COLUMN cloud_resource_id VARCHAR(64)"))
            conn.commit()

        # Backfill spools.cloud_resource_id from the sibling orca_preset target.
        conn.execute(text("""
            UPDATE spools
            SET cloud_resource_id = (
                SELECT st.cloud_id FROM sync_targets st
                WHERE st.profile_id = spools.profile_id AND st.cloud_id IS NOT NULL
            )
            WHERE cloud_resource_id IS NULL AND profile_id IS NOT NULL
        """))
        conn.commit()

        # profiles.template_override — added for preset template selection
        profile_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
        if "template_override" not in profile_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN template_override VARCHAR(255)"))
            conn.commit()
