# GitHub Backup for Orca Cloud Presets — Design Spec

**Date:** 2026-09-02
**Status:** Approved (design phase)
**Predecessor:** `docs/superpowers/specs/2026-08-26-filament-manager-design.md`

## 1. Purpose

Back up all Orca Cloud filament presets to a private GitHub repository as
version-controlled JSON files. Provides an off-cloud snapshot of preset
data, diffable in Git, restorable manually via `git clone` + import.

Non-goals: in-app restore, backing up the local SQLite catalog, backing up
printer DB entries, multi-repo support.

## 2. Architecture

New module `fm/github_backup.py` — `GitHubBackupClient` analogous to
`OrcaCloudClient`: HTTP-only, no `git` binary required.

```
┌───────────────────────────────────────────────────────────┐
│  Filament Manager                                         │
│                                                           │
│  OrcaCloudClient.sync_pull()  ──►  presets (local format) │
│         │                              │                  │
│         │                         per-preset .json         │
│         │                         + index.json             │
│         │                              │                  │
│         │                     GitHubBackupClient.backup()  │
│         │                              │                  │
│         │                     Git Data API:               │
│         │                     blobs → tree → commit → ref  │
│         │                              │                  │
│         ▼                              ▼                  │
│    handle_github_backup  ──────►  GitHub repo (private)    │
│    (job handler)                                          │
└───────────────────────────────────────────────────────────┘
```

### Components

| Component | Location | Purpose |
|---|---|---|
| `GitHubBackupClient` | `fm/github_backup.py` | Git Data API client: atomic commit of multiple files |
| `handle_github_backup` | `fm/service/sync_handlers.py` | Job handler: pull Orca Cloud → write files → commit to GitHub |
| Settings fields | `fm/settings_store.py` + `settings.json` | `github_token`, `github_repo`, `github_branch` |
| UI: Settings section | `fm/api/templates/settings.html` | GitHub config form (token, repo, branch, test button) |
| UI: Sync button | `fm/api/templates/sync.html` | "Backup to GitHub" action button |
| Routes | `fm/api/ui.py` | `POST /sync/backup-github`, `POST /api/settings/github-test` |
| Tests | `tests/test_github_backup.py` | Client, handler, auto-trigger, UI routes |

## 3. GitHub Backup Client (`fm/github_backup.py`)

### Constructor

```python
class GitHubBackupError(Exception):
    """GitHub API error with status code and response body."""
    def __init__(self, status: int, body: str, message: str = ""):
        self.status = status
        self.body = body
        self.message = message or body
        super().__init__(self.message)

class GitHubBackupClient:
    API_BASE = "https://api.github.com"

    def __init__(self, token: str, repo: str, branch: str = "main"):
        self._token = token
        self._repo = repo  # "owner/repo"
        self._branch = branch
```

### Git Data API Flow

`backup(files: dict[str, str], message: str) -> dict`:

`files` maps path-within-repo → file content (string). Returns
`{"commit_sha": str, "files_written": int}`.

1. **Get ref SHA:** `GET /repos/{repo}/git/ref/heads/{branch}`
   → 404 → `GitHubBackupError("Branch '{branch}' not found")`

2. **Get base tree SHA:** `GET /repos/{repo}/git/commits/{ref_sha}`
   → extract `tree.sha`

3. **Create blobs:** For each file, `POST /repos/{repo}/git/blobs`
   with `{"content": base64(content), "encoding": "base64"}`
   → collect `path → blob_sha`

4. **Create tree:** `POST /repos/{repo}/git/trees` with
   `{"base_tree": base_tree_sha, "tree": [{"path", "mode": "100644",
   "type": "blob", "sha": blob_sha}, ...]}`
   → new tree SHA

5. **Create commit:** `POST /repos/{repo}/git/commits` with
   `{"message", "tree": tree_sha, "parents": [ref_sha]}`
   → commit SHA

6. **Update ref:** `PATCH /repos/{repo}/git/refs/heads/{branch}`
   with `{"sha": commit_sha}`
   → done

All requests use `Authorization: Bearer {token}` header, 30s timeout.

### Test connection method

`test_connection() -> dict`: `GET /repos/{repo}` → returns
`{"repo": full_name, "branch": branch, "default_branch": ...}`.
Verifies token validity + repo access + branch existence.

## 4. Job Handler (`handle_github_backup`)

Registered in `jobs.HANDLERS["github_backup"]`.

### Flow

1. Load GitHub settings from `settings.json` (`github_token`,
   `github_repo`, `github_branch`). If missing → `error` with
   "GitHub not configured".
2. Create `OrcaCloudClient` from stored Orca Cloud token (reuse
   `_orca_client_factory`). If None → `error` with "Orca Cloud not
   connected".
3. `sync_pull(cursor=0)` → full preset list as `upserts`.
4. Convert to local format via `_cloud_presets_to_local(upserts)`.
5. Build file dict:
   - `presets/{safe_name}.json` per preset — JSON dump, 2-space indent,
     sorted keys. `safe_name` = preset name with `/`, `\`, `:`, `*`,
     `?`, `"`, `<`, `>`, `|` replaced by `-`. If two presets sanitize
     to the same name, append `_{n}` (zero-based counter) to the
     second and subsequent duplicates.
   - `index.json` on root: `[{"name", "filament_id", "cloud_id",
     "updated_time", "file"}, ...]` sorted by name. `file` is the
     `presets/{safe_name}.json` path.
6. `GitHubBackupClient.backup(files, message)` with message
   `"Backup: {ISO-8601 UTC} — {N} presets"`.
7. Log to `activity_log`: `action=github_backup`, `outcome=ok`,
   `detail={"commit_sha", "files_written", "preset_count"}`.
8. Return `{"commit_sha", "files_written", "preset_count"}`.

### Auto-trigger

After `handle_generate_preset` and `handle_temp_sync` succeed: if
GitHub settings are present, enqueue a `github_backup` job. This
happens inside the handler (not the UI route) so API-triggered syncs
also produce backups. The enqueue is fire-and-forget — backup failure
does not fail the preceding sync job.

### Manual trigger

`POST /sync/backup-github` (UI route) → enqueue `github_backup` job →
redirect to `/sync` with HTMX polling in the existing progress area.

## 5. Settings Extension

### New fields in `settings.json`

```json
{
  "github_token": "ghp_...",
  "github_repo": "owner/repo",
  "github_branch": "main"
}
```

Stored and loaded via existing `load_settings()` / `save_settings()`
in `fm/settings_store.py`. No new functions needed — the settings page
reads/writes the full dict.

### Settings store helpers

- `load_github_config() -> tuple[str, str, str] | None` — returns
  `(token, repo, branch)` or `None` if `github_token` or `github_repo`
  is missing. `branch` defaults to `"main"` if unset.
- `save_github_config(token, repo, branch)` — merges into existing
  `settings.json` (same pattern as `save_orca_token`).

## 6. UI Integration

### Settings page — new fieldset `04 GitHub Backup`

- **Token** — password input, `name="github_token"`
- **Repo** — text input, `name="github_repo"`, placeholder
  `"owner/repo"`
- **Branch** — text input, `name="github_branch"`, default `main`
- **Test connection** button — `POST /api/settings/github-test` (HTMX
  fetch, like existing `orca-connect-btn` pattern) → shows repo name +
  branch or error
- Saved via existing `POST /settings` form submit

### Sync page — "Backup to GitHub" button

- Visible only when `github_configured` template flag is true
- `POST /sync/backup-github` → enqueue job → HTMX polling in existing
  progress area
- Job result row shows: commit SHA (truncated, link to GitHub),
  preset count, timestamp

### Designer agent

UI design/refinement uses the omp `designer` agent per AGENTS.md
Rule 1. Templates and CSS are picked up without restart; new routes
require dev server restart (AGENTS.md Rule 4).

## 7. Error Handling

| Condition | Behavior |
|---|---|
| GitHub not configured | Auto-trigger skipped silently; manual button hidden |
| Orca Cloud not connected | Job `error`: "Orca Cloud not connected" |
| GitHub 401 | Job `error`: "GitHub token invalid or expired" |
| GitHub 404 (repo/branch) | Job `error`: "Repo or branch not found" |
| GitHub 403 (rate limit) | Job `error`: "Rate limit exceeded, resets at {time}" |
| Network timeout | One retry, then `error` |
| Empty Orca Cloud pull (0 presets) | Commit with empty `index.json` + no preset files |
| Auto-trigger backup fails | Logged to activity_log; preceding sync job unaffected |

## 8. Testing

`tests/test_github_backup.py`:

### Client tests (requests_mock)

- Full Git Data flow: ref → commit → blobs → tree → commit → ref-update
  → verify correct API calls + return value
- 404 on ref (branch not found) → `GitHubBackupError`
- 401 on any step → `GitHubBackupError`
- `test_connection()` success and failure

### Handler tests (monkeypatch)

- `handle_github_backup` with mocked Orca + GitHub clients → verify
  file dict contains correct preset paths + `index.json` content
- GitHub not configured → `error` result
- Orca Cloud not configured → `error` result
- Empty preset list → commit with empty `index.json`
- Auto-trigger: `handle_generate_preset` enqueues `github_backup` when
  GitHub configured; does not when not configured
- Auto-trigger: `handle_temp_sync` same behavior

### UI route tests

- `POST /sync/backup-github` enqueues job, redirects to `/sync`
- `POST /api/settings/github-test` returns repo info on success
- Settings page saves/loads GitHub fields

### Edge cases

- Preset name with special characters (`/`, `:`, `*`) → sanitized to `-`
- Duplicate sanitized names → append `_{n}` suffix
- Preset name empty string → skip with warning in activity log

## 9. Deployment Notes

- No Dockerfile changes needed (HTTP-only, `requests` already a dep)
- No new env vars required (GitHub config via settings.json UI)
- No SQLite migrations (AGENTS.md Rule 6)
- Token stored in `settings.json` on the data volume — same security
  posture as Orca Cloud refresh token and SSH password already there
