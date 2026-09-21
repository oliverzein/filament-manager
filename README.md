# Filament Manager

A hosted web app to manage 3D-printing filament spool inventory and keep a
Creality K2 printer database in sync with Orca Cloud filament presets.
Orca Cloud is the sole source of filament presets — presets are created and
tuned in OrcaSlicer Desktop, synced to Orca Cloud, and the Manager caches
them read-only for spool tracking and printer DB push.

The full design lives in
[`docs/superpowers/specs/2026-09-04-filament-manager-rework.md`](docs/superpowers/specs/2026-09-04-filament-manager-rework.md)
(original design spec archived at
[`docs/archive/superpowers/specs/2026-08-26-filament-manager-design.md`](docs/archive/superpowers/specs/2026-08-26-filament-manager-design.md)).
Working instructions for agents and humans are in [`AGENTS.md`](AGENTS.md).

## Purpose

1. **Spool inventory** — which spools are in stock, their color (dropdown of
   common colors + custom picker), remaining amount, location. Spools are
   linked to cached Orca Cloud presets via `cloud_resource_id`. Spools can
   be edited, duplicated (remaining reset to 100%), and deleted inline.
   Spools whose preset was deleted from Orca Cloud show an **Orphan** badge.
2. **Sync** — push cached Orca Cloud presets to the Creality K2 printer DB.
   The Sync Hub shows a Preset & Status table with per-preset printer DB
   sync state, push-to-printer actions, and orphan printer DB entry removal.
3. **Orca Cloud cache** — the Manager pulls filament presets from Orca Cloud
   into a read-only `profile_cache` table. Presets are created and edited in
   OrcaSlicer Desktop; the Manager never writes presets to Orca Cloud.

Non-goals: local profile/catalog editing, preset generation, NFC tag
programming (stays manual; the app shows checklists), multi-printer support,
multi-user/auth.

## Architecture

Orca Cloud is the sole source of filament presets. The Manager caches them
read-only in a `profile_cache` table. Printer DB entries are derived from
cached cloud presets and pushed to the printer. Spools reference cached
presets via `cloud_resource_id`. Presets deleted from Orca Cloud are
soft-deleted in the cache (tombstoned) and surface as orphan badges on
linked spools and in the Sync Hub.

```
┌─────────────────────────── Unraid container ───────────────────────────┐
│  Filament Manager (FastAPI + SQLite + Jinja2/HTMX)                     │
│  • Spool inventory (linked to cached presets via cloud_resource_id)    │
│  • Orca Cloud cache: read-only profile_cache from sync_pull            │
│  • Sync Hub: Preset & Status table, push to printer, orphan removal    │
│  • Printer sync: SSH/SCP push + version bump + reboot + verify         │
│    (logic lifted from cfs.py, iron rules enforced server-side)         │
│  • GitHub backup: commit all Orca Cloud presets as JSON to a repo      │
│  • Settings page: printer connection + Orca Cloud connection           │
└──────────────┬──────────────┬──────────────────────────────────────────┘
               │ SSH/SCP to printer (LAN-only)
               │ HTTPS to Orca Cloud API
┌──────────────▼───────────── Printer ───────────────────────────────────┐
│  Creality K2 (material_database.json on UDISK)                        │
└────────────────────────────────────────────────────────────────────────┘
┌──────────────▼───────────── Orca Cloud ────────────────────────────────┐
│  OrcaSlicer Cloud Sync API (api.orcaslicer.com)                        │
│  • Presets created/tuned in OrcaSlicer Desktop                        │
│  • Manager pulls via GET /api/v1/sync/pull (read-only cache)           │
│  • OrcaSlicer syncs via File > Sync Presets (desktop app)              │
└────────────────────────────────────────────────────────────────────────┘
```

| Component | Runs on | Tech |
|---|---|---|
| Web app | Unraid Docker | Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX |
| Orca Cloud cache | in app image | Direct Orca Cloud Sync API client (`fm/orca_cloud.py`), stdlib + requests |
| Shared core | in app image | Modules lifted from the skill: DB patching, version bump, preset flattening |

The existing skill stays untouched and remains available for agent-driven flows.

## Setup

### Local development

```bash
scripts/dev-start.sh   # uvicorn on :8404, isolated SQLite in .local/
# ... iterate ...
scripts/dev-stop.sh    # deterministic stop via .local/server.pid
scripts/test.sh        # full pytest suite (322 tests)
```

`dev-start.sh` exports an isolated
`FM_DATABASE_URL=sqlite:///.local/filaments.db`; override the printer env vars
inline if you need to point at a different host.

### Container (Unraid)

The image can be built locally or pushed to Docker Hub (`w00z72/filament-manager`). From a clean
tree:

```bash
scripts/release.sh 2.1.1                  # tag vX.Y.Z, build image, save dist/*.tar.gz
scripts/release.sh --push 2.1.1           # tag, build, save tarball, and push to Docker Hub
scripts/deploy-unraid.sh 2.1.1            # load image + XML template on Unraid, restart
```

`deploy-unraid.sh` expects a deploy env file at `deploy/filament-manager.env`
(override with `FM_ENV_FILE`) holding the `FM_*` variables, and SSH access to
`root@unraid` (key/config alias, or `SSHPASS` set for sshpass). Never commit
real credentials. The Unraid host IP defaults to `192.168.0.31` (override with
`UNRAID_IP`).

The container exposes port 8000, mapped to host port 8404. The app data volume
is bind-mounted at `/data` (host `/mnt/user/appdata/filament-manager`) and
holds the SQLite database and logs. The app reports its version in the UI
footer and at `GET /api/version`.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `FM_DATABASE_URL` | `sqlite:////data/filaments.db` (container) / `sqlite:///./filaments.db` (local) | SQLAlchemy database URL. Container default points at the bind-mounted `/data` volume. |
| `FM_PRINTER_IP` | `192.168.0.101` | Creality K2 printer IP for sync. |
| `FM_SSH_USER` | `root` | SSH user for the printer. |
| `FM_SSH_PASSWORD` | *(secret)* | SSH password for the printer. |
| `FM_DB_REMOTE_PATH` | `/mnt/UDISK/creality/userdata/box/material_database.json` | Remote printer DB path. |
| `FM_WS_PORT` | `9999` | Printer websocket port (busy-check). |
| `FM_VERSION_OVERRIDE` | `9876543210` | Printer DB version override (must be `>= 9876543210`). |
| `FM_ID_RANGE_START` | `99001` | Start of the custom printer ID range (99xxx). |
| `FM_SETTINGS_PATH` | `.local/settings.json` (dev) / `/data/settings.json` (container) | Path to the settings.json file (printer connection config, overrides env vars). |

`FM_PRINTER_IP`, `FM_SSH_USER`, `FM_SSH_PASSWORD`, and `FM_DB_REMOTE_PATH`
are required; the rest fall back to the defaults above. If the required printer
env vars are missing, sync endpoints return 503. Settings page values (stored
in `settings.json`) take precedence over environment variables; the Orca Cloud
refresh token is configured on the Settings page (email/password connect) and
stored in `settings.json` only — there is no env var for it. The
authoritative `FM_*` list lives in `fm/config.py`.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/dev-start.sh` | Start the app locally (uvicorn, dev config, isolated SQLite in `.local/`); waits for readiness. |
| `scripts/dev-stop.sh` | Stop the local dev app deterministically via `.local/server.pid`. |
| `scripts/test.sh` | Run the full pytest suite (322 tests). |
| `scripts/release.sh <version>` | Semver tag, build the Docker image, save a gzipped tarball to `dist/`; refuses a dirty tree. |
| `scripts/deploy-unraid.sh <version>` | Load the released image + XML template onto Unraid, restart the container, wait for readiness. |

Per `AGENTS.md`, any action likely to repeat goes into a script under `scripts/`
— agents call scripts, never ad-hoc command sequences.

## UI pages

| Page | Route | Description |
|---|---|---|
| Dashboard | `/` | Spool count by profile, drift matrix, recent activity. |
| Spools | `/spools` | Spool inventory linked to cached Orca Cloud presets via `cloud_resource_id`. Inline edit, duplicate (remaining=100%), delete, and color dropdown (30 common colors + custom picker). Spools whose preset was deleted from Orca Cloud show an **Orphan** badge. |
| Sync | `/sync` | Sync Hub: Preset & Status table from `profile_cache`, push-to-printer actions, orphan printer DB entry removal (double-confirm typed token), job progress, busy-printer resolution. |
| Settings | `/settings` | Printer connection config + Orca Cloud connection (IP, SSH credentials, DB path, test connection, Orca Cloud auth). |

## Orca Cloud cache

The Manager pulls filament presets from Orca Cloud via the Sync API
(`GET /api/v1/sync/pull`) and stores them in a read-only `profile_cache`
table. Each cache entry records the cloud resource ID, name, vendor, type,
filament ID, inheritance flag, and soft-delete tombstone. Only filament
presets are cached — Orca Cloud process and printer presets are filtered
out, and any stale cache entries from a prior type are purged.

**Presets are created and tuned in OrcaSlicer Desktop.** The user opens
OrcaSlicer, creates or edits a filament preset, and syncs it to Orca Cloud
via File > Sync Presets. The Manager then pulls the updated presets into
the cache on the next sync. The Manager never writes presets to Orca Cloud.

**Re-upload reconciliation**: when a preset is re-uploaded to Orca Cloud
(e.g. deleted and recreated), it gets a new cloud resource UUID. The cache
sync automatically matches the new UUID to existing `SyncTarget` rows and
spools by name, re-linking them so push state and spool associations survive
the re-upload.

**Inherited presets** (those with an `inherits` field) are resolved at cache
sync time — the parent system preset's identity (filament_id,
filament_vendor) and type are merged into the child, with child overrides
winning.

**Soft deletes**: presets deleted from Orca Cloud are tombstoned in the
cache (`deleted_from_cloud=True`, `deleted_at` timestamp). Spools linked to
tombstoned presets show an **Orphan** badge. Tombstoned presets appear in
the Sync Hub with an **orphan** status and a **Remove** button for cleaning
up orphaned printer DB entries.

## Spool inventory

Spools are physical filament rolls. Each spool references a cached Orca Cloud
preset via `cloud_resource_id` (a foreign key to `profile_cache`). The spool
form's profile dropdown lists non-deleted cached presets. Spools linked to
deleted presets retain their `cloud_resource_id` and show an **Orphan** badge
in the inventory table.

Spool operations: add (select a cached preset, pick a color, set location and
remaining %), inline edit, duplicate (remaining reset to 100%), delete, and
remaining-percentage slider (auto-saves via HTMX).

## Sync Hub

The Sync Hub (`/sync`) is the central sync management page. It shows:

- **Preset & Status table** — one row per cached Orca Cloud preset, showing
  vendor + name (vendor prefix deduplicated: if the preset name already
  starts with the vendor, only the name is shown), type, printer DB ID (if
  pushed), and sync status (`in_sync`, `pending`, or `orphan`).
  Each row has a **Push to Printer** button (for non-orphan presets)
  or a **Remove** button (for orphan presets with a printer DB ID).
- **Printer DB target card** — summary of in-sync vs. not-pushed counts,
  with a bulk **Push to Printer** button.
- **Cloud presets target card** — summary of cached presets and orphan count.
  Orca Cloud is the sole source of filament presets; new presets are created
  in OrcaSlicer and sync via Orca Cloud.
- **GitHub backup target card** — commit all Orca Cloud presets as JSON files
  to a configured repository.
- **Busy dialog** — when the printer is busy, awaiting-input jobs show
  resolution options (force reboot, wait, no reboot).
- **Job progress** — live polling of job status (push, verify, github_backup).

### Push to printer

Pushes cached Orca Cloud presets to the Creality K2 printer's filament
database. The push handler pulls live presets from Orca Cloud, filters out
stock Creality presets (evaluated on the flattened preset so inherited
variants like Hyper Stardust and EN-PLA+ are recognized as Creality),
flattens inherited presets, and builds printer DB entries with 99xxx IDs.
The iron-rule pipeline enforces: busy-check before upload, backup with
rotation (keep 5), version bump (≥ 9876543210), SCP upload, reboot, and
automatic verify.

### Orphan printer DB entry removal

Orphan printer DB entries (99xxx IDs whose cloud preset was deleted) persist
on the printer until explicitly removed. The Sync Hub's **Remove** button
(on orphan rows with a printer DB ID) triggers a double-confirm flow: the
typed token must match the entry ID. This removes the entry from the local
printer DB cache and deletes the associated `SyncTarget`.

## Orca Cloud integration

The Manager integrates directly with the Orca Cloud Sync API. OrcaSlicer
syncs presets to/from Cloud via its built-in File > Sync Presets feature.

**Authentication**: Orca Cloud uses Supabase auth. The refresh token is
configured on the Settings page (Orca Cloud email/password connect,
`POST /api/settings/orca-connect`). The refresh token rotates on every use;
the server persists the new token automatically after each API call
(`fm/settings_store.py`).

**Pull** (read-only cache): the Manager pulls presets from Orca Cloud via
`GET /api/v1/sync/pull` and upserts them into the `profile_cache` table.
Soft deletes are processed from the tombstone feed. This is a read-only
operation — the Manager never pushes presets to Orca Cloud.

**Conflict handling**: if the server's `updated_time` is newer than the
client's (409 conflict), the server returns the current version and the
client can re-pull to get the latest.

## Sync jobs

Sync work runs as jobs drained synchronously inside the request that triggers
them — there is no scheduler, no background worker, and no external process
monitoring the queue.
`jobs.run_pending` dispatches each queued job to a registered handler
(`fm/service/sync_handlers.py`); each handler runs inside a DB savepoint so a
failure discards its partial writes before the error is recorded.

Job kinds:

| Kind | Trigger | Action |
|---|---|---|
| `push` | `POST /api/sync/push`, Sync Hub push button | Rebuild printer DB from cloud presets (or catalog fallback), push via SSH/SCP, bump version + reboot, enqueue `verify`. |
| `verify` | (enqueued by `push`) | Pull fresh DB, floor-check + presence + hash compare, update `SyncTarget.status`. |
| `github_backup` | `POST /api/sync/backup-github`, Sync Hub backup button | Pull all Orca Cloud presets, write each as JSON + index.json, commit to GitHub. |

A `push` that finds the printer busy parks the job as `awaiting_input`; the
user resolves it from the Sync Hub (or `POST /api/jobs/{id}/resolve`) by
choosing `force_reboot`, `wait`, or `no_reboot`, which re-queues and drains the
job.

## Iron rules

Non-negotiable, lifted from the skill and restated in `AGENTS.md` so every
session sees them:

1. Version `>= 9876543210` + reboot mandatory after every printer-DB write;
   the busy-check runs **before** any upload.
2. Custom printer IDs only in the 99xxx range; stock IDs protected.
3. Backup on the printer before every write; rotate, keep 5.
4. Double-confirm on delete (typed token must match).
5. Refuse push when DB `< 30` entries (`MIN_DB_ENTRIES`) unless explicitly
   forced.

See the [rework spec](docs/superpowers/specs/2026-09-04-filament-manager-rework.md)
for the rationale and the full sync-engine, drift-detection, and error-handling
details.
