# GitHub Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Back up all Orca Cloud filament presets to a private GitHub repo as version-controlled JSON files, triggered manually and automatically after sync operations.

**Architecture:** A new `GitHubBackupClient` (HTTP-only, Git Data API) commits preset files + `index.json` atomically. A `github_backup` job handler pulls from Orca Cloud, builds the file dict, and calls the client. Auto-trigger fires after `generate_preset` and `temp_sync` handlers succeed. Settings stored in `settings.json`; UI on Settings + Sync pages.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX, `requests` (already a dep), pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-github-backup-design.md`

## Global Constraints

- No `git` binary in the Docker container — HTTP-only via GitHub REST API.
- No SQLite migrations — GitHub credentials go in `settings.json` (AGENTS.md Rule 6).
- OrcaSlicer is the editor — print settings remain read-only; this feature only reads presets from Orca Cloud and writes to GitHub.
- Scripts over repetition (AGENTS.md Rule 2) — no new scripts needed for this feature.
- Designer agent for UI (AGENTS.md Rule 1) — templates/CSS picked up without restart; new routes require dev server restart (AGENTS.md Rule 4).
- `requests` already in `pyproject.toml` dependencies — no new deps.
- Tests run via `scripts/test.sh` (which calls `pytest`).

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `fm/github_backup.py` | `GitHubBackupClient` — Git Data API client (blobs, tree, commit, ref update) | Create |
| `fm/settings_store.py` | Add `load_github_config()` + `save_github_config()` helpers | Modify |
| `fm/service/sync_handlers.py` | Add `handle_github_backup` handler, auto-trigger in `handle_generate_preset` + `handle_temp_sync`, register handler | Modify |
| `fm/api/ui.py` | Add `POST /sync/backup-github` route, `POST /api/settings/github-test` route, extend `_settings_context` + `settings_save` with GitHub fields | Modify |
| `fm/api/templates/settings.html` | Add GitHub Backup fieldset (04) | Modify |
| `fm/api/templates/sync.html` | Add "Backup to GitHub" button (conditional on `github_configured`) | Modify |
| `tests/test_github_backup.py` | Client tests, handler tests, auto-trigger tests, UI route tests | Create |

---

### Task 1: GitHubBackupClient — Git Data API client

**Files:**
- Create: `fm/github_backup.py`
- Test: `tests/test_github_backup.py`

**Interfaces:**
- Consumes: `requests` (stdlib dep, already installed)
- Produces:
  - `GitHubBackupError(Exception)` — with `.status`, `.body`, `.message`
  - `GitHubBackupClient(token: str, repo: str, branch: str = "main")`
  - `GitHubBackupClient.backup(files: dict[str, str], message: str) -> dict` — returns `{"commit_sha": str, "files_written": int}`
  - `GitHubBackupClient.test_connection() -> dict` — returns `{"repo": str, "branch": str, "default_branch": str}`

- [ ] **Step 1: Write failing tests for the client**

Create `tests/test_github_backup.py`:

```python
# tests/test_github_backup.py
"""GitHubBackupClient tests — Git Data API flow, error handling, test_connection."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch, MagicMock

import pytest

from fm.github_backup import GitHubBackupClient, GitHubBackupError


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
                content = kwargs.get("json_body", {}).get("content", "")
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
    def test_test_connection_success(self):
        client = GitHubBackupClient("ghp_fake", "owner/repo", "main")
        with patch("fm.github_backup.requests.request") as mock_req:
            mock_req.return_value = self._mock_response(200, {
                "full_name": "owner/repo",
                "default_branch": "main",
            })
            result = client.test_connection()
        assert result["repo"] == "owner/repo"
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_backup.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fm.github_backup'`

- [ ] **Step 3: Implement GitHubBackupClient**

Create `fm/github_backup.py`:

```python
# fm/github_backup.py
"""GitHub backup client — atomic commits via the Git Data REST API.

No ``git`` binary required.  All operations are HTTP requests using
``requests`` (already a project dependency).

The ``backup`` method creates a single commit containing all provided
files, following the Git Data API flow:
ref → base commit → base tree → blobs → new tree → new commit → ref update.
"""
from __future__ import annotations

import base64

import requests


class GitHubBackupError(Exception):
    """GitHub API error with status code and response body."""

    def __init__(self, status: int, body: str, message: str = ""):
        self.status = status
        self.body = body
        self.message = message or body
        super().__init__(self.message)


class GitHubBackupClient:
    """HTTP-only GitHub client for atomic multi-file commits."""

    API_BASE = "https://api.github.com"
    TIMEOUT = 30

    def __init__(self, token: str, repo: str, branch: str = "main"):
        self._token = token
        self._repo = repo  # "owner/repo"
        self._branch = branch

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
    ) -> requests.Response:
        return requests.request(
            method,
            url,
            headers=self._headers(),
            json=json_body,
            timeout=self.TIMEOUT,
        )

    def _url(self, path: str) -> str:
        return f"{self.API_BASE}/repos/{self._repo}/{path}"

    def backup(self, files: dict[str, str], message: str) -> dict:
        """Create an atomic commit with all *files*.

        ``files`` maps repo-relative path → file content (string).
        Returns ``{"commit_sha": str, "files_written": int}``.
        Raises :class:`GitHubBackupError` on any API failure.
        """
        # 1. Get current ref SHA
        resp = self._request(
            "GET", self._url(f"git/ref/heads/{self._branch}"),
        )
        if not resp.ok:
            raise GitHubBackupError(
                resp.status_code, resp.text,
                f"branch '{self._branch}' not found" if resp.status_code == 404
                else f"ref lookup failed: {resp.status_code}",
            )
        ref_sha = resp.json()["object"]["sha"]

        # 2. Get base tree SHA from the commit
        resp = self._request(
            "GET", self._url(f"git/commits/{ref_sha}"),
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        base_tree_sha = resp.json()["tree"]["sha"]

        # 3. Create blobs for each file
        tree_entries = []
        for path, content in files.items():
            resp = self._request(
                "POST", self._url("git/blobs"),
                json_body={
                    "content": base64.b64encode(
                        content.encode("utf-8")
                    ).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if not resp.ok:
                raise GitHubBackupError(resp.status_code, resp.text)
            blob_sha = resp.json()["sha"]
            tree_entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

        # 4. Create new tree
        resp = self._request(
            "POST", self._url("git/trees"),
            json_body={
                "base_tree": base_tree_sha,
                "tree": tree_entries,
            },
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        tree_sha = resp.json()["sha"]

        # 5. Create commit
        resp = self._request(
            "POST", self._url("git/commits"),
            json_body={
                "message": message,
                "tree": tree_sha,
                "parents": [ref_sha],
            },
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        commit_sha = resp.json()["sha"]

        # 6. Update ref
        resp = self._request(
            "PATCH", self._url(f"git/refs/heads/{self._branch}"),
            json_body={"sha": commit_sha},
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)

        return {"commit_sha": commit_sha, "files_written": len(files)}

    def test_connection(self) -> dict:
        """Verify token + repo access. Returns repo metadata."""
        resp = self._request("GET", self._url(""))
        if not resp.ok:
            raise GitHubBackupError(
                resp.status_code, resp.text,
                "token invalid" if resp.status_code == 401
                else f"repo not found: {resp.status_code}",
            )
        data = resp.json()
        return {
            "repo": data.get("full_name", self._repo),
            "branch": self._branch,
            "default_branch": data.get("default_branch", "main"),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_backup.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add fm/github_backup.py tests/test_github_backup.py
git commit -m "feat: add GitHubBackupClient for atomic Git Data API commits"
```

---

### Task 2: Settings store helpers for GitHub config

**Files:**
- Modify: `fm/settings_store.py` (append after `save_orca_cursor` at line 157)
- Test: `tests/test_github_backup.py` (append new test class)

**Interfaces:**
- Consumes: `load_settings()`, `save_settings()` from `fm/settings_store.py`
- Produces:
  - `load_github_config() -> tuple[str, str, str] | None` — returns `(token, repo, branch)` or `None`
  - `save_github_config(token: str, repo: str, branch: str) -> None`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_github_backup.py`:

```python
from fm.settings_store import (
    load_github_config,
    save_github_config,
    load_settings,
    save_settings,
)


class TestGithubSettings:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_backup.py::TestGithubSettings -v`
Expected: FAIL with `ImportError: cannot import name 'load_github_config'`

- [ ] **Step 3: Implement the helpers**

Append to `fm/settings_store.py` (after `save_orca_cursor`):

```python
def load_github_config() -> tuple[str, str, str] | None:
    """Return ``(token, repo, branch)`` from settings.json, or ``None``.

    ``branch`` defaults to ``"main"`` if unset.  Returns ``None`` when
    ``github_token`` or ``github_repo`` is missing.
    """
    data = load_settings()
    if data is None:
        return None
    token = data.get("github_token", "")
    repo = data.get("github_repo", "")
    if not token or not repo:
        return None
    branch = data.get("github_branch", "main") or "main"
    return token, repo, branch


def save_github_config(token: str, repo: str, branch: str) -> None:
    """Merge GitHub backup config into existing settings.json."""
    data = load_settings() or {}
    data["github_token"] = token
    data["github_repo"] = repo
    data["github_branch"] = branch
    save_settings(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_backup.py::TestGithubSettings -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add fm/settings_store.py tests/test_github_backup.py
git commit -m "feat: add load_github_config / save_github_config settings helpers"
```

---

### Task 3: Job handler — `handle_github_backup`

**Files:**
- Modify: `fm/service/sync_handlers.py` (add handler after `handle_temp_sync` ~line 754, add import, add factory, register handler)
- Test: `tests/test_github_backup.py` (append new test class)

**Interfaces:**
- Consumes:
  - `load_github_config() -> tuple[str, str, str] | None` from Task 2
  - `GitHubBackupClient` from Task 1
  - `_orca_client_factory` (existing, in `sync_handlers.py`)
  - `_cloud_presets_to_local(upserts)` (existing, in `sync_handlers.py`)
  - `jobs.enqueue(session, kind, payload)` (existing)
  - `jobs.log_activity(session, action, outcome, detail)` (existing)
- Produces:
  - `handle_github_backup(session: Session, config: Config, payload: dict) -> dict`
  - `_github_client_factory: Callable[[], GitHubBackupClient | None]` — module-level factory (tests override)
  - `_enqueue_github_backup(session: Session) -> None` — helper for auto-trigger

- [ ] **Step 1: Write failing tests for the handler**

Append to `tests/test_github_backup.py`:

```python
from fm.service import sync_handlers as sh
from fm.service import jobs
from fm.models import Profile, Job, ActivityLog
from datetime import datetime, timezone


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
        assert "presets/Sunlu PLA-.json" in files
        assert "index.json" in files

        # Verify activity log
        logs = list(s.scalars(select(ActivityLog)))
        assert len(logs) == 1
        assert logs[0].action == "github_backup"
        assert logs[0].outcome == "ok"

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
        assert "presets/PLA---Bad---Name-.json" in files

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_backup.py::TestHandleGithubBackup -v`
Expected: FAIL with `AttributeError: module 'fm.service.sync_handlers' has no attribute 'handle_github_backup'`

- [ ] **Step 3: Implement the handler**

In `fm/service/sync_handlers.py`:

**3a. Add import at top** (after the `from ..orca_cloud import ...` line, line 58):

```python
from ..github_backup import GitHubBackupClient, GitHubBackupError
from ..settings_store import load_github_config
```

**3b. Add factory** (after `_orca_client_factory` definition, ~line 110):

```python
def _default_github_client() -> GitHubBackupClient | None:
    """Create a GitHubBackupClient from stored settings, or None if unconfigured."""
    config = load_github_config()
    if config is None:
        return None
    token, repo, branch = config
    return GitHubBackupClient(token, repo, branch)


# GitHub client factory — tests override via monkeypatch.
_github_client_factory: Callable[[], GitHubBackupClient | None] = _default_github_client
```

**3c. Add name sanitizer + handler** (after `handle_temp_sync`, before the "Preset drift computation" section ~line 756):

```python
# ---------------------------------------------------------------------------
# GitHub backup
# ---------------------------------------------------------------------------

import re as _re

_UNSAFE_CHARS = _re.compile(r'[/\\:*?"<>|]')


def _sanitize_filename(name: str) -> str:
    """Replace filesystem-unsafe characters with '-'."""
    return _UNSAFE_CHARS.sub('-', name)


def handle_github_backup(session: Session, config: Config, payload: dict) -> dict:
    """GitHub backup handler: pull all Orca Cloud presets, write each as a
    JSON file plus an index.json, commit atomically to GitHub.
    """
    client = _github_client_factory()
    if client is None:
        raise ValueError(
            "GitHub not configured — set up the connection in Settings"
        )

    orca = _orca_client_factory()
    if orca is None:
        raise ValueError(
            "Orca Cloud not connected — set up the connection in Settings"
        )

    # Full pull from Orca Cloud (cursor=0 → all presets)
    pull_result = orca.sync_pull(cursor=0)
    upserts = pull_result.get("upserts", [])
    presets = _cloud_presets_to_local(upserts)

    # Build file dict: presets/{safe_name}.json + index.json
    files: dict[str, str] = {}
    index: list[dict] = []
    seen_names: dict[str, int] = {}

    for preset in presets:
        raw_name = preset.get("name", "")
        if not raw_name:
            continue  # skip empty names
        safe = _sanitize_filename(raw_name)
        if safe in seen_names:
            seen_names[safe] += 1
            safe = f"{safe}_{seen_names[safe]}"
        else:
            seen_names[safe] = 0

        path = f"presets/{safe}.json"
        files[path] = json.dumps(preset, indent=2, sort_keys=True)
        index.append({
            "name": raw_name,
            "filament_id": preset.get("filament_id", ""),
            "cloud_id": preset.get("cloud_id", ""),
            "updated_time": preset.get("updated_time", ""),
            "file": path,
        })

    index.sort(key=lambda e: e["name"])
    files["index.json"] = json.dumps(index, indent=2, sort_keys=True)

    message = f"Backup: {_now().isoformat()} — {len(presets)} presets"
    result = client.backup(files, message)

    jobs.log_activity(
        session, "github_backup", "ok",
        {
            "commit_sha": result["commit_sha"],
            "files_written": result["files_written"],
            "preset_count": len(presets),
        },
    )

    return {
        "commit_sha": result["commit_sha"],
        "files_written": result["files_written"],
        "preset_count": len(presets),
    }
```

**3d. Register the handler** (in `register_handlers()`, add line):

```python
    jobs.HANDLERS["github_backup"] = handle_github_backup
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_backup.py::TestHandleGithubBackup -v`
Expected: PASS — all 6 tests green.

- [ ] **Step 5: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_github_backup.py
git commit -m "feat: add handle_github_backup job handler"
```

---

### Task 4: Auto-trigger after generate_preset and temp_sync

**Files:**
- Modify: `fm/service/sync_handlers.py` (add `_enqueue_github_backup` helper, call it at end of `handle_generate_preset` and `handle_temp_sync`)
- Test: `tests/test_github_backup.py` (append new test class)

**Interfaces:**
- Consumes: `_github_client_factory` (from Task 3), `jobs.enqueue` (existing)
- Produces: `_enqueue_github_backup(session: Session) -> None`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_github_backup.py`:

```python
from fm.presets.builder import build_standalone_preset


class TestAutoTrigger:
    def test_generate_preset_enqueues_backup_when_configured(
        self, inmemory_session, cfg, monkeypatch, tmp_path,
    ):
        """generate_preset enqueues github_backup when GitHub is configured."""
        s = inmemory_session
        profile = _make_profile(s)

        # Mock preset template
        monkeypatch.setattr(sh, "_preset_template_factory", lambda d: _preset_template())

        # Mock Orca Cloud client
        mock_orca = MagicMock()
        mock_orca.sync_push.return_value = {"updated_time": "2026-09-01"}
        mock_orca.cloud_id.return_value = "cloud-uuid-1"
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        # Mock GitHub as configured
        mock_github = MagicMock()
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # Verify github_backup job was enqueued
        backup_jobs = list(s.scalars(
            select(Job).where(Job.kind == "github_backup")
        ))
        assert len(backup_jobs) == 1
        assert backup_jobs[0].status == "queued"

    def test_generate_preset_no_backup_when_github_unconfigured(
        self, inmemory_session, cfg, monkeypatch, tmp_path,
    ):
        """generate_preset does NOT enqueue github_backup when GitHub is None."""
        s = inmemory_session
        profile = _make_profile(s)

        monkeypatch.setattr(sh, "_preset_template_factory", lambda d: _preset_template())

        mock_orca = MagicMock()
        mock_orca.sync_push.return_value = {"updated_time": "2026-09-01"}
        mock_orca.cloud_id.return_value = "cloud-uuid-1"
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        # GitHub not configured
        monkeypatch.setattr(sh, "_github_client_factory", lambda: None)

        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        backup_jobs = list(s.scalars(
            select(Job).where(Job.kind == "github_backup")
        ))
        assert len(backup_jobs) == 0

    def test_temp_sync_enqueues_backup_when_configured(
        self, inmemory_session, cfg, monkeypatch,
    ):
        """temp_sync enqueues github_backup when GitHub is configured."""
        s = inmemory_session
        _make_profile(s, filament_id="Pabc123")

        mock_orca = MagicMock()
        mock_orca.sync_pull.return_value = {
            "next_cursor": 0, "upserts": [], "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: mock_orca)

        mock_github = MagicMock()
        monkeypatch.setattr(sh, "_github_client_factory", lambda: mock_github)

        sh.handle_temp_sync(s, cfg, {})

        backup_jobs = list(s.scalars(
            select(Job).where(Job.kind == "github_backup")
        ))
        assert len(backup_jobs) == 1


def _preset_template():
    """Minimal system preset template for build_standalone_preset."""
    return {
        "type": "filament",
        "filament_type": ["PLA"],
        "name": "Generic PLA",
        "from": "System",
        "inherits": "",
        "filament_vendor": ["Generic"],
        "filament_density": "1.24",
        "nozzle_temperature": ["210"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
        "version": "2.4.0.3",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_backup.py::TestAutoTrigger -v`
Expected: FAIL — no `github_backup` jobs enqueued.

- [ ] **Step 3: Implement auto-trigger**

In `fm/service/sync_handlers.py`:

**3a. Add the helper** (right before `handle_github_backup`):

```python
def _enqueue_github_backup(session: Session) -> None:
    """Enqueue a github_backup job if GitHub is configured. No-op otherwise."""
    if _github_client_factory() is None:
        return
    jobs.enqueue(session, "github_backup", {})
```

**3b. Call it at the end of `handle_generate_preset`** — add before the final `return` in `handle_generate_preset` (after `target.last_synced_at = _now()`, before the `return {` block at line 700):

```python
    _enqueue_github_backup(session)
```

**3c. Call it at the end of `handle_temp_sync`** — add before the final `return` in `handle_temp_sync` (after the updates/skipped logic, before `return {"updates": updates, "skipped": skipped}` at line 754):

```python
    _enqueue_github_backup(session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_backup.py::TestAutoTrigger -v`
Expected: PASS — all 3 tests green.

- [ ] **Step 5: Run full test suite to check no regressions**

Run: `scripts/test.sh`
Expected: PASS — all existing tests still green.

- [ ] **Step 6: Commit**

```bash
git add fm/service/sync_handlers.py tests/test_github_backup.py
git commit -m "feat: auto-trigger github_backup after generate_preset and temp_sync"
```

---

### Task 5: UI routes — backup button + GitHub test connection

**Files:**
- Modify: `fm/api/ui.py` (add `POST /sync/backup-github` route, `POST /api/settings/github-test` route, extend `_settings_context` + `settings_save` with GitHub fields)
- Test: `tests/test_github_backup.py` (append new test class)

**Interfaces:**
- Consumes:
  - `load_github_config()`, `save_github_config()` from Task 2
  - `GitHubBackupClient` from Task 1
  - `jobs.enqueue(session, kind, payload)` (existing)
  - `_settings_context()` (existing, in `ui.py`)
- Produces:
  - `POST /sync/backup-github` — enqueues `github_backup` job, redirects to `/sync`
  - `POST /api/settings/github-test` — returns `{"ok": bool, "repo": str, "error": str|None}`
  - Extended `_settings_context` with `github_token`, `github_repo`, `github_branch`, `github_configured` fields
  - Extended `settings_save` with `github_token`, `github_repo`, `github_branch` form params

- [ ] **Step 1: Write failing tests**

Append to `tests/test_github_backup.py`:

```python
from fastapi.testclient import TestClient
from fm.api.app import create_app, Settings


class TestUIRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch, cfg):
        monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/gh_test.db")
        from fm.service import sync_handlers as sh
        monkeypatch.setattr(sh, "_transport_factory", lambda c: MagicMock())
        monkeypatch.setattr(sh, "_sleep", lambda s: None)
        with TestClient(create_app(Settings(config=cfg))) as c:
            yield c

    def test_backup_github_route_enqueues_job(self, client, monkeypatch):
        """POST /sync/backup-github enqueues a github_backup job."""
        # Mark GitHub as configured so the button would show
        from fm.settings_store import save_settings
        save_settings({
            "printer_ip": "1.2.3.4", "ssh_user": "root",
            "ssh_password": "x", "db_remote_path": "/x",
            "github_token": "tok", "github_repo": "o/r",
        })
        r = client.post("/sync/backup-github")
        assert r.status_code == 303
        assert r.headers["location"] == "/sync"

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
        })
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

    def test_sync_page_hides_backup_button_when_unconfigured(self, client):
        """Sync page does NOT show 'Backup to GitHub' when GitHub is not configured."""
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"Backup to GitHub" not in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_github_backup.py::TestUIRoutes -v`
Expected: FAIL — routes don't exist, GitHub fields not in settings.

- [ ] **Step 3: Implement the routes**

In `fm/api/ui.py`:

**3a. Add import** (at the top, after existing imports from `..settings_store`):

```python
from ..settings_store import load_github_config, save_github_config
from ..github_backup import GitHubBackupClient, GitHubBackupError
```

**3b. Extend `_settings_context`** — add GitHub fields to the returned dict. After the `orca_user_email` line in the return dict (line 1171), add:

```python
        "github_token": (load_settings() or {}).get("github_token", ""),
        "github_repo": (load_settings() or {}).get("github_repo", ""),
        "github_branch": (load_settings() or {}).get("github_branch", "main"),
        "github_configured": load_github_config() is not None,
```

**3c. Extend `settings_save`** — add GitHub form params and persist them. Add to the function signature:

```python
    github_token: str = Form(""),
    github_repo: str = Form(""),
    github_branch: str = Form("main"),
```

Add before `save_settings(submitted)` (line 1238):

```python
    # GitHub backup config (optional — empty token means not configured)
    if github_token.strip():
        submitted["github_token"] = github_token.strip()
        submitted["github_repo"] = github_repo.strip()
        submitted["github_branch"] = github_branch.strip() or "main"
```

**3d. Add `POST /sync/backup-github` route** (after the sync page routes, ~line 836):

```python
@router.post("/sync/backup-github")
def sync_backup_github(db: DbSession) -> RedirectResponse:
    """Enqueue a github_backup job and redirect to /sync."""
    job = jobs.enqueue(db, "github_backup", {})
    return RedirectResponse(url="/sync", status_code=303)
```

Add import at top if not already present:

```python
from ..service import jobs
```

**3e. Add `POST /api/settings/github-test` route** (after the `test_connection` route, ~line 1302):

```python
@router.post("/api/settings/github-test")
def github_test_connection(
    github_token: str = Form(""),
    github_repo: str = Form(""),
    github_branch: str = Form("main"),
) -> dict:
    """Test GitHub token + repo access."""
    github_token = github_token.strip()
    github_repo = github_repo.strip()
    if not github_token or not github_repo:
        return {"ok": False, "error": "Token and repo are required."}
    try:
        client = GitHubBackupClient(github_token, github_repo, github_branch.strip() or "main")
        info = client.test_connection()
    except GitHubBackupError as exc:
        return {"ok": False, "error": exc.message}
    return {"ok": True, "repo": info["repo"], "default_branch": info["default_branch"]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_github_backup.py::TestUIRoutes -v`
Expected: Some tests may still fail because templates don't have the GitHub fields yet. The route tests (`test_backup_github_route_enqueues_job`, `test_github_test_connection_success`, `test_github_test_connection_failure`, `test_settings_save_persists_github_fields`) should pass. The template-dependent tests (`test_settings_page_shows_github_fields`, `test_sync_page_shows_backup_button_when_configured`, `test_sync_page_hides_backup_button_when_unconfigured`) will fail until Task 6.

- [ ] **Step 5: Commit**

```bash
git add fm/api/ui.py tests/test_github_backup.py
git commit -m "feat: add GitHub backup UI routes and settings persistence"
```

---

### Task 6: UI templates — Settings fieldset + Sync button

**Files:**
- Modify: `fm/api/templates/settings.html` (add GitHub Backup fieldset 04)
- Modify: `fm/api/templates/sync.html` (add conditional Backup to GitHub button)
- Test: `tests/test_github_backup.py` (template tests from Task 5 should now pass)

**Interfaces:**
- Consumes: template context fields `github_token`, `github_repo`, `github_branch`, `github_configured` from Task 5
- Produces: visible UI elements

- [ ] **Step 1: Add GitHub Backup fieldset to settings.html**

In `fm/api/templates/settings.html`, before the `<div class="form-actions">` line (the Save button row), insert a new fieldset:

```html
    <fieldset class="form-section"><div class="form-section-heading"><span>04</span><div><legend>GitHub backup</legend><p>Back up Orca Cloud presets to a private GitHub repository.</p></div></div><div class="form-grid">
      <label class="full-width"><span>Token</span><input type="password" name="github_token" value="{{ github_token }}" placeholder="ghp_... (Personal Access Token)"></label>
      <label><span>Repository</span><input name="github_repo" value="{{ github_repo }}" placeholder="owner/repo"></label>
      <label><span>Branch</span><input name="github_branch" value="{{ github_branch }}" placeholder="main"></label>
      <div class="full-width"><button type="button" id="github-test-btn" class="button button-secondary">Test connection</button></div>
    </div></fieldset>
```

- [ ] **Step 2: Add Backup to GitHub button to sync.html**

In `fm/api/templates/sync.html`, in the `#sync-actions` section's `.button-group` div (line 6), add a conditional button after the existing buttons:

```html
{% if github_configured %}<button class="button button-secondary" hx-post="/sync/backup-github" hx-target="#job-progress" hx-swap="innerHTML">Backup to GitHub</button>{% endif %}
```

- [ ] **Step 3: Add JavaScript for GitHub test-connection button**

In `fm/api/templates/settings.html`, inside the existing `<script>` block, add:

```javascript
document.getElementById('github-test-btn')?.addEventListener('click', async function() {
  const token = document.querySelector('[name="github_token"]').value;
  const repo = document.querySelector('[name="github_repo"]').value;
  const branch = document.querySelector('[name="github_branch"]').value || 'main';
  if (!token || !repo) { alert('Enter token and repo first.'); return; }
  this.disabled = true; this.textContent = 'Testing…';
  try {
    const r = await fetch('/api/settings/github-test', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams({github_token: token, github_repo: repo, github_branch: branch}),
    });
    const data = await r.json();
    if (data.ok) {
      alert('Connected! Repo: ' + data.repo + ' (default branch: ' + data.default_branch + ')');
    } else {
      alert('Error: ' + data.error);
    }
  } catch (e) { alert('Request failed: ' + e.message); }
  this.disabled = false; this.textContent = 'Test connection';
});
```

- [ ] **Step 4: Run template-dependent tests**

Run: `pytest tests/test_github_backup.py::TestUIRoutes::test_settings_page_shows_github_fields tests/test_github_backup.py::TestUIRoutes::test_sync_page_shows_backup_button_when_configured tests/test_github_backup.py::TestUIRoutes::test_sync_page_hides_backup_button_when_unconfigured -v`
Expected: PASS — all 3 template tests green.

- [ ] **Step 5: Run full test suite**

Run: `scripts/test.sh`
Expected: PASS — all tests green, no regressions.

- [ ] **Step 6: Commit**

```bash
git add fm/api/templates/settings.html fm/api/templates/sync.html
git commit -m "feat: add GitHub backup fieldset to settings and backup button to sync page"
```

---

### Task 7: Sync page context — pass `github_configured` flag

**Files:**
- Modify: `fm/api/ui.py` (extend `sync_page` route context with `github_configured`)
- Test: already covered by Task 5/6 tests

**Interfaces:**
- Consumes: `load_github_config()` from Task 2
- Produces: `github_configured` bool in sync page template context

- [ ] **Step 1: Modify sync_page route**

In `fm/api/ui.py`, in the `sync_page` function (line 726), add `github_configured` to the template context dict:

Change the return block from:
```python
    return _templates(request).TemplateResponse(
        request, "sync.html",
        {"drift": drift, "awaiting_jobs": awaiting_jobs, "recent_jobs": recent_jobs},
    )
```
To:
```python
    return _templates(request).TemplateResponse(
        request, "sync.html",
        {
            "drift": drift,
            "awaiting_jobs": awaiting_jobs,
            "recent_jobs": recent_jobs,
            "github_configured": load_github_config() is not None,
        },
    )
```

- [ ] **Step 2: Run full test suite**

Run: `scripts/test.sh`
Expected: PASS — all tests green.

- [ ] **Step 3: Commit**

```bash
git add fm/api/ui.py
git commit -m "feat: pass github_configured flag to sync page template"
```

---

### Task 8: Smoke test against running app

**Files:** None (verification only)

- [ ] **Step 1: Start dev server**

Run: `scripts/dev-start.sh`
Expected: Server starts on http://127.0.0.1:8080

- [ ] **Step 2: Verify settings page shows GitHub fieldset**

Open http://127.0.0.1:8080/settings in a browser. Verify:
- "04 GitHub backup" fieldset is visible
- Token, Repository, Branch inputs are present
- "Test connection" button is present

- [ ] **Step 3: Save GitHub credentials**

Fill in the GitHub token, repo (`owner/repo`), and branch (`main`). Click "Save settings". Verify redirect to `/settings?saved=1` and success message.

- [ ] **Step 4: Test GitHub connection**

Click "Test connection" in the GitHub fieldset. Verify success alert with repo name.

- [ ] **Step 5: Verify sync page shows backup button**

Open http://127.0.0.1:8080/sync. Verify "Backup to GitHub" button is visible in the sync actions area.

- [ ] **Step 6: Trigger a backup**

Click "Backup to GitHub". Verify:
- Redirect to /sync
- Job appears in the progress area
- Job status transitions from `queued` → `running` → `done`

- [ ] **Step 7: Verify GitHub repo has the commit**

Check the GitHub repo (via web or `git log`). Verify:
- Latest commit message matches "Backup: ... — N presets"
- `presets/` directory contains one `.json` per preset
- `index.json` exists on root with correct entries

- [ ] **Step 8: Stop dev server**

Run: `scripts/dev-stop.sh`

- [ ] **Step 9: Final commit (if any cleanup needed)**

If any issues were found and fixed during smoke test, commit the fixes.
