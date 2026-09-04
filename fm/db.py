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

        # sync_targets.profile_id — make nullable for cloud-first SyncTargets
        # that have no Profile to link to.  SQLite cannot ALTER column
        # constraints in-place, so we recreate the table when needed.
        st_cols = list(conn.execute(text("PRAGMA table_info(sync_targets)")))
        profile_id_col = next(
            (r for r in st_cols if r[1] == "profile_id"), None
        )
        if profile_id_col is not None and profile_id_col[3] == 1:
            # notnull == 1 → recreate table with profile_id nullable
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.execute(text("""
                CREATE TABLE sync_targets_temp (
                    id INTEGER PRIMARY KEY,
                    profile_id INTEGER,
                    kind VARCHAR(32),
                    cloud_id VARCHAR(64),
                    expected_hash VARCHAR,
                    actual_state VARCHAR,
                    status VARCHAR(16) DEFAULT 'pending',
                    last_synced_at DATETIME,
                    printer_db_id VARCHAR(16)
                )
            """))
            conn.execute(text("""
                INSERT INTO sync_targets_temp
                (id, profile_id, kind, cloud_id, expected_hash, actual_state, status, last_synced_at, printer_db_id)
                SELECT id, profile_id, kind, cloud_id, expected_hash, actual_state, status, last_synced_at, printer_db_id
                FROM sync_targets
            """))
            conn.execute(text("DROP TABLE sync_targets"))
            conn.execute(text("ALTER TABLE sync_targets_temp RENAME TO sync_targets"))
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.commit()

        # spools.profile_id — make nullable for cloud-first spools that link
        # via cloud_resource_id instead of a local Profile.  SQLite cannot
        # ALTER column constraints in-place, so recreate the table when needed.
        sp_cols = list(conn.execute(text("PRAGMA table_info(spools)")))
        sp_pid_col = next(
            (r for r in sp_cols if r[1] == "profile_id"), None
        )
        if sp_pid_col is not None and sp_pid_col[3] == 1:
            # notnull == 1 → recreate table with profile_id nullable
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.execute(text("""
                CREATE TABLE spools_temp (
                    id INTEGER PRIMARY KEY,
                    profile_id INTEGER,
                    color_override VARCHAR,
                    remaining_pct INTEGER DEFAULT 100,
                    location VARCHAR DEFAULT '',
                    rfid_tag VARCHAR,
                    notes VARCHAR DEFAULT '',
                    created_at DATETIME,
                    updated_at DATETIME,
                    cloud_resource_id VARCHAR(64)
                )
            """))
            conn.execute(text("""
                INSERT INTO spools_temp
                (id, profile_id, color_override, remaining_pct, location, rfid_tag, notes, created_at, updated_at, cloud_resource_id)
                SELECT id, profile_id, color_override, remaining_pct, location, rfid_tag, notes, created_at, updated_at, cloud_resource_id
                FROM spools
            """))
            conn.execute(text("DROP TABLE spools"))
            conn.execute(text("ALTER TABLE spools_temp RENAME TO spools"))
            conn.execute(text("PRAGMA foreign_keys = ON"))
            conn.commit()
