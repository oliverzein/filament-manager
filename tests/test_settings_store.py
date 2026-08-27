# tests/test_settings_store.py
"""settings_store module tests — path derivation, save/load round-trip,
build_config validation, and load_merged_config precedence.

All file operations use the pytest ``tmp_path`` fixture; env vars are scoped
with ``monkeypatch`` so no real environment is mutated.
"""
from __future__ import annotations

import pytest

from fm.config import Config
from fm.errors import FmError
from fm.settings_store import (
    build_config,
    load_merged_config,
    load_settings,
    save_settings,
    settings_path,
)

# A complete, valid settings dict matching the settings.json schema.
FULL = {
    "printer_ip": "192.168.0.101",
    "ssh_user": "root",
    "ssh_password": "s3cret",
    "db_remote_path": "/mnt/UDISK/creality/userdata/box/material_database.json",
    "ws_port": 9999,
    "version_override": 9876543210,
    "id_range_start": 99001,
    "agent_token": "tok-abc",
}

# Complete FM_* env vars (mirrors tests/test_config.py BASE + agent token).
ENV_FULL = {
    "FM_PRINTER_IP": "10.0.0.5",
    "FM_SSH_USER": "maker",
    "FM_SSH_PASSWORD": "envpw",
    "FM_DB_REMOTE_PATH": "/mnt/UDISK/x.json",
    "FM_AGENT_TOKEN": "envtok",
}


# ---------------------------------------------------------------------------
# settings_path() — env-driven derivation
# ---------------------------------------------------------------------------

def test_settings_path_explicit_env(tmp_path, monkeypatch):
    """FM_SETTINGS_PATH (absolute) is returned verbatim."""
    target = tmp_path / "custom.json"
    monkeypatch.setenv("FM_SETTINGS_PATH", str(target))
    assert settings_path() == target


def test_settings_path_from_docker_db_url(monkeypatch):
    """sqlite:////data/filaments.db → /data/settings.json."""
    monkeypatch.delenv("FM_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("FM_DATABASE_URL", "sqlite:////data/filaments.db")
    assert settings_path() == __import__("pathlib").Path("/data/settings.json")


def test_settings_path_from_relative_db_url(monkeypatch):
    """sqlite:///.local/filaments.db → .local/settings.json (relative)."""
    monkeypatch.delenv("FM_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("FM_DATABASE_URL", "sqlite:///.local/filaments.db")
    assert settings_path() == __import__("pathlib").Path(".local/settings.json")


def test_settings_path_fallback(monkeypatch):
    """No FM_SETTINGS_PATH and no FM_DATABASE_URL → ./settings.json."""
    monkeypatch.delenv("FM_SETTINGS_PATH", raising=False)
    monkeypatch.delenv("FM_DATABASE_URL", raising=False)
    assert settings_path() == __import__("pathlib").Path("settings.json")


# ---------------------------------------------------------------------------
# save_settings() + load_settings() — round-trip
# ---------------------------------------------------------------------------

def test_save_load_round_trip(tmp_path, monkeypatch):
    """A dict written via save_settings reads back identically via load_settings."""
    p = tmp_path / "settings.json"
    monkeypatch.setenv("FM_SETTINGS_PATH", str(p))
    save_settings(FULL)
    assert p.exists()
    assert load_settings() == FULL


def test_load_settings_missing_returns_none(tmp_path, monkeypatch):
    """load_settings returns None when no settings.json exists yet."""
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    assert load_settings() is None


# ---------------------------------------------------------------------------
# build_config() — validation + defaults
# ---------------------------------------------------------------------------

def test_build_config_valid():
    c = build_config(FULL)
    assert isinstance(c, Config)
    assert c.printer_ip == "192.168.0.101"
    assert c.ssh_user == "root"
    assert c.ssh_password == "s3cret"
    assert c.db_remote_path == FULL["db_remote_path"]
    assert c.ws_port == 9999
    assert c.version_override == 9876543210
    assert c.id_range_start == 99001
    assert c.agent_token == "tok-abc"


def test_build_config_optional_defaults():
    minimal = {k: FULL[k] for k in
               ("printer_ip", "ssh_user", "ssh_password", "db_remote_path", "agent_token")}
    c = build_config(minimal)
    assert c.ws_port == 9999
    assert c.version_override == 9876543210
    assert c.id_range_start == 99001


@pytest.mark.parametrize("missing", [
    "printer_ip", "ssh_user", "ssh_password", "db_remote_path", "agent_token",
])
def test_build_config_missing_required_raises(missing):
    data = {k: v for k, v in FULL.items() if k != missing}
    with pytest.raises(FmError):
        build_config(data)


def test_build_config_empty_required_raises():
    """An empty-string required value is treated as missing."""
    data = {**FULL, "ssh_password": ""}
    with pytest.raises(FmError):
        build_config(data)


# ---------------------------------------------------------------------------
# load_merged_config() — precedence + fallback
# ---------------------------------------------------------------------------

def test_merged_config_from_settings_file(tmp_path, monkeypatch):
    """settings.json present and complete → Config from file (ignores env)."""
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    save_settings(FULL)
    # Env vars absent — file must win.
    for k in ("FM_PRINTER_IP", "FM_SSH_USER", "FM_SSH_PASSWORD",
              "FM_DB_REMOTE_PATH", "FM_AGENT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    c = load_merged_config()
    assert c is not None
    assert c.printer_ip == "192.168.0.101"
    assert c.agent_token == "tok-abc"


def test_merged_config_from_env_when_no_file(tmp_path, monkeypatch):
    """No settings.json but env vars complete → Config from env."""
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    for k, v in ENV_FULL.items():
        monkeypatch.setenv(k, v)
    c = load_merged_config()
    assert c is not None
    assert c.printer_ip == "10.0.0.5"
    assert c.ssh_user == "maker"


def test_merged_config_neither_returns_none(tmp_path, monkeypatch):
    """Neither settings.json nor env vars → None."""
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    for k in ("FM_PRINTER_IP", "FM_SSH_USER", "FM_SSH_PASSWORD",
              "FM_DB_REMOTE_PATH", "FM_AGENT_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert load_merged_config() is None


def test_merged_config_incomplete_file_falls_back_to_env(tmp_path, monkeypatch):
    """settings.json incomplete + env vars complete → env Config."""
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    # File missing a required key.
    save_settings({k: v for k, v in FULL.items() if k != "ssh_password"})
    for k, v in ENV_FULL.items():
        monkeypatch.setenv(k, v)
    c = load_merged_config()
    assert c is not None
    # Env values, not file values.
    assert c.printer_ip == "10.0.0.5"
    assert c.ssh_password == "envpw"
