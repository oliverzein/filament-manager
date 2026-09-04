# tests/test_github_backup.py
"""GitHubBackupClient tests — Git Data API flow, error handling, test_connection."""
from __future__ import annotations
import json
from unittest.mock import patch, MagicMock

from datetime import datetime, timezone

from sqlalchemy import select

from fm.models import Profile, Job, ActivityLog
from fm.service import sync_handlers as sh

import pytest

from fm.github_backup import GitHubBackupClient, GitHubBackupError
from fm.settings_store import (
    load_github_config,
    save_github_config,
    load_settings,
    save_settings,
)


class TestBackupFlow:
    """Tests the full Git Data API flow with mocked requests."""

    def _mock_response(self, status_code=200, json_data=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = status_code < 400
        resp.text = text or json.dumps(json_data or {})
        resp.json.return_value = json_data or {}
        return resp

    def test_backup_creates_atomic_commit(self):
        """Full flow: ref → commit → blobs → tree → commit → ref-update."""
        client = GitHubBackupClient("ghp_fake", "owner/repo", "main")
        call_log = []

        def fake_request(method, url, **kwargs):
            call_log.append((method, url, kwargs))
            if "ref/heads/main" in url and method == "GET":
                return self._mock_response(200, {"object": {"sha": "refsha123"}})
            if "commits/refsha123" in url:
                return self._mock_response(200, {"tree": {"sha": "basesha456"}})
            if "blobs" in url and method == "POST":
                content = kwargs.get("json", {}).get("content", "")
                return self._mock_response(200, {"sha": "blob_" + content[:8]})
            if "trees" in url and method == "POST":
                return self._mock_response(200, {"sha": "newtreasha"})
            if "commits" in url and method == "POST" and "git/commits" in url:
                return self._mock_response(200, {"sha": "newcommitsha"})
            if "refs/heads/main" in url and method == "PATCH":
                return self._mock_response(200, {"object": {"sha": "newcommitsha"}})
            return self._mock_response(404, {"message": "not found"})

        with patch("fm.github_backup.requests.request", side_effect=fake_request):
            result = client.backup(
                {"presets/pla.json": '{"name": "PLA"}', "index.json": "[]"},
                "Backup: 2026-09-02 — 1 presets",
            )

        assert result["commit_sha"] == "newcommitsha"
        assert result["files_written"] == 2
        # Verify API call sequence
        methods = [c[0] for c in call_log]
        assert "GET" in methods  # ref + commit
        assert "POST" in methods  # blobs + tree + commit
        assert "PATCH" in methods  # ref update

    def test_backup_branch_not_found_raises(self):
        client = GitHubBackupClient("ghp_fake", "owner/repo", "nonexistent")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(404, {"message": "Not Found"})
            with pytest.raises(GitHubBackupError) as exc_info:
                client.backup({"index.json": "[]"}, "test")
            assert exc_info.value.status == 404

    def test_backup_token_invalid_raises(self):
        client = GitHubBackupClient("bad_token", "owner/repo", "main")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(401, {"message": "Bad credentials"})
            with pytest.raises(GitHubBackupError) as exc_info:
                client.backup({"index.json": "[]"}, "test")
            assert exc_info.value.status == 401


class TestTestConnection:
    def _mock_response(self, status_code=200, json_data=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.ok = status_code < 400
        resp.text = text or json.dumps(json_data or {})
        resp.json.return_value = json_data or {}
        return resp

    def test_test_connection_success(self):
        client = GitHubBackupClient("ghp_fake", "owner/repo", "main")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(200, {
                "full_name": "owner/repo",
                "default_branch": "main",
            })
            result = client.test_connection()
        assert result["repo"] == "owner/repo"
        assert result["branch"] == "main"
        assert result["default_branch"] == "main"

    def test_test_connection_repo_not_found(self):
        client = GitHubBackupClient("ghp_fake", "owner/nonexistent", "main")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(404, {"message": "Not Found"})
            with pytest.raises(GitHubBackupError) as exc_info:
                client.test_connection()
            assert exc_info.value.status == 404

    def test_test_connection_token_invalid(self):
        client = GitHubBackupClient("bad", "owner/repo", "main")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(401, {"message": "Bad credentials"})
            with pytest.raises(GitHubBackupError) as exc_info:
                client.test_connection()
            assert exc_info.value.status == 401


class TestGithubSettings:
    """Tests for load_github_config / save_github_config settings helpers."""

    def test_load_github_config_returns_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "s.json"))
        save_settings({"printer_ip": "1.2.3.4"})
        assert load_github_config() is None

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "s.json"))
        save_settings({"printer_ip": "1.2.3.4"})
        save_github_config("ghp_token123", "owner/repo", "main")
        result = load_github_config()
        assert result is not None
        token, repo, branch = result
        assert token == "ghp_token123"
        assert repo == "owner/repo"
        assert branch == "main"

    def test_load_defaults_branch_to_main(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "s.json"))
        save_settings({"github_token": "tok", "github_repo": "o/r"})
        result = load_github_config()
        assert result is not None
        assert result[2] == "main"  # branch defaults to main

    def test_save_merges_without_overwriting(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "s.json"))
        save_settings({"printer_ip": "1.2.3.4", "orca_user_id": "abc"})
        save_github_config("tok", "o/r", "dev")
        data = load_settings()
        assert data["printer_ip"] == "1.2.3.4"
        assert data["orca_user_id"] == "abc"
        assert data["github_token"] == "tok"
        assert data["github_repo"] == "o/r"
        assert data["github_branch"] == "dev"


def _make_profile(session, **overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        brand="Sunlu", name="Sunlu PLA+", type="PLA",
        colors='["#ffffff"]', min_temp=205, max_temp=215,
        density=1.23, drying_temp=50, drying_time=8,
        notes="", created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    p = Profile(**defaults)
    session.add(p)
    session.flush()
    return p


class TestHandleGithubBackup:
    def test_backup_success(self, inmemory_session, cfg, monkeypatch):
        """Handler pulls Orca Cloud presets, writes files, commits to GitHub."""
        s = inmemory_session

        # Mock Orca Cloud client
        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {
                    "id": "cloud-uuid-1",
                    "name": "Sunlu PLA+",
                    "content": {
                        "type": "filament",
                        "filament_id": "Pabc123",
                        "name": "Sunlu PLA+",
                        "filament_vendor": "Sunlu",
                        "filament_type": "PLA",
                        "nozzle_temperature": "210",
                        "updated_time": "2026-09-01T12:00:00Z",
                    },
                    "deleted": False,
                },
            ],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        # Mock GitHub client
        mock_github = MagicMock()
        mock_github.backup.return_value = {
            "commit_sha": "abc123def",
            "files_written": 2,
        }
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        result = sh.handle_github_backup(s, cfg, {})

        assert result["commit_sha"] == "abc123def"
        assert result["preset_count"] == 1
        assert result["files_written"] == 2

        # Verify backup was called with preset file + index.json
        call_args = mock_github.backup.call_args
        files = call_args[0][0]  # first positional arg
        assert "presets/Sunlu-PLA-.json" in files
        assert "index.json" in files

        # Handler no longer logs directly — run_pending logs on job completion.
        logs = list(s.scalars(select(ActivityLog)))
        assert len(logs) == 0

    def test_backup_github_not_configured(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)

        with pytest.raises(ValueError, match="GitHub not configured"):
            sh.handle_github_backup(s, cfg, {})

    def test_backup_orca_not_configured(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        mock_github = MagicMock()
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)

        with pytest.raises(ValueError, match="Orca Cloud not connected"):
            sh.handle_github_backup(s, cfg, {})

    def test_backup_empty_preset_list(self, inmemory_session, cfg, monkeypatch):
        """Zero presets → commit with empty index.json, no preset files."""
        s = inmemory_session

        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0, "upserts": [], "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "sha", "files_written": 1}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        result = sh.handle_github_backup(s, cfg, {})

        assert result["preset_count"] == 0
        call_args = mock_github.backup.call_args
        files = call_args[0][0]
        assert "index.json" in files
        assert len(files) == 1  # only index.json

    def test_backup_sanitizes_preset_names(self, inmemory_session, cfg, monkeypatch):
        """Special characters in preset names are replaced with '-'."""
        s = inmemory_session

        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {
                    "id": "uuid-1",
                    "name": "PLA: *Bad* / Name?",
                    "content": {
                        "type": "filament",
                        "filament_id": "P1",
                        "name": "PLA: *Bad* / Name?",
                        "filament_vendor": "X",
                        "filament_type": "PLA",
                        "nozzle_temperature": "200",
                        "updated_time": "",
                    },
                    "deleted": False,
                },
            ],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "sha", "files_written": 2}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        sh.handle_github_backup(s, cfg, {})

        call_args = mock_github.backup.call_args
        files = call_args[0][0]
        # All special chars replaced with -
        assert "presets/PLA---Bad----Name-.json" in files

    def test_backup_dedup_sanitized_names(self, inmemory_session, cfg, monkeypatch):
        """Two presets that sanitize to the same name get _0, _1 suffixes."""
        s = inmemory_session

        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {
                    "id": "uuid-1",
                    "name": "PLA+",
                    "content": {
                        "type": "filament",
                        "filament_id": "P1", "name": "PLA+",
                        "filament_vendor": "X", "filament_type": "PLA",
                        "nozzle_temperature": "200", "updated_time": "",
                    },
                    "deleted": False,
                },
                {
                    "id": "uuid-2",
                    "name": "PLA+",
                    "content": {
                        "type": "filament",
                        "filament_id": "P2", "name": "PLA+",
                        "filament_vendor": "Y", "filament_type": "PLA",
                        "nozzle_temperature": "210", "updated_time": "",
                    },
                    "deleted": False,
                },
            ],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        mock_github = MagicMock()
        mock_github.backup.return_value = {"commit_sha": "sha", "files_written": 3}
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        sh.handle_github_backup(s, cfg, {})

        call_args = mock_github.backup.call_args
        files = call_args[0][0]
        assert "presets/PLA-.json" in files
        assert "presets/PLA-_0.json" in files


from fastapi.testclient import TestClient
from fm.api.app import create_app, Settings


class TestUIRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, cfg):
        monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/gh_test.db")
        monkeypatch.setenv("FM_SETTINGS_PATH", str(tmp_path / "settings.json"))
        from fm.service import sync_handlers as sh
        monkeypatch.setattr(sh, "_transport_factory", lambda c: MagicMock())
        monkeypatch.setattr(sh, "_sleep", lambda s: None)
        with TestClient(create_app(Settings(config=cfg))) as c:
            yield c

    def test_backup_github_route_enqueues_job(self, client, monkeypatch):
        """POST /api/sync/backup-github enqueues and runs a github_backup job."""
        # Mark GitHub as configured so the button would show
        from fm.settings_store import save_settings
        save_settings({
            "printer_ip": "1.2.3.4", "ssh_user": "root",
            "ssh_password": "x", "db_remote_path": "/x",
            "github_token": "tok", "github_repo": "o/r",
        })
        # Mock the handler's external dependencies
        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0, "upserts": [], "deletes": [],
        }
        mock_github = MagicMock()
        mock_github.backup.return_value = {
            "commit_sha": "abc123", "files_written": 1,
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)
        r = client.post("/api/sync/backup-github")
        assert r.status_code == 200
        data = r.json()
        assert data["kind"] == "github_backup"
        assert data["status"] == "done"

    def test_github_test_connection_success(self, client, monkeypatch):
        """POST /api/settings/github-test returns repo info on success."""
        mock_github = MagicMock()
        mock_github.test_connection.return_value = {
            "repo": "owner/repo", "branch": "main", "default_branch": "main",
        }
        monkeypatch.setattr(
            "fm.api.ui.GitHubBackupClient", lambda *a, **kw: mock_github
        )
        r = client.post(
            "/api/settings/github-test",
            data={"github_token": "tok", "github_repo": "owner/repo"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["repo"] == "owner/repo"

    def test_github_test_connection_failure(self, client, monkeypatch):
        """POST /api/settings/github-test returns error on failure."""
        from fm.github_backup import GitHubBackupError
        mock_github = MagicMock()
        mock_github.test_connection.side_effect = GitHubBackupError(
            404, "Not Found", "repo not found",
        )
        monkeypatch.setattr(
            "fm.api.ui.GitHubBackupClient", lambda *a, **kw: mock_github
        )
        r = client.post(
            "/api/settings/github-test",
            data={"github_token": "tok", "github_repo": "owner/bad"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_settings_page_shows_github_fields(self, client):
        """Settings page renders GitHub Backup fieldset."""
        r = client.get("/settings")
        assert r.status_code == 200
        assert b"GitHub" in r.content
        assert b"github_token" in r.content
        assert b"github_repo" in r.content

    def test_settings_save_persists_github_fields(self, client, monkeypatch):
        """POST /settings saves GitHub token/repo/branch."""
        r = client.post("/settings", data={
            "printer_ip": "1.2.3.4", "ssh_user": "root",
            "ssh_password": "x", "db_remote_path": "/x",
            "github_token": "ghp_saved", "github_repo": "me/repo",
            "github_branch": "dev",
        }, follow_redirects=False)
        assert r.status_code == 303
        from fm.settings_store import load_settings
        data = load_settings()
        assert data["github_token"] == "ghp_saved"
        assert data["github_repo"] == "me/repo"
        assert data["github_branch"] == "dev"

    def test_sync_page_shows_backup_button_when_configured(self, client, monkeypatch):
        """Sync page shows 'Backup to GitHub' button when GitHub is configured."""
        from fm.settings_store import save_settings
        save_settings({
            "printer_ip": "1.2.3.4", "ssh_user": "root",
            "ssh_password": "x", "db_remote_path": "/x",
            "github_token": "tok", "github_repo": "o/r",
        })
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"Backup to GitHub" in r.content

    def test_sync_page_disables_backup_button_when_unconfigured(self, client):
        """Sync page shows 'Backup to GitHub' disabled when GitHub is not configured."""
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"Backup to GitHub" in r.content
        assert b"disabled" in r.content
