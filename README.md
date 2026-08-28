# Filament Manager

A hosted web app to manage 3D-printing filaments: a profile catalog, a spool
inventory, and a sync engine that keeps a Creality K2 printer DB and OrcaSlicer
presets derived from the catalog. It replaces the sync portions of the
`creality-custom-filament` Devin skill with a UI-driven workflow.

The full design lives in
[`docs/superpowers/specs/2026-08-26-filament-manager-design.md`](docs/superpowers/specs/2026-08-26-filament-manager-design.md).
Working instructions for agents and humans are in [`AGENTS.md`](AGENTS.md).

## Purpose

1. **Profile catalog** — brands, types, colors, print parameters (temps, flow
   ratio, retraction, PA, density, drying). OrcaSlicer is the sole editor of
   print settings; the catalog displays them read-only after sync.
2. **Spool inventory** — which spools are in stock, their color (dropdown of
   common colors + custom picker), remaining amount, location. Spools can be
   edited, duplicated (remaining reset to 100%), and deleted inline.
3. **Sync** — keep the Creality K2 printer DB and OrcaSlicer presets derived
   from the catalog. Drift is detected by comparing expected vs. actual state.
4. **Import from OrcaSlicer** — bring standard filament presets (including
   inherited presets with resolved identity) into the catalog as profiles for
   spool tracking and print-settings display.

Non-goals: NFC tag programming (stays manual; the app shows checklists),
multi-printer support, multi-user/auth.

## Architecture

Catalog is the single source of truth. Printer DB entries and OrcaSlicer
presets are derived views; drift is detected by comparing expected vs. actual
state. OrcaSlicer is the sole editor of print settings — the Manager generates
a starter preset, the user tunes in OrcaSlicer, and the Manager displays the
tuned values read-only after sync.

```
┌─────────────────────────── Unraid container ───────────────────────────┐
│  Filament Manager (FastAPI + SQLite + Jinja2/HTMX)                     │
│  • Profiles + Spool inventory                                          │
│  • Sync engine: derives printer DB entries + preset payloads           │
│  • Printer sync: SSH/SCP push + version bump + reboot + verify         │
│    (logic lifted from cfs.py, iron rules enforced server-side)         │
│  • Orca Cloud sync: push starter presets + pull tuned settings         │
│    (direct API integration, no desktop agent needed)                   │
│  • Import from OrcaSlicer: standard presets → catalog profiles         │
│  • Settings page: printer connection + Orca Cloud connection           │
└──────────────┬─────────────────────────────────────────────────────────┘
               │ SSH/SCP to printer (LAN-only)
               │ HTTPS to Orca Cloud API
┌──────────────▼───────────── Printer ───────────────────────────────────┐
│  Creality K2 (material_database.json on UDISK)                        │
└────────────────────────────────────────────────────────────────────────┘
┌──────────────▼───────────── Orca Cloud ────────────────────────────────┐
│  OrcaSlicer Cloud Sync API (api.orcaslicer.com)                        │
│  • Starter presets pushed via POST /api/v1/sync/push                   │
│  • Tuned settings pulled via GET /api/v1/sync/pull                     │
│  • OrcaSlicer syncs via File > Sync Presets (desktop app)              │
└────────────────────────────────────────────────────────────────────────┘
```

| Component | Runs on | Tech |
|---|---|---|
| Web app | Unraid Docker | Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX |
| Orca Cloud sync | in app image | Direct Orca Cloud Sync API client (`fm/orca_cloud.py`), stdlib + requests |
| Shared core | in app image | Modules lifted from the skill: DB patching, version bump, preset matching, flatten, preset builder |

The existing skill stays untouched and remains available for agent-driven flows.

## Setup

### Local development

```bash
scripts/dev-start.sh   # uvicorn on :8404, isolated SQLite in .local/
# ... iterate ...
scripts/dev-stop.sh    # deterministic stop via .local/server.pid
scripts/test.sh        # full pytest suite
```

`dev-start.sh` exports an isolated
`FM_DATABASE_URL=sqlite:///.local/filaments.db`; override the printer env vars
inline if you need to point at a different host.

### Container (Unraid)

The image is built and loaded locally — there is no registry. From a clean
tree:

```bash
scripts/release.sh 0.1.0          # tag vX.Y.Z, build image, save dist/*.tar.gz
scripts/deploy-unraid.sh 0.1.0    # load image + XML template on Unraid, restart
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
| `FM_ORCA_REFRESH_TOKEN` | *(optional)* | Orca Cloud refresh token for Sync API authentication. Can be set via Settings page instead of env var. |
| `FM_SETTINGS_PATH` | `.local/settings.json` (dev) / `/data/settings.json` (container) | Path to the settings.json file (printer connection config, overrides env vars). |

`FM_PRINTER_IP`, `FM_SSH_USER`, `FM_SSH_PASSWORD`, and `FM_DB_REMOTE_PATH`
are required; the rest fall back to the defaults above. If the required printer
env vars are missing, sync endpoints return 503. Settings page values (stored
in `settings.json`) take precedence over environment variables.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/dev-start.sh` | Start the app locally (uvicorn, dev config, isolated SQLite in `.local/`); waits for readiness. |
| `scripts/dev-stop.sh` | Stop the local dev app deterministically via `.local/server.pid`. |
| `scripts/test.sh` | Run the full pytest suite. |
| `scripts/release.sh <version>` | Semver tag, build the Docker image, save a gzipped tarball to `dist/`; refuses a dirty tree. |
| `scripts/deploy-unraid.sh <version>` | Load the released image + XML template onto Unraid, restart the container, poll for readiness. |

Per `AGENTS.md`, any action likely to repeat goes into a script under `scripts/`
— agents call scripts, never ad-hoc command sequences.

## UI pages

| Page | Route | Description |
|---|---|---|
| Dashboard | `/` | Spool count by profile, drift matrix, recent activity. |
| Profiles | `/profiles` | Catalog of filament profiles with filter/search. |
| Profile edit | `/profiles/{id}/edit` | Edit identity (brand, name, type, colors, density, drying, notes) + read-only OrcaSlicer print settings (synced from Orca Cloud). |
| Spools | `/spools` | Spool inventory with inline edit, duplicate (remaining=100%), delete, and color dropdown (30 common colors + custom picker). |
| Sync | `/sync` | Drift table, push/verify/preset actions, job progress. |
| Import from OrcaSlicer | `/import-orca` | Import standard OrcaSlicer presets (including resolved inherited presets) as catalog profiles. |
| Migration | `/migration` | One-time import of existing printer DB entries. |
| Settings | `/settings` | Printer connection config + Orca Cloud connection (IP, SSH credentials, DB path, test connection, Orca Cloud auth). |

## Migration (one-time)

The Migration page (`/migration`) imports existing printer-DB custom entries
into the catalog on first deploy.

1. Open **Migration** in the web UI.
2. **Step 1 — Import scan**: click *Start import scan*. The app pulls the
   printer DB, extracts all 99xxx entries as proposals.
3. **Step 2 — Review proposals**: a table lists each proposed profile
   (printer DB ID, brand, name, type, temps, density) with an import checkbox.
   Entries already in the catalog are marked *exists* and pre-checked.
4. Select the entries to import and click **Import selected**. The app creates
   / updates Profiles and SyncTargets, **preserving existing `printer_db_id`
   and `filament_id`** to avoid OrcaSlicer Cloud collisions.

Expected first-run entries: 99001 Sunlu PLA+, 99002 eSUN PETG Basic, 99003
CAILAB PLA Silk, 99004 Cailab PLA+ Bio.

## Import from OrcaSlicer

The Import page (`/import-orca`) shows OrcaSlicer presets not yet linked to a
catalog profile. Presets are pulled directly from Orca Cloud via the Sync API.
Inherited presets (those with an `inherits` field) are resolved at import time
— the parent system preset's identity (filament_id, filament_vendor) and
settings are merged into the child, with child overrides winning.
`filament_type` is derived from the parent preset name when the system preset
omits it.

Imported profiles get `printer_db_id=None` (no push to printer — standard
filaments are already on the printer). After import, click "Sync from Orca" on
the profile page to populate the read-only print settings.

## Orca Cloud integration

The Manager integrates directly with the Orca Cloud Sync API — no desktop
agent is needed. OrcaSlicer syncs presets to/from Cloud via its built-in
File > Sync Presets feature.

**Authentication**: Orca Cloud uses Supabase auth. The refresh token is
configured on the Settings page (or via the `FM_ORCA_REFRESH_TOKEN` env var).
The refresh token rotates on every use; the server persists the new token
automatically after each API call.

**Push** (starter presets): when a profile is generated, the Manager pushes
the starter preset directly to Orca Cloud via `POST /api/v1/sync/push`. The
user then opens OrcaSlicer, runs File > Sync Presets to pull the starter,
tunes the print settings, and syncs again to push the tuned values back to
Cloud.

**Pull** (tuned settings): the Manager pulls tuned presets from Orca Cloud
via `GET /api/v1/sync/pull` and stores them in `SyncTarget.actual_state` for
read-only display on the profile page. Incremental pulls use a cursor stored
in settings; a 410 Gone response triggers a full resync (cursor reset to 0).

**Conflict handling**: if the server's `updated_time` is newer than the
client's (409 conflict), the server returns the current version and the
client can re-pull to get the latest.

**Profile IDs**: Orca Cloud profile IDs are deterministic UUIDs derived from
the profile name + user ID (matching OrcaSlicer's `generate_uuid_for_setting_id`).
This ensures the same profile always maps to the same Cloud ID across pushes.

**Payload limit**: the Sync API accepts payloads up to 1 MB per profile.

## Iron rules

Non-negotiable, lifted from the skill and restated in `AGENTS.md` so every
session sees them:

1. Version `>= 9876543210` + reboot mandatory after every printer-DB write;
   the busy-check runs **before** any upload.
2. Profile name must contain the vendor ("Sunlu PLA+", not "PLA+").
3. Custom printer IDs only in the 99xxx range; stock IDs protected.
4. Backup on the printer before every write; rotate, keep 5.
5. Double-confirm on delete (typed token must match).
6. Refuse push when DB `< 30` entries (`MIN_DB_ENTRIES`) unless explicitly
   forced.

See the [design spec](docs/superpowers/specs/2026-08-26-filament-manager-design.md)
for the rationale and the full sync-engine, drift-detection, and error-handling
details.
