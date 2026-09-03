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
    """Add columns to existing tables (SQLite ALTER TABLE ADD COLUMN)."""
    with engine.connect() as conn:
        # sync_targets.cloud_id — added for Orca Cloud resource UUID tracking
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(sync_targets)"))}
        if "cloud_id" not in cols:
            conn.execute(text("ALTER TABLE sync_targets ADD COLUMN cloud_id VARCHAR(64)"))
            conn.commit()

        # profiles.template_override — added for preset template selection
        profile_cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
        if "template_override" not in profile_cols:
            conn.execute(text("ALTER TABLE profiles ADD COLUMN template_override VARCHAR(255)"))
            conn.commit()
