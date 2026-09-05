# tests/test_settings_ui.py
"""Settings page persistence tests — POST /settings must merge onto existing
settings.json instead of replacing it, so Orca Cloud tokens and the sync
cursor survive a save.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from fm.settings_store import load_settings, save_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui.db")
    monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
    from fm.api.app import create_app
    with TestClient(create_app()) as c:
        yield c


_VALID_PRINTER = {
    "printer_ip": "192.168.0.42",
    "ssh_user": "root",
    "ssh_password": "hunter2",
    "db_remote_path": "/home/root/printer.db",
}


def _read_settings(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_settings_save_preserves_orca_tokens_and_cursor(client, tmp_path):
    """Pre-existing orca_refresh_token, orca_user_id, and orca_sync_cursor
    must survive a POST /settings that only submits printer fields.
    """
    save_settings({
        **_VALID_PRINTER,
        "orca_refresh_token": "rt-pre-existing",
        "orca_user_id": "uid-pre-existing",
        "orca_sync_cursor": 12345,
    })

    r = client.post("/settings", data={
        **_VALID_PRINTER,
        "ws_port": "9999",
        "version_override": "9876543210",
        "id_range_start": "99001",
    }, follow_redirects=False)

    assert r.status_code == 303
    stored = _read_settings(tmp_path)
    assert stored["orca_refresh_token"] == "rt-pre-existing"
    assert stored["orca_user_id"] == "uid-pre-existing"
    assert stored["orca_sync_cursor"] == 12345


def test_settings_save_connects_orca_via_form(client, tmp_path, monkeypatch):
    """Submitting orca_email + orca_password to POST /settings performs a
    password login and stores the returned tokens.
    """
    save_settings(_VALID_PRINTER)

    monkeypatch.setattr(
        "fm.api.ui.OrcaCloudClient.login_with_password",
        classmethod(lambda cls, email, password: ("rt-new", "uid-new")),
    )

    r = client.post("/settings", data={
        **_VALID_PRINTER,
        "orca_email": "me@example.com",
        "orca_password": "secret",
    }, follow_redirects=False)

    assert r.status_code == 303
    stored = _read_settings(tmp_path)
    assert stored["orca_refresh_token"] == "rt-new"
    assert stored["orca_user_id"] == "uid-new"


def test_settings_save_clearing_github_preserves_orca(client, tmp_path):
    """An empty github_token must remove the GitHub keys while keeping any
    Orca Cloud credentials intact.
    """
    save_settings({
        **_VALID_PRINTER,
        "github_token": "ghp_old",
        "github_repo": "owner/repo",
        "github_branch": "main",
        "orca_refresh_token": "rt-keep",
        "orca_user_id": "uid-keep",
        "orca_sync_cursor": 99,
    })

    r = client.post("/settings", data={
        **_VALID_PRINTER,
        "github_token": "",
        "github_repo": "",
        "github_branch": "main",
    }, follow_redirects=False)

    assert r.status_code == 303
    stored = _read_settings(tmp_path)
    assert "github_token" not in stored
    assert "github_repo" not in stored
    assert "github_branch" not in stored
    assert stored["orca_refresh_token"] == "rt-keep"
    assert stored["orca_user_id"] == "uid-keep"
    assert stored["orca_sync_cursor"] == 99
