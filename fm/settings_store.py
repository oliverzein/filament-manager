# fm/settings_store.py
"""Persistent printer-config store backed by ``settings.json``.

The settings file lives next to the local SQLite database (or at an explicit
``FM_SETTINGS_PATH``), so a single directory holds both the app data and the
printer connection details.  ``load_merged_config`` prefers the settings file
and falls back to the legacy ``FM_*`` env vars, returning ``None`` when neither
source can produce a complete :class:`Config`.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

from .config import Config, load_config
from .errors import FmError

_REQUIRED = ("printer_ip", "ssh_user", "ssh_password", "db_remote_path")


def settings_path() -> Path:
    """Resolve the location of ``settings.json``.

    1. ``FM_SETTINGS_PATH`` env var (absolute, or relative to CWD).
    2. Directory of the SQLite file named by ``FM_DATABASE_URL``
       (e.g. ``sqlite:////data/filaments.db`` -> ``/data/settings.json``,
       ``sqlite:///.local/filaments.db`` -> ``.local/settings.json``).
    3. Fallback: ``./settings.json``.
    """
    explicit = os.environ.get("FM_SETTINGS_PATH")
    if explicit:
        return Path(explicit)

    db_url = os.environ.get("FM_DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        db_file = db_url[len("sqlite:///"):]
        # Leading slashes are significant on Linux (absolute paths); Path
        # handles them correctly, e.g. "/data/filaments.db" -> /data.
        return Path(db_file).parent / "settings.json"

    return Path("settings.json")


def load_settings() -> dict | None:
    """Read ``settings.json``; return a dict or ``None`` if absent/invalid."""
    path = settings_path()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def save_settings(data: dict) -> None:
    """Write ``settings.json`` atomically (temp file + ``os.replace``)."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Named temp file in the same directory guarantees os.replace is atomic.
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_config(data: dict) -> Config:
    """Build a :class:`Config` from a settings dict.

    Required keys: ``printer_ip``, ``ssh_user``, ``ssh_password``,
    ``db_remote_path``.  Optional keys: ``ws_port`` (9999), ``version_override`` (9876543210),
    ``id_range_start`` (99001), ``orca_refresh_token`` (""),
    ``orca_user_id`` (""), ``orca_sync_cursor`` (0).
    Raises :class:`FmError` if any required key is missing or empty.
    """
    missing = [k for k in _REQUIRED if not data.get(k)]
    if missing:
        raise FmError(1, f"missing required settings: {', '.join(missing)}")
    return Config(
        printer_ip=data["printer_ip"],
        ssh_user=data["ssh_user"],
        ssh_password=data["ssh_password"],
        db_remote_path=data["db_remote_path"],
        ws_port=int(data.get("ws_port", 9999)),
        version_override=int(data.get("version_override", 9876543210)),
        id_range_start=int(data.get("id_range_start", 99001)),
    )


def load_merged_config(env: Mapping[str, str] = os.environ) -> Config | None:
    """Load config from ``settings.json`` first, then env vars.

    Returns a :class:`Config` or ``None`` if neither source has all required
    fields.  A present-but-incomplete settings file falls through to the env
    var path rather than raising.
    """
    data = load_settings()
    if data is not None:
        try:
            return build_config(data)
        except FmError:
            pass  # incomplete settings file — try env vars instead
    try:
        return load_config(env)
    except FmError:
        return None


def load_orca_token() -> tuple[str, str] | None:
    """Return ``(refresh_token, user_id)`` from settings.json, or ``None``."""
    data = load_settings()
    if data is None:
        return None
    refresh_token = data.get("orca_refresh_token", "")
    user_id = data.get("orca_user_id", "")
    if not refresh_token:
        return None
    return refresh_token, user_id


def save_orca_token(refresh_token: str, user_id: str) -> None:
    """Merge Orca Cloud token + user id into existing settings.json."""
    data = load_settings() or {}
    data["orca_refresh_token"] = refresh_token
    data["orca_user_id"] = user_id
    save_settings(data)


def load_orca_cursor() -> int:
    """Return the stored sync cursor (0 if unset)."""
    data = load_settings()
    if data is None:
        return 0
    try:
        return int(data.get("orca_sync_cursor", 0))
    except (TypeError, ValueError):
        return 0


def save_orca_cursor(cursor: int) -> None:
    """Persist the sync cursor into settings.json."""
    data = load_settings() or {}
    data["orca_sync_cursor"] = cursor
    save_settings(data)
