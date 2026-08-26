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
   ratio, retraction, PA, density, drying).
2. **Spool inventory** — which spools are in stock, their color, remaining
   amount, location.
3. **Sync** — keep the Creality K2 printer DB and OrcaSlicer presets derived
   from the catalog. Drift is detected by comparing expected vs. actual state.

Non-goals: NFC tag programming (stays manual; the app shows checklists),
multi-printer support, multi-user/auth beyond a single agent token.

## Architecture

Catalog is the single source of truth. Printer DB entries and OrcaSlicer
presets are derived views; drift is detected by comparing expected vs. actual
state.

```
┌─────────────────────────── Unraid container ───────────────────────────┐
│  Filament Manager (FastAPI + SQLite + Jinja2/HTMX)                     │
│  • Profiles + Spool inventory                                          │
│  • Sync engine: derives printer DB entries + preset payloads           │
│  • Printer sync: SSH/SCP push + version bump + reboot + verify         │
│    (logic lifted from cfs.py, iron rules enforced server-side)         │
└──────────────┬─────────────────────────────────────────────────────────┘
               │ REST API (token auth, LAN-only)
┌──────────────▼───────────── Desktop ───────────────────────────────────┐
│  Companion agent (`fm-agent`, thin Python CLI)                         │
│  • Applies generated presets to ~/.config/OrcaSlicer                   │
│    (refuses while OrcaSlicer runs; .info with empty sync_info)         │
│  • Reports preset nozzle_temperature back for DB temp sync             │
└────────────────────────────────────────────────────────────────────────┘
```

| Component | Runs on | Tech |
|---|---|---|
| Web app | Unraid Docker | Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX |
| Companion agent | Desktop PC | Python CLI (`fm-agent`), stdlib + requests |
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

`dev-start.sh` exports a dev `FM_AGENT_TOKEN` and an isolated
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
| `FM_AGENT_TOKEN` | *(secret)* | Shared token authenticating the `fm-agent` CLI. |
| `FM_ORCASLICER_CONFIG_DIR` | `~/.config/OrcaSlicer` | Agent-side override for OrcaSlicer config-dir discovery. |

`FM_PRINTER_IP`, `FM_SSH_USER`, `FM_SSH_PASSWORD`, `FM_DB_REMOTE_PATH`, and
`FM_AGENT_TOKEN` are required; the rest fall back to the defaults above. If the
required printer env vars are missing, sync endpoints return 503.

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

## Companion agent install

The desktop companion (`fm-agent`) is a thin Python CLI that polls the app's
agent job queue, applies generated presets to the local OrcaSlicer user tree,
and reports preset temperatures back for DB temp sync.

Install from a checkout of this repo with pipx:

```bash
pipx install .
```

This provides the `fm-agent` console script. Configure credentials once:

```bash
mkdir -p ~/.config/filament-manager
cat > ~/.config/filament-manager/agent.json <<'JSON'
{"base_url": "http://192.168.0.31:8404", "token": "<FM_AGENT_TOKEN>"}
JSON
```

`--base-url` / `--token` override the file per invocation. Set
`FM_ORCASLICER_CONFIG_DIR` to override OrcaSlicer config-dir discovery
(otherwise it defaults to `~/.config/OrcaSlicer`).

Usage:

```bash
fm-agent poll        # one shot: claim and process at most one queued job
fm-agent watch       # poll loop every 60 s (v1 simple loop)
```

Job types:

- `apply_preset` — write `<name>.json` + `<name>.info` into the first
  `~/.config/OrcaSlicer/user/<UUID>/filament/` dir; **refuses** (posts an error
  result) while OrcaSlicer is running.
- `report_presets` — scan the first user filament dir only and report
  `nozzle_temperature` (plus the match keys) back for temp sync.

Pull-based by design: Unraid cannot reach into the desktop, so the agent polls.
Physical steps stay human (NFC tag writing, spool insertion, starting
OrcaSlicer, Cloud-preset deletion) and are shown as UI checklists.

## Migration (one-time)

The Migration page (`/migration`) imports existing printer-DB custom entries
into the catalog on first deploy.

1. Open **Migration** in the web UI.
2. **Step 1 — Import scan**: click *Start import scan*. The app pulls the
   printer DB, extracts all 99xxx entries as proposals, and enqueues a
   `report_presets` job for the agent (run `fm-agent poll` on the desktop to
   populate preset data).
3. **Step 2 — Review proposals**: a table lists each proposed profile
   (printer DB ID, brand, name, type, temps, density) with an import checkbox.
   Entries already in the catalog are marked *exists* and pre-checked.
4. Select the entries to import and click **Import selected**. The app creates
   / updates Profiles and SyncTargets, **preserving existing `printer_db_id`
   and `filament_id`** to avoid OrcaSlicer Cloud collisions.

Expected first-run entries: 99001 Sunlu PLA+, 99002 eSUN PETG Basic, 99003
CAILAB PLA Silk, 99004 Cailab PLA+ Bio.

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
7. Never write OrcaSlicer presets while OrcaSlicer is running; `.info` gets
   empty `sync_info` / `setting_id`.

See the [design spec](docs/superpowers/specs/2026-08-26-filament-manager-design.md)
for the rationale and the full sync-engine, drift-detection, and error-handling
details.
