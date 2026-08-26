# fm/api/app.py
"""FastAPI application factory.

``create_app()`` binds a SQLAlchemy engine (reading ``FM_DATABASE_URL`` from the
environment, defaulting to ``sqlite:///./filaments.db`` for local dev), mounts
the catalog + sync routers, Jinja2 templates, and a static-files directory, and
exposes a ``GET /api/version`` health endpoint.  Designed as a uvicorn factory
target::

    uvicorn fm.api.app:create_app --factory
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config, load_config
from ..db import init_engine
from ..errors import FmError
from . import agent, catalog, sync, ui

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the app factory.

    ``database_url`` overrides ``FM_DATABASE_URL`` when provided.
    ``config`` overrides the printer ``Config`` (otherwise loaded from FM_*
    env vars); used by tests.  When ``None`` and env vars are missing, sync
    endpoints return 503.
    """

    database_url: str | None = None
    config: Config | None = None


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("filament-manager")
    except PackageNotFoundError:
        return "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    With no arguments (the uvicorn ``--factory`` path) the database URL is read
    from ``FM_DATABASE_URL`` (default ``sqlite:///./filaments.db``).  Passing
    ``Settings(database_url=...)`` overrides the env var — used by tests.
    """
    db_url = os.environ.get("FM_DATABASE_URL", "sqlite:///./filaments.db")
    if settings is not None and settings.database_url:
        db_url = settings.database_url
    init_engine(db_url)

    # Printer config for sync handlers — explicit override, else try env.
    app_config: Config | None = None
    if settings is not None and settings.config:
        app_config = settings.config
    else:
        try:
            app_config = load_config()
        except FmError:
            app_config = None

    app = FastAPI(title="Filament Manager", version=_package_version())

    # Templates + static (dirs exist as placeholders; populated by Task 13/14).
    templates = Jinja2Templates(directory=str(_DIR / "templates"))
    app.state.templates = templates
    app.state.config = app_config
    app.mount("/static", StaticFiles(directory=str(_DIR / "static")), name="static")

    app.include_router(catalog.router)
    app.include_router(sync.router)
    app.include_router(agent.router)
    app.include_router(ui.router)
    @app.get("/api/version", tags=["meta"])
    def api_version() -> dict[str, str]:
        return {"version": _package_version()}

    return app
