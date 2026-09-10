# Filament Manager — Technical Manual

Developer and administrator reference for the Filament Manager web
application: architecture, internal flows, data model, API surface,
deployment, and the Orca Cloud integration.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Data Model](#data-model)
3. [Module Map](#module-map)
4. [Configuration](#configuration)
5. [Job Queue and Handler System](#job-queue-and-handler-system)
6. [Workflows and Internal Flows](#workflows-and-internal-flows)
   - [Spool CRUD](#spool-crud)
   - [Orca Cloud Cache Sync](#orca-cloud-cache-sync)
   - [Printer DB Push](#printer-db-push)
   - [Verify](#verify)
   - [GitHub Backup](#github-backup)
   - [Orphan Printer DB Entry Removal](#orphan-printer-db-entry-removal)
   - [Printer DB Drift Detection (Live)](#printer-db-drift-detection-live)
7. [Printer DB Codec](#printer-db-codec)
8. [Preset Flattener](#preset-flattener)
9. [Orca Cloud Integration](#orca-cloud-integration)
10. [Transport Layer](#transport-layer)
11. [Push Pipeline](#push-pipeline)
12. [Iron Rules](#iron-rules)
13. [Settings Store](#settings-store)
14. [API Reference](#api-reference)
15. [Deployment](#deployment)
16. [Development](#development)
17. [Testing](#testing)

---

## Architecture Overview

```
┌─────────────────────────── Unraid container ───────────────────────────┐
│  Filament Manager (FastAPI + SQLite + Jinja2/HTMX)                     │
│  • Spool inventory (linked to cached presets via cloud_resource_id)    │
│  • Orca Cloud cache: read-only profile_cache from sync_pull            │
│  • Sync Hub: Preset & Status table, push to printer, orphan removal    │
│  • Printer sync: SSH/SCP push + version bump + reboot + verify         │
│  • GitHub backup: commit all Orca Cloud presets as JSON to a repo      │
│  • Settings page: printer connection + Orca Cloud credentials          │
│  • Job queue: push/verify/github_backup                                │
│    (drained synchronously inside requests — no scheduler/worker)       │
└──────────────┬──────────────┬──────────────────────────────────────────┘
               │ SSH/SCP/WS (LAN-only)                │ HTTPS
┌──────────────▼───────────────────────────┐  ┌────────▼─────────────────────┐
│  Creality K2 printer                      │  │  Orca Cloud (Supabase-backed)│
│  • material_database.json                 │  │  • Preset sync API           │
│  • Version bump + reboot on every write   │  │  • Refresh-token rotation    │
└───────────────────────────┘  └──────────────────────────────┘
```

Orca Cloud is the sole source of filament presets. The Manager caches them
read-only in a `profile_cache` table (SQLite). Printer DB entries are derived
from cached cloud presets and pushed to the printer. Spools reference cached
presets via `cloud_resource_id`. Presets deleted from Orca Cloud are
soft-deleted in the cache (tombstoned) and surface as orphan badges on linked
spools and in the Sync Hub.

**OrcaSlicer** is the desktop slicer application. It exists only on the
dev machine, where it is the sole editor of print settings and the source
for preset creation. **Orca Cloud** is the cloud sync service — the runtime
source of preset sync. The Manager pulls presets from Orca Cloud (read-only);
it never writes presets to Orca Cloud. OrcaSlicer syncs presets to/from
Cloud via its built-in File > Sync Presets feature.

**Key design principle:** OrcaSlicer is the sole editor of print settings.
The Manager displays cached preset data read-only. There is no local profile
catalog CRUD, no preset generation, and no migration wizard in v2.0.0.

---

## Data Model

SQLAlchemy models in `fm/models.py`. Database is SQLite. Schema
migrations are handled by `_migrate()` in `fm/db.py`, which adds new
columns via `ALTER TABLE ADD COLUMN` on engine init.

### ProfileCache

The read-only cache of Orca Cloud filament presets. This is the sole
source of preset identity in v2.0.0 — there is no editable `Profile` catalog.

| Column | Type | Description |
|---|---|---|
| `cloud_resource_id` | str(64) PK | Orca Cloud resource UUID (primary key) |
| `name` | str(255) | Preset name |
| `vendor` | str(255) | Filament vendor (from `filament_vendor`) |
| `type` | str(64) | Material type (from `filament_type`) |
| `filament_id` | str(64) \| None | Orca Cloud `filament_id` |
| `is_inherited` | bool | Whether the preset inherits from a system preset |
| `deleted_from_cloud` | bool | Soft-delete tombstone (preset deleted in Orca Cloud) |
| `deleted_at` | datetime \| None | Tombstone timestamp |
| `last_seen_at` | datetime | Last cache sync timestamp |

Relationship: `spools` → list of `Spool` (back_populates).

### Spool

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `profile_id` | int FK → `profiles.id` \| None | Legacy FK (nullable, kept for migration compatibility) |
| `cloud_resource_id` | str(64) FK → `profile_cache.cloud_resource_id` \| None | Links spool to a cached Orca Cloud preset |
| `color_override` | str \| None | Hex color (overrides profile default) |
| `remaining_pct` | int | Material remaining (0–100, default 100) |
| `location` | str | Storage location |
| `rfid_tag` | str \| None | RFID tag identifier (unused in UI, kept for compatibility) |
| `notes` | str | Free-text notes |
| `created_at` | datetime | Creation timestamp (UTC) |
| `updated_at` | datetime | Last modification timestamp (UTC) |

Relationship: `cached_profile` → `ProfileCache` (back_populates).

### SyncTarget

Tracks sync state per preset per target kind.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `profile_id` | int FK → `profiles.id` \| None | Legacy FK (nullable in v2.0.0) |
| `kind` | str(32) | `printer_db` or `orca_preset` |
| `cloud_id` | str(64) \| None | Orca Cloud resource UUID (links to `ProfileCache.cloud_resource_id`) |
| `expected_hash` | str \| None | Canonical hash of expected entry |
| `actual_state` | str \| None | JSON string of actual state |
| `status` | str(16) | `in_sync` \| `pending` \| `drift` \| `error` |
| `last_synced_at` | datetime \| None | Last sync attempt timestamp |
| `printer_db_id` | str(16) \| None | "99xxx" printer DB row this target mirrors |

### Job

Job queue for sync operations. Jobs are drained synchronously inside
API/UI request paths — there is no scheduler and no background worker.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `kind` | str(32) | `push` \| `verify` \| `github_backup` |
| `payload` | str | JSON string (job parameters) |
| `status` | str(24) | `queued` \| `running` \| `done` \| `error` \| `awaiting_input` |
| `result` | str \| None | JSON string (job result or error) |
| `created_at` | datetime | Creation timestamp (UTC) |
| `finished_at` | datetime \| None | Completion timestamp |

### ActivityLog

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `ts` | datetime | Event timestamp |
| `action` | str(64) | Action name (e.g. "verify", "push") |
| `detail` | str | JSON string (event details) |
| `outcome` | str(8) | `ok` \| `warn` \| `error` |

---

## Module Map

```
fm/
├── __init__.py
├── config.py              # Config dataclass, load_config() from env vars
├── db.py                  # SQLAlchemy engine + SessionLocal + _migrate()
├── errors.py              # FmError exception
├── models.py              # SQLAlchemy models (ProfileCache, Spool, SyncTarget, Job, ActivityLog, Profile)
├── orca_cloud.py          # OrcaCloudClient — Orca Cloud Sync API (pull, Supabase auth)
├── github_backup.py       # GitHubBackupClient — commit presets as JSON to a repo
├── settings_store.py      # settings.json read/write, load_merged_config(), Orca Cloud token storage
├── api/
│   ├── __init__.py
│   ├── app.py             # FastAPI factory (create_app), router mounting
│   ├── sync.py            # REST API: /api/sync/* (push, backup-github, drift, jobs)
│   ├── ui.py              # UI routes (HTML pages, HTMX fragments, form handlers)
│   ├── templates/         # Jinja2 templates
│   └── static/            # CSS
├── service/
│   ├── __init__.py
│   ├── jobs.py            # Job queue: enqueue, claim_next, run_pending, complete, fail
│   ├── sync_handlers.py   # Handlers: push, verify, github_backup, compute_printer_db_drift
│   └── cache_sync.py      # sync_profile_cache — pull Orca Cloud presets into profile_cache
├── printer/
│   ├── __init__.py
│   ├── transport.py       # SshTransport (SSH/SCP/WS), PrinterTransport Protocol
│   ├── pipeline.py        # run_push(), verify_printer(), canonical_hash()
│   └── dbcodec.py         # Printer DB JSON codec: load/save/insert/remove/validate
└── presets/
    ├── __init__.py
    ├── flattener.py       # Preset flattening, find_preset_by_name, find_system_preset
    ├── template_index.py  # Runtime loader for template_index.json (legacy, used by tests)
    ├── template_index.json # Bundled @base preset index (legacy, used by tests)
    └── templates/         # Bundled @base preset templates (used for inheritance resolution)
```

**Removed in v2.0.0:** `fm/api/catalog.py` (catalog CRUD REST API),
`fm/presets/builder.py` (preset builder), and `fm/presets/matching.py`
(preset matching). The template index (`fm/presets/template_index.py`,
`template_index.json`) and `scripts/build_templates.py` remain for test
coverage but are not imported by runtime code.

---

## Configuration

### Config Dataclass (`fm/config.py`)

```python
@dataclass(frozen=True)
class Config:
    printer_ip: str
    ssh_user: str
    ssh_password: str
    db_remote_path: str
    ws_port: int = 9999
    version_override: int = 9876543210
    id_range_start: int = 99001
```

`load_config()` reads the required printer env vars and raises `FmError`
if any of `FM_PRINTER_IP`, `FM_SSH_USER`, `FM_SSH_PASSWORD`,
`FM_DB_REMOTE_PATH` are missing.

### Configuration Precedence

1. **`settings.json`** (managed via Settings page) — highest priority.
2. **`FM_*` environment variables** — fallback.
3. **Hardcoded defaults** — last resort.

`load_merged_config()` (`fm/settings_store.py`) tries `settings.json`
first, then env vars. Returns `None` if neither source is complete; sync
endpoints return 503 in that case.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `FM_DATABASE_URL` | `sqlite:////data/filaments.db` (container) / `sqlite:///./filaments.db` (local) | SQLAlchemy database URL |
| `FM_PRINTER_IP` | `192.168.0.101` | Creality K2 printer IP |
| `FM_SSH_USER` | `root` | SSH user for printer |
| `FM_SSH_PASSWORD` | *(secret)* | SSH password for printer |
| `FM_DB_REMOTE_PATH` | `/mnt/UDISK/creality/userdata/box/material_database.json` | Remote printer DB path |
| `FM_WS_PORT` | `9999` | Printer WebSocket port (busy check) |
| `FM_VERSION_OVERRIDE` | `9876543210` | DB version floor |
| `FM_ID_RANGE_START` | `99001` | Start of custom printer ID range |
| `FM_SETTINGS_PATH` | `.local/settings.json` (dev) / `/data/settings.json` (container) | Path to settings.json |
| `FM_CACHE_DB_PATH` | *(derived from `FM_DATABASE_URL`)* | Override printer DB cache path |

---

## Job Queue and Handler System

The job queue (`fm/service/jobs.py`) drives all sync operations. Jobs
are drained **synchronously** inside API/UI request paths by
`run_pending` — there is no scheduler, no background worker, and no
polling process.

### Job Lifecycle

```
queued → running → done
                  → error
                  → awaiting_input → (user resolves) → queued → ...
```

### Key Functions

| Function | Description |
|---|---|
| `enqueue(session, kind, payload)` | Create a queued job with JSON payload |
| `claim_next(session)` | Atomically claim oldest queued job (FIFO): queued → running |
| `complete(job, result)` | Mark job done, store JSON result |
| `fail(job, error)` | Mark job errored, store error dict |
| `await_input(job, options)` | Park job awaiting user input (stores options in result) |
| `run_pending(session, config)` | Drain queue: claim and run each queued job through registered handler |
| `log_activity(session, action, outcome, detail)` | Append to ActivityLog |

### Handler Registration

Handlers are registered in `HANDLERS: dict[str, Callable]` in
`fm/service/jobs.py`. `sync_handlers.py` calls `register_handlers()` on
import (side-effect of including the sync router).

### Handler Routing

`run_pending` processes jobs by kind. All registered kinds are
server-side handlers executed immediately within the request:

- **push** — rebuild printer DB from cloud presets, upload, reboot, enqueue verify.
- **verify** — pull fresh DB, version-floor + presence + hash check.
- **github_backup** — pull all Orca Cloud presets, write each as a JSON
  file plus an index.json, commit atomically to GitHub.

### Injection Points (Testing)

`sync_handlers.py` exposes module-level injection points that tests
override via monkeypatch:

| Injection Point | Default | Purpose |
|---|---|---|
| `_transport_factory` | `lambda cfg: SshTransport(cfg)` | Replace SSH transport with fake |
| `_cache_db_path` | `None` (resolved at runtime) | Override printer DB cache path |
| `_orca_client_factory` | `_default_orca_client` (loads token from settings) | Replace Orca Cloud client with fake |
| `_github_client_factory` | `_default_github_client` (loads GitHub config) | Replace GitHub client with fake |
| `_sleep` | `time.sleep` | No-op sleep for tests |

---

## Workflows and Internal Flows

### Spool CRUD

**Create** (`POST /spools/form`, `ui.py`): validates `cloud_resource_id`
exists in `profile_cache`, creates `Spool` row linked to the cached preset.

**Edit** (`POST /spools/{sid}/edit`, `ui.py`): inline form updates
`cloud_resource_id`, `color_override`, `remaining_pct`, `location`, `notes`.

**Duplicate** (`POST /spools/{sid}/duplicate`, `ui.py`): copies all
fields, resets `remaining_pct=100`.

**Delete** (`POST /spools/{sid}/delete`, `ui.py`): removes spool.

**Remaining update** (`POST /spools/{sid}/remaining`, `ui.py`):
HTMX endpoint, updates `remaining_pct` via slider, returns inline HTML
fragment.

**Filter** (`GET /spools?cloud_resource_id=&location=`): filter by
cached preset or location text.

### Orca Cloud Cache Sync

**Function:** `sync_profile_cache` (`fm/service/cache_sync.py`).

Pulls filament presets from Orca Cloud via `OrcaCloudClient.sync_pull`
and upserts them into the `profile_cache` table. Only filament presets
are cached — Orca Cloud also returns `print` and `printer` process
presets, which are filtered out. Any stale `profile_cache` entries left
over from a preset that changed type (e.g. a filament preset re-uploaded
as a print preset) are purged. Soft deletes from the tombstone feed are
applied (`deleted_from_cloud=True`, `deleted_at` set).

**Flow:**

1. Call `client.sync_pull(cursor=0)` — returns `{upserts, deletes}`.
2. For each upsert:
   - Extract `id` (cloud resource UUID), `name`, `inherits`,
     `filament_vendor`, `filament_type`, `filament_id` from the content.
   - **Non-filament filter**: if `content.type` is `print` or `printer`
     (or the entry lacks filament fields), skip it and purge any stale
     `ProfileCache` row with the same `cloud_resource_id`.
   - If inherited and vendor/type missing: resolve from the parent system
     preset via `find_preset_by_name` + `flatten_preset`.
   - Upsert into `ProfileCache` (create or update, clear tombstone).
   - **Re-upload reconciliation**: if no existing `SyncTarget(printer_db)`
     row matches the new `cloud_resource_id`, search for one whose
     `cloud_id` is in the deletes feed or whose linked `Profile` name
     matches the preset name. If found, re-link the `SyncTarget.cloud_id`
     and update all `Spool.cloud_resource_id` references from the old UUID
     to the new one. This handles presets that were deleted and re-uploaded
     to Orca Cloud with a new UUID.
3. For each delete ID: tombstone the `ProfileCache` entry if not already.
4. Commit and return summary `{synced, deleted, offline}`.

On Orca Cloud API failure, returns `{synced: 0, deleted: 0, offline: True}`.

### Printer DB Push

**Trigger:** `POST /api/sync/push` (`sync.py`) or **Push to printer**
button on Sync Hub.

**Handler:** `handle_push` (`sync_handlers.py`).

**Flow:**

1. **Enqueue** push job with optional `cloud_resource_ids`,
   `profile_ids`, `force_reboot`, `no_reboot`. `run_pending` drains the
   queue synchronously.
2. **`_rebuild_printer_db`** (`sync_handlers.py`):
   - **Cloud-first path** (Orca Cloud configured): pull filament presets
     via `sync_pull`, filter by `cloud_resource_ids` if provided, flatten
     inherited presets, build entries via `build_entry`. `printer_db_id`
     is mapped via existing `SyncTarget` rows (`cloud_id == res_id`) or
     allocated as `next_free_id`.
   - **Fallback path** (no Orca Cloud client): rebuild from cache DB +
     catalog `Profile` rows, filtered by `profile_ids` if provided.
   - **Orphan Rule**: existing custom 99xxx entries missing from the
     cloud/catalog are *never* removed here — they persist on the printer
     until explicitly deleted via the orphan removal flow.
   - Returns `(db, pushed, skipped_ids)`.
3. **Save** rebuilt DB to temp file.
4. **`run_push`** (`pipeline.py`) — the iron-rule pipeline:
   - `MIN_DB_ENTRIES` check (≥30, refuse unless `force_push`).
   - Busy check via `check_printer_busy(transport)` (WS query). If busy
     and not `force_reboot`: return `{"status": "busy"}` without
     touching the printer.
   - Backup: `cp` remote DB to `.bak.{timestamp}`, rotate (keep 5).
   - Version bump: `bump_version(db, config.version_override)` —
     auto-increments if current ≥ override.
   - SCP upload: `transport.scp_push(local_path)`.
   - Sleep 3s (filesystem flush).
   - If `no_reboot`: return `{"status": "no_reboot"}`.
   - Reboot: `ssh ... 'sync; sync; sleep 2; reboot'` (connection drop
     expected, caught).
   - Wait for reboot: poll SSH up to 300s (`_wait_for_reboot`).
5. **On busy:** job → `awaiting_input` with options
   `["force_reboot", "wait", "no_reboot"]`. UI shows busy dialog.
6. **On success:** update `SyncTarget(printer_db)` per pushed entry:
   - `expected_hash` = `_entry_hash(entry)`.
   - `status = "pending"`.
   - `last_synced_at = now`.
7. **Enqueue verify** job automatically (drained on the next
   `run_pending` call).

### Verify

**Trigger:** Enqueued automatically after push.

**Handler:** `handle_verify` (`sync_handlers.py`).

**Flow:**

1. Pull fresh DB from printer via SCP (ignores cache).
2. Version floor check: `int(version) >= config.version_override`. If
   below → `cloud_rollback = True` (cloud sync overwrote the DB).
3. For each `SyncTarget(printer_db)` with `printer_db_id`:
   - If `cloud_rollback`: `SyncTarget.status = "error"`.
   - If entry missing: `SyncTarget.status = "error"`.
   - If entry present: compare `_entry_hash(entry)` against
     `target.expected_hash`. Match → `"in_sync"`. Mismatch → `"error"`.
4. Log warning if cloud rollback detected.
5. Returns version_ok, present IDs, missing IDs, cloud_rollback flag.

### GitHub Backup

**Trigger:** `POST /api/sync/backup-github` (`sync.py`) or **Backup to
GitHub** button on Sync Hub.

**Handler:** `handle_github_backup` (`sync_handlers.py`).

**Flow:**

1. Create `GitHubBackupClient` from stored settings (token, repo, branch).
2. Pull all Orca Cloud presets via `sync_pull`.
3. Write each preset as a JSON file plus an `index.json` summary.
4. Commit atomically to the configured GitHub repository.
5. Returns summary `{committed, files}`.

### Orphan Printer DB Entry Removal

**Trigger:** `POST /sync/printer-db/{entry_id}/remove` (`ui.py`).

**Flow:**

1. Double-confirm: the `confirm` form field must match `entry_id`
   (Iron Rule: typed token must match).
2. Remove the entry from the local printer DB cache via `remove_entry`.
3. Delete the associated `SyncTarget(printer_db)` row.
4. Return the refreshed Sync Hub fragment via HTMX.

### Printer DB Drift Detection (Live)

**Function:** `compute_printer_db_drift` (`sync_handlers.py`).

Called by `drift_status` (`sync.py`) on each page load. Pulls the
printer DB live via `load_printer_db_for_drift` (SCP to a temp file,
cache-file fallback, returns None if neither available). For each
`SyncTarget(printer_db)`, computes a live status:


| Status | Condition |
|---|---|
| `new` | No `printer_db_id` — never pushed |
| `in_sync` | Entry exists and `_entry_drift_detail(entry, expected)` is empty |
| `modified` | Entry exists but differs — detail string shows changed fields |

When `load_printer_db_for_drift` returns None (printer unreachable, no
cache), `drift_status` falls back to the existing SyncTarget-based
status chain.

---

## Printer DB Codec

`fm/printer/dbcodec.py` — load/save/mutate the Creality K2
`material_database.json`.

### Structure

The printer DB JSON has this shape:

```json
{
  "result": {
    "version": 9876543211,
    "count": 42,
    "list": [
      {
        "base": {"id": "01001", "name": "...", "brand": "...", ...},
        "kvParam": {"nozzle_temperature": 220, ...}
      }
    ]
  }
}
```

### Key Functions

| Function | Description |
|---|---|
| `load_db(path)` | Load + parse JSON. Raises `FmError(EXIT_DB)` if unparseable. |
| `save_db(path, db)` | Write JSON with `indent=2`, `ensure_ascii=False`. |
| `find_entry(db, id)` | Find entry by `base.id`. Returns entry dict or None. |
| `find_custom_entries(db)` | All entries with `base.id` starting with "99". |
| `next_free_id(db, start)` | Next available 99xxx ID (increments from `start`). |
| `insert_entry(db, entry)` | Insert entry, auto-fix count. Refuses if ID exists. |
| `patch_entry(db, id, changes)` | Patch entry fields. Refuses on stock IDs. |
| `remove_entry(db, id)` | Remove entry. Refuses on stock (non-99xxx) IDs. |
| `build_entry(db, values)` | Clone template entry `01001`, overlay values dict. |
| `bump_version(db, version)` | Set version ≥ `version`; auto-increment if current ≥ override. |
| `count_autofix(db)` | Set `result.count` to `len(result.list)`. |
| `validate_entry(values)` | Validate brand/name/type/temps/density. Returns (errors, warnings). |

### Constants

| Constant | Value | Description |
|---|---|---|
| `MIN_DB_ENTRIES` | 30 | Minimum entries for push safety check |
| `TEMPLATE_ID` | `"01001"` | Template entry cloned by `build_entry` |

---

## Preset Flattener

`fm/presets/flattener.py` — flattens OrcaSlicer preset inheritance chains.

### Key Functions

| Function | Description |
|---|---|
| `flatten_preset(path)` | Load a preset JSON file and flatten its `inherits` chain, merging parent settings with child overrides winning. |
| `find_preset_by_name(name)` | Find a bundled `@base` template by display name. Returns `Path` or `None`. |
| `find_system_preset(type)` | Find the generic system preset for a filament type. |

The flattener is used by `cache_sync.py` to resolve inherited presets'
vendor and type from the parent system preset when the cloud content
omits them.

---

## Orca Cloud Integration

`fm/orca_cloud.py` — `OrcaCloudClient`, a direct client for the Orca
Cloud Sync API. Auth is via a Supabase refresh token; the token rotates
on every refresh and the caller must persist the new token via the
`on_token_rotated` callback.

### Authentication

- `login_with_password(email, password)` (classmethod) — calls
  Supabase's password-grant endpoint, returns `(refresh_token, user_id)`.
  Used by the Settings page connect flow
  (`POST /api/settings/orca-connect`).
- `_refresh()` — exchanges the refresh token for an access token;
  rotates the stored refresh token via the callback.
- `_request()` — authenticated request; on 401, forces a refresh and
  retries once.

### Token Storage

Tokens are persisted in `settings.json` via `fm/settings_store.py`:

| Function | Description |
|---|---|
| `load_orca_token()` | Return `(refresh_token, user_id)` from settings.json, or `None`. |
| `save_orca_token(refresh_token, user_id)` | Merge Orca Cloud token + user id into settings.json. |
| `load_orca_cursor()` / `save_orca_cursor(cursor)` | Sync cursor persistence. |

`_default_orca_client()` (`sync_handlers.py`) builds an
`OrcaCloudClient` from stored credentials, wiring
`on_token_rotated` to `save_orca_token`. Returns `None` when no
credentials are configured.

### Sync API

| Method | Description |
|---|---|
| `sync_pull(cursor=0)` | Pull profiles. Returns `{next_cursor, upserts, deletes}`. On 410 Gone, retries without cursor (full resync). |
| `cloud_id(filament_id)` | Deterministic Orca Cloud resource UUID (UUID v5 of user_id + filament_id). |
| `get_user_id()` | Return the authenticated user's UUID (`sub` from JWT). |

### Orca Cloud Settings Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/settings/orca-connect` | Email+password login → persist refresh token |
| POST | `/api/settings/orca-disconnect` | Remove stored Orca Cloud credentials |

---

## Transport Layer

`fm/printer/transport.py` — SSH/SCP/WebSocket boundary behind an
injectable Protocol.

### PrinterTransport Protocol

```python
class PrinterTransport(Protocol):
    def run(self, cmd: str, timeout: int = 30) -> subprocess.CompletedProcess: ...
    def scp_push(self, local: Path) -> None: ...
    def scp_pull(self, local: Path) -> None: ...
    def ws_request(self, method: str, params: dict) -> dict: ...
```

### SshTransport

Real implementation using `sshpass` + `ssh`/`scp` subprocess calls and
the `websocket-client` library.

| Method | Description |
|---|---|
| `run(cmd, timeout)` | SSH command execution |
| `scp_pull(local)` | SCP download of printer DB |
| `scp_push(local)` | SCP upload of local DB to printer |
| `ws_request(method, params)` | WebSocket JSON-RPC request to printer |
| `get_printer_status()` | WS `get` request returning full status dict |
| `check_printer_busy()` | Returns None (idle) or dict with filename/progress/layer |
| `wait_for_reboot(timeout=300)` | Polls SSH until printer responds |
| `ssh_backup()` | `cp` remote DB to timestamped backup, rotate (keep 5) |
| `ssh_reboot()` | `sync; sync; sleep 2; reboot` (connection drop expected) |

### `check_printer_busy(transport)`

Module-level function operating on the Protocol. Queries printer status
via WS. Returns `None` if idle, or a dict with `filename`, `progress`,
`layer` if printing/paused. `BUSY_STATES = {1, 2}` (printing, paused).

---

## Push Pipeline

`fm/printer/pipeline.py` — the iron-rule-enforced push sequence.

### `run_push(config, local_db_path, transport, *, force_reboot, no_reboot, force_push, sleep)`

**Order (Iron Rule 1, load-bearing):**

1. **MIN_DB_ENTRIES check** — `len(db.result.list) >= 30`. Refuses
   with `FmError(EXIT_VALIDATE)` unless `force_push`.
2. **Busy check** — `check_printer_busy(transport)`. If busy and not
   `force_reboot`: return `{"status": "busy", "job": details}` without
   touching the printer.
3. **Backup** — `transport.run("cp ... .bak.{ts}")` + rotate (keep 5).
4. **Version bump** — `bump_version(db, config.version_override)`.
   Auto-increments if current ≥ override.
5. **Save + SCP upload** — `save_db(local_path, db)` then
   `transport.scp_push(local_path)`.
6. **Sleep 3s** — filesystem flush on printer.
7. **No-reboot exit** — if `no_reboot`: return
   `{"status": "no_reboot"}`.
8. **Reboot** — `transport.run("sync; sync; sleep 2; reboot",
   timeout=15)`. Connection drop caught as expected.
9. **Wait for online** — `_wait_for_reboot(transport, timeout=300,
   sleep)`. Polls SSH every 5s. Raises `FmError(EXIT_REBOOT)` if
   timeout.
10. Return `{"status": "ok", "job": None}`.

### `canonical_hash(fields)`

SHA-256 of canonical JSON (`sort_keys=True`) of identity + parameter
fields. Used for drift detection (expected vs. actual hash comparison).

---

## Iron Rules

Non-negotiable rules enforced in code, lifted from the skill
(`AGENTS.md`):

1. **Version ≥ 9876543210 + reboot mandatory** after every printer-DB
   write. Busy-check runs **before** any upload. Enforced in
   `run_push()`.
2. **Custom printer IDs only in 99xxx range.** Stock IDs protected.
   Enforced in `remove_entry()`, `patch_entry()` via `_is_custom_id()`.
3. **Backup on printer before every write.** Rotate, keep 5. Enforced
   in `run_push()`.
4. **Double-confirm on delete.** Typed token must match. Enforced in
   `POST /sync/printer-db/{entry_id}/remove`.
5. **Refuse push when DB < 30 entries** (`MIN_DB_ENTRIES`) unless
   explicitly forced. Enforced in `run_push()`.

---

## Settings Store

`fm/settings_store.py` — persistent config in `settings.json`.

### Path Resolution

`settings_path()` resolves in this order:
1. `FM_SETTINGS_PATH` env var (explicit override).
2. Parent directory of `FM_DATABASE_URL` SQLite file + `settings.json`.
3. `settings.json` in CWD.

### Functions

| Function | Description |
|---|---|
| `load_settings()` | Read `settings.json`, return dict or None. |
| `save_settings(data)` | Atomic write (temp file + `os.replace`). |
| `build_config(data)` | Build `Config` from settings dict. |
| `load_merged_config(env)` | Try settings.json first, then env vars. Returns `Config` or `None`. |
| `load_orca_token()` | Return `(refresh_token, user_id)` from settings.json, or `None`. |
| `save_orca_token(refresh_token, user_id)` | Merge Orca Cloud token + user id into settings.json. |
| `load_orca_cursor()` / `save_orca_cursor(cursor)` | Orca Cloud sync cursor persistence. |
| `load_github_config()` | Return `(token, repo, branch)` from settings.json, or `None`. |
| `save_github_config(token, repo, branch)` | Merge GitHub config into settings.json. |

### Settings Page Flow

- `GET /settings` (`ui.py`): render form pre-filled from current
  config or stored settings, plus Orca Cloud connection status.
- `POST /settings` (`ui.py`): validate required fields, parse ints,
  `save_settings()`, update `app.state.config`, redirect.
- `POST /api/settings/test-connection` (`ui.py`): SSH to printer,
  check DB path exists. Returns `{"ok": bool, "error": str \| None}`.
- `POST /api/settings/orca-connect` (`ui.py`): email+password
  login to Orca Cloud, persist refresh token.
- `POST /api/settings/orca-disconnect` (`ui.py`): remove stored
  Orca Cloud credentials.
- `POST /api/settings/github-test` (`ui.py`): test GitHub token +
  repo access.

---

## API Reference

### Sync API (`/api`)

| Method | Path | Description |
|---|---|---|
| POST | `/api/sync/push` | Enqueue push job, drain queue |
| POST | `/api/sync/backup-github` | Enqueue github_backup job, drain queue |
| GET | `/api/status/drift` | Per-preset drift matrix (live printer DB status) |
| POST | `/api/jobs/{job_id}/resolve` | Resolve awaiting_input job |
| GET | `/api/jobs?since={id}` | List jobs since ID |
| GET | `/api/jobs/{job_id}` | Get single job |

### Settings API (`/api/settings`)

| Method | Path | Description |
|---|---|---|
| POST | `/api/settings/orca-connect` | Connect to Orca Cloud (email+password) |
| POST | `/api/settings/orca-disconnect` | Disconnect from Orca Cloud |
| POST | `/api/settings/test-connection` | Test SSH connection to printer |
| POST | `/api/settings/github-test` | Test GitHub token + repo access |

### Meta

| Method | Path | Description |
|---|---|---|
| GET | `/api/version` | App version |

### UI Routes

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/spools` | Spool inventory with filters |
| POST | `/spools/form` | Create spool (linked to cached preset via `cloud_resource_id`) |
| POST | `/spools/{sid}/remaining` | HTMX: update remaining_pct |
| POST | `/spools/{sid}/edit` | Edit spool |
| POST | `/spools/{sid}/delete` | Delete spool |
| POST | `/spools/{sid}/duplicate` | Duplicate spool (remaining=100) |
| GET | `/sync` | Sync Hub (Preset & Status table, actions, job progress) |
| GET | `/sync/progress?since={id}` | HTMX: job progress table |
| POST | `/sync/jobs/{job_id}/resolve` | HTMX: resolve awaiting_input job |
| POST | `/sync/push-modal` | HTMX: enqueue push job, return modal fragment |
| GET | `/sync/push-modal/{job_id}` | HTMX: poll push modal fragment |
| POST | `/sync/backup-modal` | HTMX: enqueue backup job, return modal fragment |
| GET | `/sync/backup-modal/{job_id}` | HTMX: poll backup modal fragment |
| POST | `/sync/jobs/{job_id}/resolve-modal` | HTMX: resolve job from modal |
| GET | `/sync/hub-fragment` | HTMX: Sync Hub preset & status table fragment |
| POST | `/sync/printer-db/{entry_id}/remove` | Remove orphan printer DB entry (double-confirm) |
| GET | `/settings` | Settings page |
| POST | `/settings` | Save settings |

**Removed in v2.0.0:** `/profiles`, `/profiles/{id}/edit`,
`/profiles/form`, `/profiles/sync-from-orca`, `/profiles/{pid}/orca-fragment`,
`/api/profiles`, `/api/spools`, `/api/templates`, `/migration`,
`/import-orca`, `/api/sync/preset/{profile_id}`.

---

## Deployment

### Container (Unraid)

The image is built and loaded locally — no registry.

```bash
scripts/release.sh 2.0.0           # tag vX.Y.Z, build image, save dist/*.tar.gz
scripts/deploy-unraid.sh 2.0.0     # load image + XML template on Unraid, restart
```

`deploy-unraid.sh` expects:
- Deploy env file at `deploy/filament-manager.env` (override with
  `FM_ENV_FILE`) holding `FM_*` variables.
- SSH access to `root@unraid` (key/config alias, or `SSHPASS` set for
  sshpass).
- Unraid host IP defaults to `192.168.0.31` (override with
  `UNRAID_IP`).

Container exposes port 8000, mapped to host port 8404. App data volume
bind-mounted at `/data` (host `/mnt/user/appdata/filament-manager`),
holding SQLite database and `settings.json`.

### Dockerfile

Single-stage Python image. Entry point: `uvicorn
fm.api.app:create_app --factory --host 0.0.0.0 --port 8000`.

### Unraid XML Template

`deploy/filament-manager.xml` — Unraid Docker template for the
container. Must be manually added via Docker tab → Add Container →
select template (does not appear automatically).

---

## Development

### Local Dev Server

```bash
scripts/dev-start.sh   # uvicorn on :8404, isolated SQLite in .local/
scripts/dev-stop.sh    # deterministic stop via .local/server.pid
scripts/test.sh        # full pytest suite (322 tests)
```

`dev-start.sh` exports:
- Isolated `FM_DATABASE_URL=sqlite:///.local/filaments.db`
- Printer env defaults (`FM_PRINTER_IP`, `FM_SSH_USER`,
  `FM_SSH_PASSWORD`, `FM_DB_REMOTE_PATH`).

Override printer env vars inline if needed.

### Key Rules for Developers

1. **Designer role for UI**: All UI design/refinement work uses the omp
   designer agent.
2. **Scripts over repetition**: Any repeatable action goes into
   `scripts/`. Agents call scripts, never ad-hoc command sequences.
3. **Skill repo is reference material**: Logic is lifted from
   `~/Dokumente/Daten/Development/skills/Creality-custom-filament`.
   Never modify that repo.
4. **Dev server restart**: After changing UI routes (`fm/api/ui.py`),
   restart the dev server to pick up new endpoints. Templates and CSS are
   picked up without restart.
5. **Orca Cloud is the sole source**: Do not add local profile editing,
   preset generation, or catalog CRUD. Presets are created and tuned in
   OrcaSlicer Desktop and synced to Orca Cloud. The Manager caches them
   read-only.
6. **No model migrations**: Use existing columns and `SyncTarget.actual_state`
   for new data. Do not add columns to existing models. (Schema additions
   for new features go through `_migrate()` in `fm/db.py`.)

### Key Files

| File | Purpose |
|---|---|
| `fm/api/ui.py` | UI routes (pages, forms, HTMX fragments). Restart dev server after adding routes. |
| `fm/api/templates/` | Jinja2 templates. Changes picked up without restart. |
| `fm/api/static/app.css` | Stylesheet. Changes picked up without restart. |
| `fm/service/sync_handlers.py` | Sync logic: push, verify, github_backup, `compute_printer_db_drift`, printer DB operations. |
| `fm/service/cache_sync.py` | Orca Cloud cache sync: `sync_profile_cache`. |
| `fm/orca_cloud.py` | Orca Cloud Sync API client (pull, Supabase auth, token rotation). |
| `fm/github_backup.py` | GitHub backup client (commit presets as JSON). |
| `fm/presets/flattener.py` | Preset flattening, `find_preset_by_name`, `find_system_preset`. |
| `fm/settings_store.py` | settings.json read/write (printer config, Orca Cloud token, GitHub config). |

---

## Testing

```bash
scripts/test.sh        # full pytest suite (322 tests)
```

### Test Files

| File | Coverage |
|---|---|
| `tests/test_ui.py` | UI routes: spool forms, sync page, settings |
| `tests/test_ui_sync.py` | Sync Hub: drift table, busy dialog, job progress, resolve, push/backup modal |
| `tests/test_sync_handlers.py` | Sync handlers: push, verify, drift |
| `tests/test_sync_handlers_reshaped.py` | Reshaped push handler: cloud-first rebuild, orphan rule |
| `tests/test_sync_hub_reshaped.py` | Reshaped Sync Hub: preset & status table, orphan removal |
| `tests/test_spools_reshaped.py` | Reshaped spools: cloud_resource_id linking, orphan badge |
| `tests/test_cache_sync.py` | Orca Cloud cache sync: upsert, soft-delete, inheritance resolution, non-filament filtering, re-upload reconciliation |
| `tests/test_pipeline.py` | Push pipeline: MIN_DB check, busy check, backup, version bump, reboot |
| `tests/test_transport.py` | SSH transport: run, scp_push/pull, ws_request, busy check, reboot wait |
| `tests/test_flattener_k2.py` | Preset flattener: flatten_preset, find_preset_by_name |
| `tests/test_dbcodec.py` | Printer DB codec: load/save/insert/remove/validate |
| `tests/test_jobs.py` | Job queue: enqueue, claim_next, run_pending, complete, fail |
| `tests/test_api_sync.py` | Sync API: push, backup-github, drift, jobs |
| `tests/test_api_settings.py` | Settings API: save, load, test-connection, orca-connect/disconnect, github-test |
| `tests/test_settings_store.py` | settings.json: load, save, build_config, load_merged_config, orca token, github config |
| `tests/test_orca_cloud.py` | OrcaCloudClient: auth, sync_pull, cloud_id, conflict handling |
| `tests/test_github_backup.py` | GitHubBackupClient: commit presets as JSON |
| `tests/test_status_class_filter.py` | status_class Jinja2 filter |
| `tests/test_config.py` | Config dataclass, load_config, required env vars |
| `tests/test_models.py` | Model instantiation and defaults |
| `tests/test_migration.py` | Database migration: _migrate() column additions |
| `tests/test_template_index.py` | Template index loader (legacy, used by tests) |

### Test Conventions

- Tests use `pytest` with `conftest.py` providing in-memory SQLite
  fixtures and config injection.
- Transport is faked via monkeypatch of `_transport_factory`.
- Orca Cloud client is faked via monkeypatch of `_orca_client_factory`.
- GitHub client is faked via monkeypatch of `_github_client_factory`.
- No real SSH/SCP/WebSocket or Orca Cloud calls in tests.
- `conftest.py` sets up isolated database, test config, and app
  fixture.
