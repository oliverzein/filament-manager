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
   - [Profile CRUD](#profile-crud)
   - [Spool CRUD](#spool-crud)
   - [Printer DB Push](#printer-db-push)
   - [Verify](#verify)
   - [Orca Cloud Preset Generation](#orca-cloud-preset-generation)
   - [Orca Cloud Sync (Profile ↔ Cloud)](#orca-cloud-sync-profile--cloud)
   - [Temperature Sync](#temperature-sync)
   - [Preset Drift Detection](#preset-drift-detection)
   - [Migration (Import Scan)](#migration-import-scan)
   - [Import from Orca Cloud](#import-from-orca-cloud)
7. [Printer DB Codec](#printer-db-codec)
8. [Preset Builder](#preset-builder)
9. [Preset Matching and Temp-Sync Target Selection](#preset-matching-and-temp-sync-target-selection)
10. [Template Selection](#template-selection)
11. [Orca Cloud Integration](#orca-cloud-integration)
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
│  • Orca Cloud integration: push/pull/delete presets (Supabase auth)    │
│  • Orca Cloud Import: cloud presets → catalog profiles                 │
│  • Settings page: printer connection + Orca Cloud credentials          │
│  • Job queue: push/verify/generate_preset/temp_sync/import_scan        │
│    (drained synchronously inside requests — no scheduler/worker)       │
└──────────────┬──────────────────────────────────────┬──────────────────┘
               │ SSH/SCP/WS (LAN-only)                │ HTTPS
┌──────────────▼───────────────────────────┐  ┌────────▼─────────────────────┐
│  Creality K2 printer                      │  │  Orca Cloud (Supabase-backed)│
│  • material_database.json                 │  │  • Preset sync API           │
│  • Version bump + reboot on every write   │  │  • Refresh-token rotation    │
└───────────────────────────────────────────┘  └──────────────────────────────┘
```

The catalog (SQLite) is the single source of truth. Printer DB entries
and Orca Cloud presets are derived views. Drift is detected by comparing
expected vs. actual state via canonical hashes.

**OrcaSlicer** is the desktop slicer application. It exists only on the
dev machine, where it is the sole editor of print settings and the
source for preset **bundling** (see [Template Selection](#template-selection)).
**Orca Cloud** is the cloud sync service — the runtime source/target of
preset sync and import. The Manager never writes to OrcaSlicer directly;
it pushes presets to Orca Cloud, which OrcaSlicer syncs on the desktop.

**Key design principle:** OrcaSlicer is the sole editor of print
settings. The Manager generates a starter preset, the user tunes in
OrcaSlicer, and the Manager displays the tuned values read-only after
sync. Print settings are never written back to the Profile model — they
live in `SyncTarget.actual_state` as JSON.

---

## Data Model

SQLAlchemy models in `fm/models.py`. Database is SQLite. Schema
migrations are handled by `_migrate()` in `fm/db.py`, which adds new
columns via `ALTER TABLE ADD COLUMN` on engine init.

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
| `pa` | float \| None | Pressure advance (from Orca Cloud sync) |
| `flow_ratio` | float \| None | Flow ratio (from Orca Cloud sync) |
| `max_volumetric` | float \| None | Max volumetric speed (from Orca Cloud sync) |
| `retraction_length` | float \| None | Retraction length (from Orca Cloud sync) |
| `retraction_speed` | float \| None | Retraction speed (from Orca Cloud sync) |
| `nozzle_temperature` | int \| None | Mirrors `kvParam.nozzle_temperature` (temp-sync target) |
| `printer_db_id` | str \| None | "99xxx" once pushed to printer DB |
| `filament_id` | str \| None | Orca Cloud `filament_id` once preset generated |
| `template_override` | str(255) \| None | Selected preset template name (overrides generic type default) |
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
| `cloud_id` | str(64) \| None | Orca Cloud resource UUID (deterministic link to a cloud preset) |
| `expected_hash` | str \| None | Canonical hash of expected entry/preset |
| `actual_state` | str \| None | JSON string of actual state (preset data from Orca Cloud) |
| `status` | str(16) | `in_sync` \| `pending` \| `drift` \| `error` |
| `last_synced_at` | datetime \| None | Last sync attempt timestamp |

### Job

Job queue for sync operations. Jobs are drained synchronously inside
API/UI request paths — there is no scheduler and no background worker.

| Column | Type | Description |
|---|---|---|
| `id` | int PK | Auto-increment |
| `kind` | str(32) | `push` \| `verify` \| `generate_preset` \| `temp_sync` \| `import_scan` |
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
├── models.py              # SQLAlchemy models (Profile, Spool, SyncTarget, Job, ActivityLog)
├── orca_cloud.py          # OrcaCloudClient — Orca Cloud Sync API (push/pull/delete, Supabase auth)
├── settings_store.py      # settings.json read/write, load_merged_config(), Orca Cloud token storage
├── api/
│   ├── __init__.py
│   ├── app.py             # FastAPI factory (create_app), router mounting
│   ├── catalog.py         # REST API: /api/profiles, /api/spools CRUD, /api/templates
│   ├── sync.py            # REST API: /api/sync/* (push, preset, tempsync, drift, jobs)
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
└── presets/
    ├── __init__.py
    ├── builder.py         # build_standalone_preset(), generate_filament_id()
    ├── matching.py        # match_presets(), temp_sync_targets(), find_orca_user_dir()
    ├── flattener.py       # Preset flattening, find_preset_by_name, find_system_preset
    ├── template_index.py  # Runtime loader for template_index.json
    └── templates/         # Bundled @base preset templates (committed to git)
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

- **push** — rebuild printer DB, upload, reboot, enqueue verify.
- **verify** — pull fresh DB, version-floor + presence + hash check.
- **generate_preset** — build preset, push directly to Orca Cloud.
- **temp_sync** — pull presets from Orca Cloud, update nozzle temps.
- **import_scan** — pull printer DB, extract 99xxx entries as proposals.

### Injection Points (Testing)

`sync_handlers.py` exposes module-level injection points that tests
override via monkeypatch:

| Injection Point | Default | Purpose |
|---|---|---|
| `_transport_factory` | `lambda cfg: SshTransport(cfg)` | Replace SSH transport with fake |
| `_cache_db_path` | `None` (resolved at runtime) | Override printer DB cache path |
| `_import_db_path` | `None` | Pre-pulled DB for import_scan tests |
| `_preset_template_factory` | resolves override → flatten, else `find_system_preset` | Override preset template lookup |
| `_orca_client_factory` | `_default_orca_client` (loads token from settings) | Replace Orca Cloud client with fake |
| `_sleep` | `time.sleep` | No-op sleep for tests |

---

## Workflows and Internal Flows

### Profile CRUD

**Create** (`POST /profiles/form`, `ui.py:283`):

1. Form fields: brand, name, type, colors, min_temp, max_temp, density,
   drying_temp, drying_time, notes, template_override.
2. Validation via `_validate()` → `dbcodec.validate_entry()`:
   - Required: brand, name, type, min_temp, max_temp.
   - Temp range: 100–400°C, min_temp < max_temp.
   - Density: 0.9–1.6.
   - Vendor-in-name warning (not blocking).
3. Server-side template validation: if `template_override` is set, it is
   checked against `template_index.json`; a template whose `type` does
   not match the submitted type is cleared (set to empty).
4. On error: re-render form with errors (HTTP 422).
5. On success: create `Profile` row, redirect to `/profiles` (303).

**Edit** (`POST /profiles/{pid}/form`, `ui.py:335`): same validation
and template-check, updates existing profile fields.

**Delete** (`DELETE /api/profiles/{pid}`, `catalog.py:289`): double
confirm via `confirm` query param (must match pid) or `force` flag.
Orphans `printer_db_id` entries are cleaned up during the next push
(`_rebuild_printer_db` removes orphaned 99xxx entries).

**REST API** (`catalog.py`): full CRUD at `/api/profiles` and
`/api/profiles/{pid}` with Pydantic schemas (`ProfileCreate`,
`ProfileUpdate`, `ProfileOut`).

### Spool CRUD

**Create** (`POST /spools/form`, `ui.py:505`): validates `profile_id`
exists, creates `Spool` row.

**Edit** (`POST /spools/{sid}/edit`, `ui.py:545`): inline form updates
profile_id, color_override, remaining_pct, location, notes.

**Duplicate** (`POST /spools/{sid}/duplicate`, `ui.py:582`): copies all
fields, resets `remaining_pct=100`.

**Delete** (`POST /spools/{sid}/delete`, `ui.py:572`): removes spool.

**Remaining update** (`POST /spools/{sid}/remaining`, `ui.py:531`):
HTMX endpoint, updates `remaining_pct` via slider, returns inline HTML
fragment.

**REST API** (`catalog.py`): full CRUD at `/api/spools` and
`/api/spools/{sid}` with Pydantic schemas (`SpoolCreate`, `SpoolUpdate`,
`SpoolOut`).

### Printer DB Push

**Trigger:** `POST /api/sync/push` (`sync.py:98`) or **Push to printer**
button on Sync page.

**Handler:** `handle_push` (`sync_handlers.py:368`).

**Flow:**

1. **Enqueue** push job with optional `profile_ids`, `force_reboot`,
   `no_reboot`. `run_pending` drains the queue synchronously.
2. **`_rebuild_printer_db`** (`sync_handlers.py:310`):
   - Load cached printer DB (`_load_cache_db`): pulls from printer via
     SCP on first run, then reuses local cache (resolved by
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
7. **Enqueue verify** job automatically (drained on the next
   `run_pending` call).

### Verify

**Trigger:** Enqueued automatically after push, or manually.

**Handler:** `handle_verify` (`sync_handlers.py:412`).

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

### Orca Cloud Preset Generation

**Trigger:** the Sync page drift-table generate button (per profile) or
`POST /api/sync/preset/{profile_id}` (`sync.py:114`).

**Handler:** `handle_generate_preset` (`sync_handlers.py:479`).

**Flow:**

1. Load profile, build `profile_dict` with all catalog fields including
   `template_override`.
2. Resolve the preset template via `_preset_template_factory`
   (`sync_handlers.py:73`):
   - If `template_override` is set: find the named `@base` template via
     `find_preset_by_name(override + " @base")` and flatten it with
     `flatten_preset`.
   - If the override file is missing: `log.warning` and fall back to the
     generic type default via `find_system_preset(profile.type)`.
   - If no override: use `find_system_preset(profile.type)`.
   - Raises if no template can be resolved.
3. Build standalone preset via `build_standalone_preset(profile_dict,
   template)` (`fm/presets/builder.py:61`):
   - Clones template, overlays profile identity (name, brand, type).
   - Generates `filament_id` via `generate_filament_id(name, db_id)`:
     MD5 hash with "P" prefix, 14 chars.
   - Maps catalog optional fields to `kvParam`-style overrides via
     `_KV_MAP`.
   - Clears `inherits` (standalone, not inherited).
4. Compute `expected_hash` from preset identity + parameter fields
   via `canonical_hash()`.
5. Update `profile.filament_id` with generated ID.
6. Set `SyncTarget(orca_preset)`: `expected_hash`, `status="pending"`,
   `cloud_id=None` (set after successful push).
7. **Push directly to Orca Cloud** via `OrcaCloudClient.sync_push`
   (`fm/orca_cloud.py:222`):
   - On 409 conflict: retry once with the server's `updated_time` to
     force an update. If still conflicting, set `status="drift"`.
   - On success: store `target.cloud_id = client.cloud_id(filament_id)`,
     `status="in_sync"`, `last_synced_at=now`.

Changing a profile's template has no effect until the next
generate-preset run.

### Orca Cloud Sync (Profile ↔ Cloud)

**Trigger:**

- `POST /profiles/sync-from-orca` (`ui.py:176`) — sync **all** profiles.
- `POST /profiles/{pid}/sync-from-orca` (`ui.py:390`) — sync a single
  profile, returns the orca-fragment inline.
- `GET /profiles/{pid}/orca-fragment` (`ui.py:430`) — read-only
  print-settings display.

**Flow (synchronous, server-side):**

1. `refresh_preset_drift(db)` (`sync_handlers.py:645`) pulls all
   presets from Orca Cloud via `OrcaCloudClient.sync_pull` and converts
   upserts to local preset format.
2. For each existing `SyncTarget(orca_preset)`:
   - If `cloud_id` is set: match by exact cloud resource UUID
     ( mirrors OrcaSlicer's `setting_id` approach).
   - If `cloud_id` is unset (legacy target): discover via name matching
     (`match_presets`, score ≥ 30) and store the cloud `id` as
     `cloud_id` for future exact matches.
   - If matched: compare `canonical_hash` of the preset identity +
     `nozzle_temperature` against `expected_hash`. Match → `"in_sync"`,
     mismatch → `"drift"`. Store full preset in `actual_state` as JSON.
   - If not in cloud (deleted): `status="drift"`, `actual_state=None`.
3. Profiles without an `orca_preset` SyncTarget are skipped (appear as
   `pending` in the drift table).

**Orca-fragment display** (`orca_fragment.html`):

- Renders up to 12 read-only print settings from `actual_state` JSON.
- Shows `template: <name>` — the `template_override` value, or
  `Generic <type> defaults` when no override is set.
- Print settings are read-only in the Manager; OrcaSlicer remains the
  sole print-settings editor.

### Temperature Sync

**Trigger:** `POST /api/sync/tempsync` (`sync.py:125`) or "Temperature
sync" button on Sync page.

**Handler:** `handle_temp_sync` (`sync_handlers.py:591`).

**Flow:**

1. Pull presets directly from Orca Cloud via
   `OrcaCloudClient.sync_pull` and convert to local format.
2. Build `profiles_state` from catalog (only profiles with
   `printer_db_id`, or filtered by `profile_ids`).
3. Call `temp_sync_targets(profiles_state, presets)`
   (`fm/presets/matching.py:152`):
   - For each profile, find matching Orca Cloud preset via
     `match_presets()` (vendor + name + type scoring).
   - Compare `nozzle_temperature` from preset vs catalog
     `nozzle_temperature`.
   - Returns `(updates, skipped)` where updates are profiles whose
     nozzle temp changed.
4. Apply `nozzle_temperature` changes to catalog Profile rows.
5. Enqueue `push` job to propagate temp changes to printer DB.

### Preset Drift Detection

**Function:** `refresh_preset_drift` (`sync_handlers.py:645`).

Called after Orca Cloud sync (single or all profiles). Compares each
profile's `SyncTarget(orca_preset).expected_hash` against the hash of
the pulled preset data. Updates `status` to `in_sync` or `drift`. Also
stores the full preset data in `SyncTarget.actual_state` as JSON.

### Migration (Import Scan)

**Trigger:** `POST /migration/import` (`ui.py:765`).

**Handler:** `handle_import_scan` (`sync_handlers.py:807`).

**Flow:**

1. Pull printer DB via SCP (or use pre-supplied `_import_db_path` for
   tests).
2. Extract all custom (99xxx) entries via `find_custom_entries(db)`.
3. Convert each to a proposal dict via `_entry_to_proposal`:
   - `printer_db_id`, `brand`, `name`, `type`, `min_temp`, `max_temp`,
     `density`, `drying_temp`, `drying_time`, `colors`,
     `nozzle_temperature`, `flow_ratio`, `pa`, `max_volumetric`.
4. Returns `{"proposals": [...]}`.

**Apply** (`POST /migration/apply`, `ui.py:776`):

1. Read selected `printer_db_id` values from form checkboxes.
2. For each selected proposal:
   - If profile with same `printer_db_id` exists: update fields,
     **preserve `filament_id`** (Cloud-collision avoidance).
   - If new: create Profile with `printer_db_id` set, `filament_id=None`.
   - Create/update `SyncTarget(printer_db)` with `status="pending"`.

### Import from Orca Cloud

**Trigger:** `GET /import-orca` (`ui.py:921`) + `POST /import-orca/apply`
(`ui.py:953`). Nav label **"Orca Cloud Import"**, page heading
**"Import from Orca Cloud"**.

**Scan/display flow:**

1. `_pull_orca_presets()` (`ui.py:874`): create an `OrcaCloudClient`
   from stored credentials, call `sync_pull`, convert upserts to local
   preset format. Returns `[]` if no credentials or the API call fails.
2. `_unmatched_orca_presets(db)` (`ui.py:895`):
   - A preset is **matched** if its `filament_id` links to an existing
     Profile's `filament_id`. Unmatched presets are import candidates.
   - Only presets with `filament_id` + `filament_type` are importable.
3. Display as proposal table with: name, brand, type, nozzle temp, PA,
   flow ratio, inherits flag, user-tuned flag.

**Apply flow:**

1. For each selected `filament_id`:
   - Extract vendor, type, nozzle temp from preset data.
   - Create Profile with:
     - `printer_db_id = None` (standard filaments — no push).
     - `filament_id = fid` (links to Orca Cloud preset).
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

`fm/presets/builder.py` — builds standalone Orca Cloud filament
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
it deletes the local file as "deleted in Cloud"). This utility is kept
for reference; the Manager pushes presets to Orca Cloud directly rather
than writing local `.info` files.

---

## Preset Matching and Temp-Sync Target Selection

`fm/presets/matching.py`

### `find_orca_user_dir(config_dir=None)`

Finds the first `~/.config/OrcaSlicer/user/<UUID>/filament/` directory.
Returns `Path` or `None`. Used by `load_presets` on the dev machine.

### `load_presets(config_dir=None)`

Loads all system + user filament presets from the OrcaSlicer config
directory. Dev-machine utility; the running app reads presets from
Orca Cloud instead.

### `match_presets(presets, vendor, brand_name, base_type)`

Simulates `CrealityPrintAgent::match_filament_preset` scoring. Scores
each preset by vendor match, name substring match, and type match.
Returns `(matches, considered)`. Used by `refresh_preset_drift` (legacy
name-based discovery) and `temp_sync_targets`.

### `temp_sync_targets(profiles_state, presets)`

Selects temp-sync targets across catalog profiles:

1. For each profile, find best-matching Orca Cloud preset via
   `match_presets()`.
2. Extract `nozzle_temperature` from the matched preset.
3. Compare against the profile's current `nozzle_temperature`.
4. If different: add to `updates` list with `profile_id`, `orca_temp`,
   `old_temp`.
5. If no match or same temp: add to `skipped`.
6. Returns `(updates, skipped)`.

### `is_orcaslicer_running()`

Checks for OrcaSlicer process via `pgrep`/`tasklist`. Returns bool.
Dev-machine utility lifted from the skill repo.

---

## Template Selection

Preset templates let a profile start from a brand-specific `@base`
preset instead of the generic type default. Templates are bundled into
the repo so the app and Docker image are self-contained — servers need
no OrcaSlicer installation.

### One-off Bundler (`scripts/build_templates.py`)

A **dev-time-only** script, run on the dev machine where OrcaSlicer is
installed. It is **not** part of the runtime or the Docker image.

1. Scans `/opt/orca-slicer/resources/profiles` for `@base` filament
   presets (~356 files).
2. Deduplicates by display name (preferring vendor-specific copies over
   OrcaFilamentLibrary copies).
3. Copies them into `fm/presets/templates/`.
4. Generates `fm/presets/template_index.json` (352 indexed entries) with
   vendor + type metadata per template.

Both `fm/presets/templates/` and `fm/presets/template_index.json` are
**committed to git**. Re-run the script only when OrcaSlicer updates its
preset library.

### Data Model

- `Profile.template_override` (`String(255)`, nullable) — the selected
  template name, or `None` for the generic type default.
- Added by `_migrate()` in `fm/db.py` via `ALTER TABLE` on engine init.

### Template Index (`fm/presets/template_index.py`)

Runtime loader for `template_index.json` (cached after first call):

| Function | Description |
|---|---|
| `load_index()` | Load + cache the JSON index. |
| `templates_for_type(type)` | Templates matching a type (Generic first). |
| `templates_for_type_and_vendor(type, vendor)` | Filter by type + vendor (case-insensitive partial). |
| `all_vendors_for_type(type)` | Unique vendors for a type (Generic always first). |
| `preview_for_template(name)` | Flatten the named template, extract 5 preview values (nozzle temp, flow ratio, fan min/max, max volumetric). |

### Templates API

`GET /api/templates?type=&vendor=` (`catalog.py:414`) — lists templates
optionally filtered by type and vendor. Vendors list is Generic-first.
Returns `{"templates": [...], "vendors": [...]}`.

### HTMX Picker Flow

The profile form (`profile_form.html`) has a "Preset source" picker with
three cascading fields, each an HTMX fragment served by
`/profiles/new/template-dropdown` (`ui.py:257`) or
`/profiles/{pid}/template-dropdown` (`ui.py:269`):

1. **Type filter** — selecting a type fires the HTMX request with
   `reason=type-change`, which resets vendor and template to defaults
   and shows a status message: "Template reset to Generic \<type\>
   defaults because material type changed."
2. **Vendor filter** — "All vendors" by default; narrows the template
   list.
3. **Template dropdown** — templates grouped by vendor (`<optgroup>`),
   with a default option "Use Generic \<type\> defaults (recommended)".
4. **Live preview** — when a template is selected, its flattened
   preview values (nozzle temp, flow ratio, fan range, max volumetric)
   are shown inline.

### Builder Resolution

`_preset_template_factory` (`sync_handlers.py:73`) resolves the
template at preset-generation time:

1. If `template_override` is set: look up `"<override> @base"` via
   `find_preset_by_name` and flatten the `@base` chain with
   `flatten_preset`.
2. If the override file is missing: `log.warning` and fall back to the
   generic type default (`find_system_preset(type)`).
3. If no override: use the generic type default.

Brand and template vendor are independent — a house brand may use a
Generic preset.

### Server-side Validation

On profile create/update (`ui.py:283`, `ui.py:335`), if
`template_override` is set, it is checked against `template_index.json`.
A template whose `type` does not match the submitted type is cleared
(set to empty), so a type change never leaves a stale override.

### Orca-fragment Display

The read-only orca-fragment shows `template: <name>` — the
`template_override` value, or `Generic <type> defaults` when no override
is set.

---

## Orca Cloud Integration

`fm/orca_cloud.py` — `OrcaCloudClient`, a direct client for the Orca
Cloud Sync API. Auth is via a Supabase refresh token; the token rotates
on every refresh and the caller must persist the new token via the
`on_token_rotated` callback.

### Authentication

- `login_with_password(email, password)` (classmethod,
  `orca_cloud.py:62`) — calls Supabase's password-grant endpoint,
  returns `(refresh_token, user_id)`. Used by the Settings page
  connect flow (`POST /api/settings/orca-connect`, `ui.py:1125`).
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

`_default_orca_client()` (`sync_handlers.py:94`) builds an
`OrcaCloudClient` from stored credentials, wiring
`on_token_rotated` to `save_orca_token`. Returns `None` when no
credentials are configured (sync endpoints raise in that case).

### Sync API

| Method | Description |
|---|---|
| `sync_push(profile_id, name, content, original_updated_time="")` | Push a preset. `profile_id` is the `filament_id`; the cloud resource UUID is derived via `cloud_id()`. Converts local array format → cloud flat-string format. Returns `{updated_time, created_time}`. Raises `OrcaCloudConflict` on 409. |
| `sync_pull(cursor=0)` | Pull profiles. Returns `{next_cursor, upserts, deletes}`. On 410 Gone, retries without cursor (full resync). |
| `sync_delete(ids)` | Delete profiles by cloud resource UUID. |
| `cloud_id(filament_id)` | Deterministic Orca Cloud resource UUID (UUID v5 of user_id + filament_id) — the equivalent of OrcaSlicer's `setting_id`. |
| `get_user_id()` | Return the authenticated user's UUID (`sub` from JWT). |

### Orca Cloud Settings Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/api/settings/orca-connect` | Email+password login → persist refresh token (`ui.py:1125`) |
| POST | `/api/settings/orca-disconnect` | Remove stored Orca Cloud credentials (`ui.py:1143`) |

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

### Settings Page Flow

- `GET /settings` (`ui.py:1057`): render form pre-filled from current
  config or stored settings, plus Orca Cloud connection status.
- `POST /settings` (`ui.py:1067`): validate required fields, parse ints,
  `save_settings()`, update `app.state.config`, redirect.
- `POST /api/settings/test-connection` (`ui.py:1153`): SSH to printer,
  check DB path exists. Returns `{"ok": bool, "error": str \| None}`.
- `POST /api/settings/orca-connect` (`ui.py:1125`): email+password
  login to Orca Cloud, persist refresh token.
- `POST /api/settings/orca-disconnect` (`ui.py:1143`): remove stored
  Orca Cloud credentials.

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
| GET | `/api/templates?type=&vendor=` | List preset templates (filtered) |

### Sync API (`/api`)

| Method | Path | Description |
|---|---|---|
| POST | `/api/sync/push` | Enqueue push job, drain queue |
| POST | `/api/sync/preset/{profile_id}` | Enqueue generate_preset job (API-only) |
| POST | `/api/sync/tempsync` | Enqueue temp_sync job |
| GET | `/api/status/drift` | Per-profile drift matrix |
| POST | `/api/jobs/{job_id}/resolve` | Resolve awaiting_input job |
| GET | `/api/jobs?since={id}` | List jobs since ID |
| GET | `/api/jobs/{job_id}` | Get single job |

### Orca Cloud Settings API (`/api/settings`)

| Method | Path | Description |
|---|---|---|
| POST | `/api/settings/orca-connect` | Connect to Orca Cloud (email+password) |
| POST | `/api/settings/orca-disconnect` | Disconnect from Orca Cloud |
| POST | `/api/settings/test-connection` | Test SSH connection to printer |

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
| GET | `/profiles/new/template-dropdown` | HTMX: template picker fragment (new) |
| GET | `/profiles/{pid}/template-dropdown` | HTMX: template picker fragment (edit) |
| POST | `/profiles/form` | Create profile (form handler) |
| POST | `/profiles/{pid}/form` | Update profile (form handler) |
| POST | `/profiles/sync-from-orca` | Sync all profiles from Orca Cloud |
| POST | `/profiles/{pid}/sync-from-orca` | Sync single profile from Orca Cloud (HTMX) |
| GET | `/profiles/{pid}/orca-fragment` | HTMX: read-only Orca Cloud preset values |
| GET | `/spools` | Spool inventory with filters |
| POST | `/spools/form` | Create spool |
| POST | `/spools/{sid}/remaining` | HTMX: update remaining_pct |
| POST | `/spools/{sid}/edit` | Edit spool |
| POST | `/spools/{sid}/delete` | Delete spool |
| POST | `/spools/{sid}/duplicate` | Duplicate spool (remaining=100) |
| GET | `/sync` | Sync page (drift table, actions, job progress) |
| GET | `/sync/progress?since={id}` | HTMX: job progress table |
| POST | `/sync/jobs/{job_id}/resolve` | HTMX: resolve awaiting_input job |
| GET | `/migration` | Migration wizard |
| POST | `/migration/import` | Start import scan |
| POST | `/migration/apply` | Apply selected proposals |
| GET | `/import-orca` | Orca Cloud Import page |
| POST | `/import-orca/apply` | Import selected presets |
| GET | `/settings` | Settings page |
| POST | `/settings` | Save settings |

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
   restart the dev server to pick up new endpoints. Templates and CSS
   are picked up without restart.
5. **OrcaSlicer is the editor**: Do not add print-settings fields to
   the Profile model or the profile edit form. Print settings come from
   Orca Cloud via `sync_pull` and are stored in
   `SyncTarget.actual_state`.
6. **No model migrations**: Use existing columns and
   `SyncTarget.actual_state` for new data. Do not add columns to
   Profile or Spool models. (Schema additions for new features go
   through `_migrate()` in `fm/db.py`.)

### Key Files

| File | Purpose |
|---|---|
| `fm/api/ui.py` | UI routes (pages, forms, HTMX fragments). Restart dev server after adding routes. |
| `fm/api/templates/` | Jinja2 templates. Changes picked up without restart. |
| `fm/api/static/app.css` | Stylesheet. Changes picked up without restart. |
| `fm/orca_cloud.py` | Orca Cloud Sync API client (push/pull/delete, Supabase auth, token rotation). |
| `fm/service/sync_handlers.py` | Sync logic, `refresh_preset_drift`, printer DB operations, Orca Cloud push/pull. |
| `fm/presets/matching.py` | OrcaSlicer preset matching, `find_orca_user_dir`, `load_presets`. |
| `fm/presets/builder.py` | Preset builder, `_KV_MAP` (catalog → Orca key mapping). |
| `fm/presets/template_index.py` | Runtime loader for `template_index.json`. |
| `fm/settings_store.py` | settings.json read/write (printer config, Orca Cloud token storage). |

---

## Testing

```bash
scripts/test.sh        # full pytest suite (356 tests)
```

### Test Files

| File | Coverage |
|---|---|
| `tests/test_ui.py` | UI routes: profile forms, spool forms, sync page, migration, settings |
| `tests/test_ui_sync.py` | Sync page: drift table, busy dialog, job progress, resolve |
| `tests/test_ui_template_picker.py` | Template picker HTMX flow: type/vendor filter, preview, type-change reset |
| `tests/test_sync_handlers.py` | Sync handlers: push, verify, generate_preset, temp_sync, import_scan, drift |
| `tests/test_pipeline.py` | Push pipeline: MIN_DB check, busy check, backup, version bump, reboot |
| `tests/test_transport.py` | SSH transport: run, scp_push/pull, ws_request, busy check, reboot wait |
| `tests/test_matching.py` | Preset matching, temp_sync_targets, find_orca_user_dir |
| `tests/test_builder.py` | Preset builder: generate_filament_id, build_standalone_preset, _KV_MAP |
| `tests/test_dbcodec.py` | Printer DB codec: load/save/insert/remove/validate |
| `tests/test_jobs.py` | Job queue: enqueue, claim_next, run_pending, complete, fail |
| `tests/test_api_catalog.py` | Catalog API: profiles/spools CRUD, validation |
| `tests/test_api_sync.py` | Sync API: push, preset, tempsync, drift, jobs |
| `tests/test_api_templates.py` | Templates API: list/filter by type and vendor |
| `tests/test_api_settings.py` | Settings API: save, load, test-connection, orca-connect/disconnect |
| `tests/test_settings_store.py` | settings.json: load, save, build_config, load_merged_config, orca token |
| `tests/test_orca_cloud.py` | OrcaCloudClient: auth, sync_push/pull/delete, cloud_id, conflict handling |
| `tests/test_template_index.py` | Template index: load, filter by type/vendor, preview |
| `tests/test_config.py` | Config dataclass, load_config, required env vars |
| `tests/test_models.py` | Model instantiation and defaults |

### Test Conventions

- Tests use `pytest` with `conftest.py` providing in-memory SQLite
  fixtures and config injection.
- Transport is faked via monkeypatch of `_transport_factory`.
- Orca Cloud client is faked via monkeypatch of `_orca_client_factory`.
- No real SSH/SCP/WebSocket or Orca Cloud calls in tests.
- `conftest.py` sets up isolated database, test config, and app
  fixture.
