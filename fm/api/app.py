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

import json
import os
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..settings_store import load_merged_config
from ..db import init_engine
from . import catalog, sync, ui

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the app factory.

    ``database_url`` overrides ``FM_DATABASE_URL`` when provided.
    ``config`` overrides the printer ``Config`` (otherwise loaded from
    ``settings.json`` or ``FM_*`` env vars); used by tests.  When ``None``
    and neither source is complete, sync endpoints return 503.
    """

    database_url: str | None = None
    config: Config | None = None


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("filament-manager")
    except PackageNotFoundError:
        return "0.1.0"

_STATUS_CLASS_MAP = {
    "drift": "drifted",
    "done": "completed",
}


def status_class(value: str) -> str:
    """Map internal status values to app.css badge class names.

    Only ``drift`` → ``drifted`` and ``done`` → ``completed`` need mapping;
    all other status values (in_sync, pending, error, modified, missing,
    new, stock, queued, running, awaiting_input) have direct CSS classes.
    """
    return _STATUS_CLASS_MAP.get(value, value)


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
        app_config = load_merged_config()

    app = FastAPI(title="Filament Manager", version=_package_version())

    # Templates + static (dirs exist as placeholders; populated by Task 13/14).
    templates = Jinja2Templates(directory=str(_DIR / "templates"))
    # Parse Profile.colors (JSON string) -> list for template rendering.
    templates.env.filters["colors_list"] = lambda v: json.loads(v) if isinstance(v, str) else (v or [])
    templates.env.filters["from_json"] = lambda v: json.loads(v) if isinstance(v, str) else (v or {})
    templates.env.filters["status_class"] = status_class
    app.state.templates = templates
    app.state.config = app_config
    app.mount("/static", StaticFiles(directory=str(_DIR / "static")), name="static")

    app.include_router(catalog.router)
    app.include_router(sync.router)
    app.include_router(ui.router)
    @app.get("/api/version", tags=["meta"])
    def api_version() -> dict[str, str]:
        return {"version": _package_version()}

    return app
