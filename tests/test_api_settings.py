# tests/test_api_settings.py
"""Settings UI endpoint tests — GET /settings render, POST /settings save +
redirect, validation error re-render, and live config update after save.

Uses fastapi.testclient.TestClient with create_app(Settings(...)). The
FM_SETTINGS_PATH env var is pointed at a tmp_path file so settings.json
persistence is verifiable without touching the real filesystem.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from fm.api.app import Settings, create_app

# Form payload with all required fields + optional overrides.
GOOD_FORM = {
    "printer_ip": "192.168.0.101",
    "ssh_user": "root",
    "ssh_password": "s3cret",
    "db_remote_path": "/mnt/UDISK/creality/userdata/box/material_database.json",
    "ws_port": "9999",
    "version_override": "9876543210",
    "id_range_start": "99001",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App with no pre-existing config; settings.json written to tmp_path."""
    settings_file = tmp_path / "settings.json"
    monkeypatch.setenv("FM_SETTINGS_PATH", str(settings_file))
    # Ensure no FM_* printer env leaks in (config starts as None).
    for k in ("FM_PRINTER_IP", "FM_SSH_USER", "FM_SSH_PASSWORD",
              "FM_DB_REMOTE_PATH"):
        monkeypatch.delenv(k, raising=False)
    app = create_app(Settings(database_url=f"sqlite:///{tmp_path}/test.db"))
    c = TestClient(app)
    c.settings_file = settings_file  # type: ignore[attr-defined]
    c.app = app  # type: ignore[attr-defined]
    yield c


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------

def test_get_settings_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert b"Settings" in r.content


def test_get_settings_has_form_fields(client):
    r = client.get("/settings")
    assert b"name=\"printer_ip\"" in r.content
    assert b"name=\"ssh_password\"" in r.content


def test_get_settings_shows_warning_when_unconfigured(client):
    """With no config, the page warns the printer is not configured."""
    r = client.get("/settings")
    assert b"not configured" in r.content.lower() or b"not-configured" in r.content.lower()


# ---------------------------------------------------------------------------
# POST /settings — success path
# ---------------------------------------------------------------------------

def test_post_settings_success_redirects(client):
    r = client.post("/settings", data=GOOD_FORM, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/settings?saved=1"


def test_post_settings_follow_redirect_shows_success(client):
    r = client.post("/settings", data=GOOD_FORM, follow_redirects=True)
    assert r.status_code == 200
    # Success banner present after the redirect.
    assert b"saved" in r.content.lower() or b"success" in r.content.lower()


def test_post_settings_updates_app_config(client):
    """After a successful save, app.state.config is populated."""
    assert client.app.state.config is None
    client.post("/settings", data=GOOD_FORM, follow_redirects=True)
    cfg = client.app.state.config
    assert cfg is not None
    assert cfg.printer_ip == "192.168.0.101"
    assert cfg.ssh_user == "root"


def test_post_settings_persists_settings_json(client):
    """settings.json is written to the FM_SETTINGS_PATH location."""
    assert not client.settings_file.exists()
    client.post("/settings", data=GOOD_FORM, follow_redirects=True)
    assert client.settings_file.exists()
    data = json.loads(client.settings_file.read_text())
    assert data["printer_ip"] == "192.168.0.101"
    assert data["ssh_password"] == "s3cret"


# ---------------------------------------------------------------------------
# POST /settings — validation error
def test_post_settings_missing_password_re_renders(client):
    """Missing required ssh_password → 422 (form re-rendered with errors), no redirect.

    Matches the codebase convention: profile_create_form also returns 422 on
    validation errors while re-rendering the template.
    """
    bad = {k: v for k, v in GOOD_FORM.items() if k != "ssh_password"}
    r = client.post("/settings", data=bad, follow_redirects=False)
    assert r.status_code == 422
    # No redirect location header.
    assert "location" not in r.headers
    # Form re-rendered (page still shows Settings + the error).
    assert b"Settings" in r.content
    assert b"SSH password is required" in r.content


def test_post_settings_validation_error_does_not_save(client):
    """A failed validation must not write settings.json or set app config."""
    bad = {k: v for k, v in GOOD_FORM.items() if k != "ssh_password"}
    client.post("/settings", data=bad, follow_redirects=False)
    assert not client.settings_file.exists()
    assert client.app.state.config is None


# ---------------------------------------------------------------------------
# Orca Cloud connect / disconnect
# ---------------------------------------------------------------------------

class TestOrcaConnect:
    def test_orca_connect_saves_token(self, client):
        """POST /api/settings/orca-connect logs in with email+password and saves token."""
        with patch("fm.api.ui.OrcaCloudClient") as MockClient:
            MockClient.login_with_password.return_value = ("test-refresh-token", "user-uuid-123")
            r = client.post("/api/settings/orca-connect",
                            data={"email": "user@example.com", "password": "secret"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        data = json.loads(client.settings_file.read_text())
        assert data["orca_refresh_token"] == "test-refresh-token"
        assert data["orca_user_id"] == "user-uuid-123"

    def test_orca_connect_invalid_credentials_returns_error(self, client):
        """Invalid credentials return an error response."""
        from fm.orca_cloud import OrcaCloudError
        with patch("fm.api.ui.OrcaCloudClient") as MockClient:
            MockClient.login_with_password.side_effect = OrcaCloudError(401, "bad credentials")
            r = client.post("/api/settings/orca-connect",
                            data={"email": "user@example.com", "password": "wrong"})
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "error" in r.json()

    def test_orca_connect_missing_fields_returns_error(self, client):
        """Missing email or password → error response."""
        r = client.post("/api/settings/orca-connect", data={})
        assert r.json()["ok"] is False


class TestOrcaDisconnect:
    def test_orca_disconnect_clears_token(self, client):
        """POST /api/settings/orca-disconnect clears the Orca Cloud token."""
        from fm.settings_store import save_orca_token
        save_orca_token("test-refresh-token", "user-uuid-123")
        r = client.post("/api/settings/orca-disconnect")
        assert r.status_code == 200
        assert r.json()["ok"] is True
        data = json.loads(client.settings_file.read_text())
        assert not data.get("orca_refresh_token")
        assert not data.get("orca_user_id")
