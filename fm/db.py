# fm/db.py
from sqlalchemy import Engine, create_engine
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
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)
    SessionLocal.configure(bind=engine)
    return engine
