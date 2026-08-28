# Filament Manager — Technical Manual

Developer and administrator reference for the Filament Manager web
application: architecture, internal flows, data model, API surface,
deployment, and the companion agent protocol.

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Data Model](#data-model)
3. [Module Map](#module-map)
4. [Configuration](#configuration)
5. [Job Queue and Handler System](#job-queue-and-handler-system)
6. [Workflows and Internal Flows](#workflows-and-internal-flows)
   - [Profile CRUD](#profile-crud)
   - [Spool CRUD](#spool-crud)
   - [Printer DB Push](#printer-db-push)
   - [Verify](#verify)
   - [OrcaSlicer Preset Generation](#orcaslicer-preset-generation)
   - [Report Presets (Agent Scan)](#report-presets-agent-scan)
   - [Temperature Sync](#temperature-sync)
   [Preset Drift Detection](#preset-drift-detection)
   - [Migration (Import Scan)](#migration-import-scan)
   - [Import from OrcaSlicer](#import-from-orcaslicer)
7. [Printer DB Codec](#printer-db-codec)
8. [Preset Builder](#preset-builder)
9. [Preset Matching and Temp-Sync Target Selection](#preset-matching-and-temp-sync-target-selection)
10. [Companion Agent (fm-agent)](#companion-agent-fm-agent)
11. [Agent API Protocol](#agent-api-protocol)
12. [Transport Layer](#transport-layer)
13. [Push Pipeline](#push-pipeline)
14. [Iron Rules](#iron-rules)
15. [Settings Store](#settings-store)
16. [API Reference](#api-reference)
17. [Deployment](#deployment)
18. [Development](#development)
19. [Testing](#testing)

---

## Architecture Overview

```
┌─────────────────────────── Unraid container ───────────────────────────┐
│  Filament Manager (FastAPI + SQLite + Jinja2/HTMX)                     │
│  • Profiles + Spool inventory                                          │
│  • Sync engine: derives printer DB entries + preset payloads           │
│  • Printer sync: SSH/SCP push + version bump + reboot + verify         │
│  • Import from OrcaSlicer: standard presets → catalog profiles         │
│  • Settings page: printer connection config (settings.json)            │
│  • Agent job queue: apply_preset + report_presets                      │
└──────────────┬──────────────────────────────────────┬──────────────────┘
               │ REST API (token auth, LAN-only)       │ Agent pull API
│                │ (Bearer token)
┌──────────────▼───────────── Desktop ────────────────▼──────────────────┐
│  Companion agent (fm-agent, thin Python CLI)                           │
│  • Polls /api/agent/jobs for queued work                               │
│  • apply_preset: writes .json + .info to OrcaSlicer user filament dir  │
│    (refuses while OrcaSlicer runs; .info with empty sync_info)         │
│  • report_presets: scans user presets, resolves inherited presets,     │
│    reports 21 fields back for read-only display and temp sync          │
└────────────────────────────────────────────────────────────────────────┘
```

The catalog (SQLite) is the single source of truth. Printer DB entries
and OrcaSlicer presets are derived views. Drift is detected by comparing
expected vs. actual state via canonical hashes.

**Key design principle:** OrcaSlicer is the sole editor of print
settings. The Manager generates a starter preset, the user tunes in
OrcaSlicer, and the Manager displays the tuned values read-only after
sync. Print settings are never written back to the Profile model — they
live in `SyncTarget.actual_state` as JSON.

---

## Data Model

SQLAlchemy models in `fm/models.py`. Database is SQLite.

### Profile

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `brand` | str(255) | Manufacturer name |
| `name` | str(255) | Product name (must contain vendor) |
| `type` | str(64) | Material type (PLA, PETG, ABS, TPU, PA, PC, PLA-Silk, Other) |
| `colors` | str | JSON array of hex color values |
| `min_temp` | int | Safe minimum nozzle temperature (°C) |
| `max_temp` | int | Safe maximum nozzle temperature (°C) |
| `density` | float | Material density (g/cm³) |
| `drying_temp` | int | Recommended drying temperature (°C) |
| `drying_time` | int | Recommended drying time (hours) |
| `pa` | float \| None | Pressure advance (from OrcaSlicer sync) |
| `flow_ratio` | float \| None | Flow ratio (from OrcaSlicer sync) |
| `max_volumetric` | float \| None | Max volumetric speed (from OrcaSlicer sync) |
| `retraction_length` | float \| None | Retraction length (from OrcaSlicer sync) |
| `retraction_speed` | float \| None | Retraction speed (from OrcaSlicer sync) |
| `nozzle_temperature` | int \| None | Mirrors `kvParam.nozzle_temperature` (temp-sync target) |
| `printer_db_id` | str \| None | "99xxx" once pushed to printer DB |
| `filament_id` | str \| None | OrcaSlicer `filament_id` once preset generated |
| `notes` | str | Free-text notes |
| `created_at` | datetime | Creation timestamp (UTC) |
| `updated_at` | datetime | Last modification timestamp (UTC) |

Relationship: `spools` → list of `Spool` (back_populates).

### Spool

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `profile_id` | int FK → `profiles.id` | Referenced profile |
| `color_override` | str \| None | Hex color (overrides profile default) |
| `remaining_pct` | int | Material remaining (0–100, default 100) |
| `location` | str | Storage location |
| `rfid_tag` | str \| None | RFID tag identifier (unused in UI, kept for compatibility) |
| `notes` | str | Free-text notes |
| `created_at` | datetime | Creation timestamp (UTC) |
| `updated_at` | datetime | Last modification timestamp (UTC) |

Relationship: `profile` → `Profile` (back_populates).

### SyncTarget

Tracks sync state per profile per target kind.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `profile_id` | int FK → `profiles.id` | Referenced profile |
| `kind` | str(32) | `printer_db` or `orca_preset` |
| `expected_hash` | str \| None | Canonical hash of expected entry/preset |
| `actual_state` | str \| None | JSON string of actual state (preset data from agent) |
| `status` | str(16) | `in_sync` \| `pending` \| `drift_detected` \| `error` |
| `last_synced_at` | datetime \| None | Last sync attempt timestamp |

### Job

Asynchronous job queue for sync operations and agent tasks.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `kind` | str(32) | `push` \| `verify` \| `generate_preset` \| `apply_preset` \| `report_presets` \| `temp_sync` \| `import_scan` |
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
├── db.py                  # SQLAlchemy engine + SessionLocal
├── errors.py              # FmError exception
├── models.py              # SQLAlchemy models (Profile, Spool, SyncTarget, Job, ActivityLog)
├── settings_store.py      # settings.json read/write, load_merged_config()
├── api/
│   ├── __init__.py
│   ├── app.py             # FastAPI factory (create_app), router mounting
│   ├── catalog.py         # REST API: /api/profiles, /api/spools CRUD
│   ├── sync.py            # REST API: /api/sync/* (push, preset, tempsync, drift, jobs)
│   ├── agent.py           # Agent pull API: /api/agent/jobs, /api/agent/results
│   ├── ui.py              # UI routes (HTML pages, HTMX fragments, form handlers)
│   ├── templates/         # Jinja2 templates
│   └── static/            # CSS
├── service/
│   ├── __init__.py
│   ├── jobs.py            # Job queue: enqueue, claim_next, run_pending, complete, fail
│   └── sync_handlers.py   # Handlers: push, verify, generate_preset, temp_sync, import_scan
├── printer/
│   ├── __init__.py
│   ├── transport.py       # SshTransport (SSH/SCP/WS), PrinterTransport Protocol
│   ├── pipeline.py        # run_push(), verify_printer(), canonical_hash()
│   └── dbcodec.py         # Printer DB JSON codec: load/save/insert/remove/validate
├── presets/
│   ├── __init__.py
│   ├── builder.py         # build_standalone_preset(), generate_filament_id()
│   ├── matching.py        # match_presets(), temp_sync_targets(), find_orca_user_dir()
│   └── flattener.py       # Preset flattening (inherits resolution)
└── agent/
    ├── __init__.py
    └── cli.py             # fm-agent CLI: poll, watch, apply_preset, report_presets
```

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
    agent_token: str = ""
```

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
| `FM_AGENT_TOKEN` | *(secret)* | Shared token for agent auth |
| `FM_ORCASLICER_CONFIG_DIR` | `~/.config/OrcaSlicer` | Agent-side OrcaSlicer config dir override |
| `FM_SETTINGS_PATH` | `.local/settings.json` (dev) / `/data/settings.json` (container) | Path to settings.json |

---

## Job Queue and Handler System

The job queue (`fm/service/jobs.py`) drives all async operations.

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

`run_pending` processes jobs by kind:

- **Server-side handlers** (push, verify, generate_preset, temp_sync,
  import_scan) — executed immediately by `run_pending`.
- **Agent-facing kinds** (apply_preset, report_presets) — left queued;
  the agent claims them via `GET /api/agent/jobs`. `run_pending` skips
  kinds in `_AGENT_KINDS`.

### Injection Points (Testing)

`sync_handlers.py` exposes module-level injection points that tests
override via monkeypatch:

| Injection Point | Default | Purpose |
|---|---|---|
| `_transport_factory` | `lambda cfg: SshTransport(cfg)` | Replace SSH transport with fake |
| `_cache_db_path` | `None` (resolved at runtime) | Override printer DB cache path |
| `_import_db_path` | `None` | Pre-pulled DB for import_scan tests |
| `_preset_template_factory` | `lambda p: find_system_preset(p["type"])` | Override preset template lookup |
| `_sleep` | `time.sleep` | No-op sleep for tests |

---

## Workflows and Internal Flows

### Profile CRUD

**Create** (`POST /profiles/form`, `ui.py:180`):

1. Form fields: brand, name, type, colors, min_temp, max_temp, density,
   drying_temp, drying_time, notes.
2. Validation via `_validate()` → `dbcodec.validate_entry()`:
   - Required: brand, name, type, min_temp, max_temp.
   - Temp range: 100–400°C, min_temp < max_temp.
   - Density: 0.9–1.6.
   - Vendor-in-name warning (not blocking).
3. On error: re-render form with errors (HTTP 422).
4. On success: create `Profile` row, redirect to `/profiles` (303).

**Edit** (`POST /profiles/{pid}/form`, `ui.py:224`): same validation,
updates existing profile fields.

**Delete** (`DELETE /api/profiles/{pid}`, `catalog.py:285`): double
confirm via `confirm` query param (must match pid) or `force` flag.
Orphans `printer_db_id` entries are cleaned up during the next push
(`_rebuild_printer_db` removes orphaned 99xxx entries).

**REST API** (`catalog.py`): full CRUD at `/api/profiles` and
`/api/profiles/{pid}` with Pydantic schemas (`ProfileCreate`,
`ProfileUpdate`, `ProfileOut`).

### Spool CRUD

**Create** (`POST /spools/form`, `ui.py:381`): validates `profile_id`
exists, creates `Spool` row.

**Edit** (`POST /spools/{sid}/edit`, `ui.py:421`): inline form updates
profile_id, color_override, remaining_pct, location, notes.

**Duplicate** (`POST /spools/{sid}/duplicate`, `ui.py:458`): copies all
fields, resets `remaining_pct=100`.

**Delete** (`POST /spools/{sid}/delete`, `ui.py:448`): removes spool.

**Remaining update** (`POST /spools/{sid}/remaining`, `ui.py:407`):
HTMX endpoint, updates `remaining_pct` via slider, returns inline HTML
fragment.

**REST API** (`catalog.py`): full CRUD at `/api/spools` and
`/api/spools/{sid}` with Pydantic schemas (`SpoolCreate`, `SpoolUpdate`,
`SpoolOut`).

### Printer DB Push

**Trigger:** `POST /api/sync/push` (`sync.py:98`) or **Push to printer**
button on Sync page.

**Handler:** `handle_push` (`sync_handlers.py:274`).

**Flow:**

1. **Enqueue** push job with optional `profile_ids`, `force_reboot`,
   `no_reboot`.
2. **`_rebuild_printer_db`** (`sync_handlers.py:216`):
   - Load cached printer DB (`_load_cache_db`): pulls from printer via
     SCP on first run, then reuses local cache at
     `~/.cache/filament-manager/printer-db.json` (resolved by
     `_resolve_cache_path`).
   - For each target profile:
     - If `printer_db_id` exists: remove old entry by that ID.
     - If no `printer_db_id`: assign next free 99xxx ID via
       `next_free_id(db, config.id_range_start)`.
     - Map profile to values dict (`_profile_to_values`).
     - Build entry via `build_entry(db, values)` (clones template
       entry `01001`, overlays values).
     - Insert entry via `insert_entry(db, entry)`.
   - Remove orphaned 99xxx entries (IDs with no matching catalog
     profile).
   - Returns `(db, pushed)` where `pushed` is a list of
     `(profile, entry)` tuples.
3. **Save** rebuilt DB to temp file.
4. **`run_push`** (`pipeline.py:80`) — the iron-rule pipeline:
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
6. **On success:** update `SyncTarget(printer_db)` per pushed profile:
   - `expected_hash` = `_entry_hash(entry)` (canonical hash of identity
     + parameter fields).
   - `status = "pending"`.
   - `last_synced_at = now`.
7. **Enqueue verify** job automatically.

### Verify

**Trigger:** Enqueued automatically after push, or manually.

**Handler:** `handle_verify` (`sync_handlers.py:318`).

**Flow:**

1. Pull fresh DB from printer via SCP (ignores cache).
2. Version floor check: `int(version) >= config.version_override`. If
   below → `cloud_rollback = True` (cloud sync overwrote the DB).
3. For each profile with `printer_db_id`:
   - If `cloud_rollback`: `SyncTarget.status = "error"`.
   - If entry missing: `SyncTarget.status = "error"`.
   - If entry present: compare `_entry_hash(entry)` against
     `target.expected_hash`. Match → `"in_sync"`. Mismatch → `"error"`.
4. Log warning if cloud rollback detected.
5. Returns version_ok, present IDs, missing IDs, cloud_rollback flag.

### OrcaSlicer Preset Generation

**Trigger:** `POST /api/sync/preset/{profile_id}` (`sync.py:114`) or
preset generate button in drift table.

**Handler:** `handle_generate_preset` (`sync_handlers.py:385`).

**Flow:**

1. Load profile, build `profile_dict` with all catalog fields.
2. Find system preset template via `_preset_template_factory` →
   `find_system_preset(profile.type)`. Raises if no template found.
3. Build standalone preset via `build_standalone_preset(profile_dict,
   template)` (`fm/presets/builder.py`):
   - Clones template, overlays profile identity (name, brand, type).
   - Generates `filament_id` via `generate_filament_id(name, db_id)`:
     MD5 hash with "P" prefix, 14 chars.
   - Maps catalog optional fields to `kvParam`-style overrides via
     `_KV_MAP`.
   - Clears `inherits` (standalone, not inherited).
4. Build `.info` content: empty `sync_info`, `setting_id`, `base_id`;
   set `updated_time`.
5. Compute `expected_hash` from preset identity + parameter fields
   via `canonical_hash()`.
6. Update `profile.filament_id` with generated ID.
7. Set `SyncTarget(orca_preset)`: `expected_hash`, `status="pending"`.
8. Enqueue `apply_preset` job with preset JSON + .info content.

### Report Presets (Agent Scan)

**Trigger:** `POST /profiles/{pid}/sync-from-orca` (`ui.py:275`) or
"Scan now" on Import page.

**Flow (server-side):**

1. Enqueue `report_presets` job (empty payload).
2. Return HTMX fragment with syncing spinner state.
3. Job is agent-facing — left queued for `fm-agent` to claim.

**Flow (agent-side):** see [Companion Agent](#companion-agent-fm-agent).

**Post-processing** (`ui.py:287` `orca_fragment`):

1. HTMX polls `/profiles/{pid}/orca-fragment?job_id=N` every 3s.
2. If job queued/running and <5 min old: show spinner.
3. If job >5 min old: show stale state with retry button.
4. If job done: call `refresh_preset_drift(db)` to update
   `SyncTarget(orca_preset)` status, parse `actual_state` JSON, render
   12 read-only print settings.
5. If job error: show stale state.

### Temperature Sync

**Trigger:** `POST /api/sync/tempsync` (`sync.py:125`) or "Temperature
sync" button on Sync page.

**Handler:** `handle_temp_sync` (`sync_handlers.py:460`).

**Flow:**

1. Find most recent completed `report_presets` job. Raises if none.
2. Parse `presets` from job result.
3. Build `profiles_state` from catalog (only profiles with
   `printer_db_id`, or filtered by `profile_ids`).
4. Call `temp_sync_targets(profiles_state, presets)`
   (`fm/presets/matching.py:152`):
   - For each profile, find matching OrcaSlicer preset via
     `match_presets()` (vendor + name + type scoring).
   - Compare `nozzle_temperature` from preset vs catalog
     `nozzle_temperature`.
   - Returns `(updates, skipped)` where updates are profiles whose
     nozzle temp changed.
5. Apply `nozzle_temperature` changes to catalog Profile rows.
6. Enqueue `push` job to propagate temp changes to printer DB.

### Preset Drift Detection

**Function:** `refresh_preset_drift` (`sync_handlers.py:518`).

Called after `report_presets` completes. Compares each profile's
`SyncTarget(orca_preset).expected_hash` against the hash of the
reported preset data. Updates `status` to `in_sync` or `drift_detected`.
Also stores the full preset data in `SyncTarget.actual_state` as JSON.

### Migration (Import Scan)

**Trigger:** `POST /migration/import` (`ui.py:641`).

**Handler:** `handle_import_scan` (`sync_handlers.py:657`).

**Flow:**

1. Pull printer DB via SCP (or use pre-supplied `_import_db_path` for
   tests).
2. Extract all custom (99xxx) entries via `find_custom_entries(db)`.
3. Convert each to a proposal dict via `_entry_to_proposal`:
   - `printer_db_id`, `brand`, `name`, `type`, `min_temp`, `max_temp`,
     `density`, `drying_temp`, `drying_time`, `colors`,
     `nozzle_temperature`, `flow_ratio`, `pa`, `max_volumetric`.
4. Enqueue `report_presets` job (to populate preset data for linked
   profiles).
5. Returns `{"proposals": [...]}`.

**Apply** (`POST /migration/apply`, `ui.py:652`):

1. Read selected `printer_db_id` values from form checkboxes.
2. For each selected proposal:
   - If profile with same `printer_db_id` exists: update fields,
     **preserve `filament_id`** (Cloud-collision avoidance).
   - If new: create Profile with `printer_db_id` set, `filament_id=None`.
   - Create/update `SyncTarget(printer_db)` with `status="pending"`.

### Import from OrcaSlicer

**Trigger:** `GET /import-orca` (`ui.py:792`) + `POST /import-orca/apply`
(`ui.py:824`).

**Scan/display flow:**

1. `_unmatched_orca_presets(db)` (`ui.py:759`):
   - Load most recent completed `report_presets` job.
   - Get all preset data from result.
   - Filter: presets whose `filament_id` is not in any existing
     Profile's `filament_id`.
   - Only presets with `filament_id` + `filament_type` are importable.
2. Display as proposal table with: name, brand, type, nozzle temp, PA,
   flow ratio, inherits flag, user-tuned flag.

**Apply flow:**

1. For each selected `filament_id`:
   - Extract vendor, type, nozzle temp from preset data.
   - Create Profile with:
     - `printer_db_id = None` (standard filaments — no push).
     - `filament_id = fid` (links to OrcaSlicer preset).
     - `min_temp = max(temp - 15, 0)`, `max_temp = temp + 15`.
     - `nozzle_temperature = temp`.
2. Redirect to `/import-orca?applied=1`.

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

### Validation Rules

- Required: `brand`, `name`, `type`, `minTemp`, `maxTemp`.
- Temp range: 100–400°C, `minTemp < maxTemp`.
- Density: 0.9–1.6.
- Known types: PLA, PETG, ABS, TPU, PA, PC, ASA, PVA, HIPS, PETG-CF,
  PLA-CF.
- Warning (not error): name should contain vendor.

### Constants

| Constant | Value | Description |
|---|---|---|
| `MIN_DB_ENTRIES` | 30 | Minimum entries for push safety check |
| `TEMPLATE_ID` | `"01001"` | Template entry cloned by `build_entry` |

---

## Preset Builder

`fm/presets/builder.py` — builds standalone OrcaSlicer filament
presets from catalog profiles.

### `generate_filament_id(name, db_id="")`

MD5 hash of `"{name} {db_id}"`, prefixed with "P", 14 chars total.
Including `db_id` prevents collisions when regenerating presets for the
same filament name after Cloud deletion + re-creation.

### `build_standalone_preset(profile_dict, template)`

1. Deep-copy the system preset template.
2. Overlay profile identity: `name`, `filament_id`, `filament_vendor`
   (as array), `filament_type` (as array, type removed from name).
3. Map catalog optional fields to OrcaSlicer keys via `_KV_MAP`:

   | Catalog Field | OrcaSlicer Key |
   |---|---|
   | `nozzle_temperature` | `nozzle_temperature` |
   | `flow_ratio` | `filament_flow_ratio` |
   | `pa` | `pressure_advance` |
   | `max_volumetric` | `filament_max_volumetric_speed` |
   | `retraction_length` | `filament_retraction_length` |
   | `retraction_speed` | `filament_retraction_speed` |

4. Set `filament_density` as scalar.
5. Clear `inherits` (standalone).
6. Return flat preset dict.

### `write_info_file(preset_path, *, setting_id="")`

Writes `.info` file with key=value format. `sync_info` and `setting_id`
are empty so OrcaSlicer Cloud-Sync treats the preset as new (otherwise
it deletes the local file as "deleted in Cloud").

---

## Preset Matching and Temp-Sync Target Selection

`fm/presets/matching.py`

### `find_orca_user_dir(config_dir=None)`

Finds the first `~/.config/OrcaSlicer/user/<UUID>/filament/` directory.
Returns `Path` or `None`.

### `load_presets(config_dir=None)`

Loads all system + user filament presets from the OrcaSlicer config
directory.

### `match_presets(presets, vendor, brand_name, base_type)`

Simulates `CrealityPrintAgent::match_filament_preset` scoring. Scores
each preset by vendor match, name substring match, and type match.
Returns `(matches, considered)`.

### `temp_sync_targets(profiles_state, presets)`

Selects temp-sync targets across catalog profiles:

1. For each profile, find best-matching OrcaSlicer preset via
   `match_presets()`.
2. Extract `nozzle_temperature` from the matched preset.
3. Compare against the profile's current `nozzle_temperature`.
4. If different: add to `updates` list with `profile_id`, `orca_temp`,
   `old_temp`.
5. If no match or same temp: add to `skipped`.
6. Returns `(updates, skipped)`.

### `is_orcaslicer_running()`

Checks for OrcaSlicer process via `pgrep`/`tasklist`. Returns bool.
Used by the agent before writing preset files.

---

## Companion Agent (fm-agent)

`fm/agent/cli.py` — thin Python CLI that runs on the desktop PC and
polls the app's agent job queue.

### Configuration

- Config file: `~/.config/filament-manager/agent.json`
  ```json
  {"base_url": "http://192.168.0.31:8404", "token": "<FM_AGENT_TOKEN>"}
  ```
- CLI overrides: `--base-url`, `--token`
- Env override: `FM_ORCASLICER_CONFIG_DIR` for non-default OrcaSlicer
  config directory.

### Subcommands

| Command | Description |
|---|---|
| `fm-agent poll` | Claim and process at most one queued job, then exit. Returns 0 on success. |
| `fm-agent watch` | Poll loop every 60 seconds. Runs continuously until interrupted. |

### Job Handlers

#### `apply_preset` (`_handle_apply_preset`)

1. Check `is_orcaslicer_running()` — if running, return error
   `{"error": "OrcaSlicer running"}`.
2. Extract `name`, `preset` (dict), `info` (string) from job payload.
3. Find OrcaSlicer user filament dir via `find_orca_user_dir()`.
4. Write `{name}.json` (preset JSON) and `{name}.info` (info content)
   to the filament dir.
5. Return `{"name": name, "files": ["{name}.json", "{name}.info"]}`.

#### `report_presets` (`_handle_report_presets`)

1. Find OrcaSlicer user filament dir.
2. Build system preset index (`_load_system_preset_index`) for
   inheritance resolution.
3. For each `*.json` in the filament dir:
   - Parse JSON, skip non-filament types.
   - If preset has `inherits` field: resolve via
     `_resolve_inheritance(d, system_index)`:
     - Start with parent system preset fields.
     - Overlay child overrides (child wins).
     - Preserve child's `name` and `inherits`.
     - Fill missing `filament_type` from parent name via
       `_derive_filament_type()`.
   - Build report dict via `_preset_report()` with 21 fields:
     `name`, `filament_type`, `filament_vendor`, `nozzle_temperature`,
     `nozzle_temperature_initial_layer`, `inherits`, `from`,
     `filament_id`, `_path`, `_system`, `pressure_advance`,
     `enable_pressure_advance`, `filament_flow_ratio`,
     `filament_max_volumetric_speed`, `filament_retraction_length`,
     `filament_retraction_speed`, `fan_min_speed`, `fan_max_speed`,
     `cool_plate_temp`, `eng_plate_temp`, `hot_plate_temp`.
4. Return `{"presets": [...]}`.

### Filament Type Derivation

`_derive_filament_type(name)` — extracts filament type from preset name
by matching known types ordered by specificity (longer matches first to
avoid PLA matching PLA-CF). Falls back to "PLA" for unrecognizable
Creality preset names.

### Agent Reinstall

After changing agent code (`fm/agent/cli.py`):

```bash
uv tool install . --force --no-cache
```

`--no-cache` is required to pick up code changes. Then restart the
agent process.

---

## Agent API Protocol

`fm/api/agent.py` — token-authed endpoints for the agent job queue.

### Authentication

Bearer token via `Authorization: Bearer <token>`. Token validated
against `config.agent_token`. Returns 401 on mismatch.

### Endpoints

#### `GET /api/agent/jobs`

Claims the oldest queued agent-facing job (`queued → running`).

Agent-facing kinds: `apply_preset`, `report_presets` (defined in
`_AGENT_KINDS`).

Returns:
```json
{
  "jobs": [{
    "job_id": 42,
    "kind": "apply_preset",
    "status": "running",
    "payload": {...},
    "result": null
  }]
}
```

Returns `{"jobs": []}` if no queued agent jobs.

#### `POST /api/agent/results`

Records the outcome of a claimed job.

Request body:
```json
{
  "job_id": 42,
  "status": "done",
  "result": {...}
}
```

- On `status="done"`: calls `jobs.complete(job, result)`. If kind is
  `report_presets`, also calls `refresh_preset_drift(session)` to
  update SyncTarget statuses and store preset data in
  `actual_state`.
- On `status="error"`: calls `jobs.fail(job, result)`.

Returns the updated job serialization.

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

**Order (Global Constraint 1, load-bearing):**

1. **MIN_DB_ENTRIES check** — `len(db.result.list) >= 30`. Refuses
   with `FmError(EXIT_VALIDATE)` unless `force_push`.
2. **Busy check** — `check_printer_busy(transport)`. If busy and not
   `force_reboot`: return `{"status": "busy", "job": details}` without
   touching the printer.
3. **Backup** — `transport.run("cp ... .bak.{ts}")` + rotate (keep 5).
4. **Version bump** — `bump_version(db, config.version_override)`.
   Auto-increments if current ≥ override. Ensures CFS RFID app sees
   the update.
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

### `verify_printer(config, transport, expected_ids)`

Pulls DB fresh from printer, checks version floor + entry presence.
Returns `{"version_ok", "present", "missing"}`.

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
2. **Profile name must contain the vendor** ("Sunlu PLA+", not "PLA+").
   Enforced as a warning in `validate_entry()`.
3. **Custom printer IDs only in 99xxx range.** Stock IDs protected.
   Enforced in `remove_entry()`, `patch_entry()` via `_is_custom_id()`.
4. **Backup on printer before every write.** Rotate, keep 5. Enforced
   in `run_push()`.
5. **Double-confirm on delete.** Typed token must match. Enforced in
   `DELETE /api/profiles/{pid}`.
6. **Refuse push when DB < 30 entries** (`MIN_DB_ENTRIES`) unless
   explicitly forced. Enforced in `run_push()`.
7. **Never write OrcaSlicer presets while OrcaSlicer is running.**
   `.info` gets empty `sync_info`/`setting_id`. Enforced in
   `_handle_apply_preset()`.

---

## Settings Store

`fm/settings_store.py` — persistent printer config in `settings.json`.

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

### Settings Page Flow

- `GET /settings` (`ui.py:918`): render form pre-filled from current
  config or stored settings.
- `POST /settings` (`ui.py:928`): validate required fields, parse ints,
  `save_settings()`, update `app.state.config`, redirect.
- `POST /api/settings/test-connection` (`ui.py:988`): SSH to printer,
  check DB path exists. Returns `{"ok": bool, "error": str \| None}`.

---

## API Reference

### Catalog API (`/api`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/profiles` | List all profiles |
| POST | `/api/profiles` | Create profile |
| GET | `/api/profiles/{pid}` | Get profile |
| PATCH | `/api/profiles/{pid}` | Update profile |
| DELETE | `/api/profiles/{pid}?confirm={pid}` | Delete profile (double-confirm) |
| POST | `/api/profiles/{pid}/validate` | Get validation warnings |
| GET | `/api/spools` | List all spools |
| POST | `/api/spools` | Create spool |
| PATCH | `/api/spools/{sid}` | Update spool |
| DELETE | `/api/spools/{sid}` | Delete spool |

### Sync API (`/api`)

| Method | Path | Description |
|---|---|---|
| POST | `/api/sync/push` | Enqueue push job, drain queue |
| POST | `/api/sync/preset/{profile_id}` | Enqueue generate_preset job |
| POST | `/api/sync/tempsync` | Enqueue temp_sync job |
| GET | `/api/status/drift` | Per-profile drift matrix |
| POST | `/api/jobs/{job_id}/resolve` | Resolve awaiting_input job |
| GET | `/api/jobs?since={id}` | List jobs since ID |
| GET | `/api/jobs/{job_id}` | Get single job |

### Agent API (`/api/agent`)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/agent/jobs` | Bearer | Claim oldest queued agent job |
| POST | `/api/agent/results` | Bearer | Report job result |

### Meta

| Method | Path | Description |
|---|---|---|
| GET | `/api/version` | App version |

### UI Routes

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/profiles` | Profiles list with filters |
| GET | `/profiles/new` | New profile form |
| GET | `/profiles/{pid}/edit` | Edit profile form |
| POST | `/profiles/form` | Create profile (form handler) |
| POST | `/profiles/{pid}/form` | Update profile (form handler) |
| POST | `/profiles/{pid}/sync-from-orca` | Enqueue report_presets, return HTMX fragment |
| GET | `/profiles/{pid}/orca-fragment` | HTMX fragment: read-only preset values |
| GET | `/spools` | Spool inventory with filters |
| POST | `/spools/form` | Create spool |
| POST | `/spools/{sid}/remaining` | HTMX: update remaining_pct |
| POST | `/spools/{sid}/edit` | Edit spool |
| POST | `/spools/{sid}/delete` | Delete spool |
| POST | `/spools/{sid}/duplicate` | Duplicate spool (remaining=100) |
| GET | `/sync` | Sync page (drift table, actions, job progress) |
| GET | `/sync/progress?since={id}` | HTMX fragment: job progress table |
| POST | `/sync/jobs/{job_id}/resolve` | HTMX: resolve awaiting_input job |
| GET | `/migration` | Migration wizard |
| POST | `/migration/import` | Start import scan |
| POST | `/migration/apply` | Apply selected proposals |
| GET | `/import-orca` | Import from OrcaSlicer page |
| POST | `/import-orca/apply` | Import selected presets |
| GET | `/settings` | Settings page |
| POST | `/settings` | Save settings |
| POST | `/api/settings/test-connection` | Test SSH connection |

---

## Deployment

### Container (Unraid)

The image is built and loaded locally — no registry.

```bash
scripts/release.sh 0.1.0          # tag vX.Y.Z, build image, save dist/*.tar.gz
scripts/deploy-unraid.sh 0.1.0    # load image + XML template on Unraid, restart
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
scripts/test.sh        # full pytest suite
```

`dev-start.sh` exports:
- Dev `FM_AGENT_TOKEN`
- Isolated `FM_DATABASE_URL=sqlite:///.local/filaments.db`

Override printer env vars inline if needed.

### Key Rules for Developers

1. **Designer role for UI**: All UI design/refinement work uses the omp
   designer agent.
2. **Scripts over repetition**: Any repeatable action goes into
   `scripts/`. Agents call scripts, never ad-hoc command sequences.
3. **Skill repo is reference material**: Logic is lifted from
   `~/Dokumente/Daten/Development/skills/Creality-custom-filament`.
   Never modify that repo.
4. **Agent reinstall**: After changing `fm/agent/cli.py`, reinstall
   with `uv tool install . --force --no-cache`.
5. **Dev server restart**: After changing UI routes (`fm/api/ui.py`),
   restart the dev server to pick up new endpoints. Templates and CSS
   are picked up without restart.
6. **OrcaSlicer is the editor**: Do not add print-settings fields to
   the Profile model or the profile edit form. Print settings come from
   OrcaSlicer via `report_presets` and are stored in
   `SyncTarget.actual_state`.
7. **No model migrations**: Use existing columns and
   `SyncTarget.actual_state` for new data. Do not add columns to
   Profile or Spool models.

### Key Files

| File | Purpose |
|---|---|
| `fm/api/ui.py` | UI routes (pages, forms, HTMX fragments). Restart dev server after adding routes. |
| `fm/api/templates/` | Jinja2 templates. Changes picked up without restart. |
| `fm/api/static/app.css` | Stylesheet. Changes picked up without restart. |
| `fm/agent/cli.py` | Agent CLI. Reinstall with `uv tool install . --force --no-cache` after changes. |
| `fm/service/sync_handlers.py` | Sync logic, `refresh_preset_drift`, printer DB operations. |
| `fm/presets/matching.py` | OrcaSlicer preset matching, `find_orca_user_dir`, `load_presets`. |
| `fm/presets/builder.py` | Preset builder, `_KV_MAP` (catalog → Orca key mapping). |
| `fm/settings_store.py` | settings.json read/write (printer connection config). |

---

## Testing

```bash
scripts/test.sh        # full pytest suite
```

### Test Files

| File | Coverage |
|---|---|
| `tests/test_ui.py` | UI routes: profile forms, spool forms, sync page, migration, settings |
| `tests/test_ui_sync.py` | Sync page: drift table, busy dialog, job progress, resolve |
| `tests/test_sync_handlers.py` | Sync handlers: push, verify, generate_preset, temp_sync, import_scan, drift |
| `tests/test_pipeline.py` | Push pipeline: MIN_DB check, busy check, backup, version bump, reboot |
| `tests/test_transport.py` | SSH transport: run, scp_push/pull, ws_request, busy check, reboot wait |
| `tests/test_matching.py` | Preset matching, temp_sync_targets, find_orca_user_dir |
| `tests/test_jobs.py` | Job queue: enqueue, claim_next, run_pending, complete, fail |
| `tests/test_api_settings.py` | Settings API: save, load, test-connection |
| `tests/test_settings_store.py` | settings.json: load, save, build_config, load_merged_config |
| `tests/test_models.py` | Model instantiation and defaults |

### Test Conventions

- Tests use `pytest` with `conftest.py` providing in-memory SQLite
  fixtures and config injection.
- Transport is faked via monkeypatch of `_transport_factory`.
- No real SSH/SCP/WebSocket calls in tests.
- `conftest.py` sets up isolated database, test config, and app
  fixture.
