# tests/test_github_backup.py
"""GitHubBackupClient tests — Git Data API flow, error handling, test_connection."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

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
