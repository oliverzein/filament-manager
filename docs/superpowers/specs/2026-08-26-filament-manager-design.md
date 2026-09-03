# Filament Manager — Design Spec

**Date:** 2026-08-26
**Status:** Approved (design phase)
**Repo:** `/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager`
**Predecessor knowledge:** skill repo `Creality-custom-filament` (cfs.py/orca.py logic), BrainVault `projects/homeassistant/k2-rfid-custom-filament.md`, `k2-orca-preset-implementation.md`
> **Superseded sections (2026-08):** §2 Companion agent, §4 Temp sync und §5 Companion Agent Contract wurden durch den Orca Cloud Sync ersetzt und aus dem Code entfernt — Preset-Auslieferung und Temp-Write-back laufen heute über die Cloud-Transportstrecke (`generate_preset` → Cloud → OrcaSlicer-eigener Sync; `refresh_preset_drift(adopt=True)` mit exaktem `cloud_id`-Matching). Siehe docs/sync-workflow-analysis.md. Die nachfolgenden Abschnitte beschreiben den ursprünglichen Design-Entwurf und werden als historisches Dokument unverändert belassen.


## 1. Purpose

A hosted web app to manage 3D-printing filaments:

1. **Profile catalog** — brands, types, colors, print parameters (temps, flow ratio, retraction, PA, density, drying).
2. **Spool inventory** — which spools are in stock, their color, remaining amount, location.
3. **Sync** — keep the Creality K2 printer DB and OrcaSlicer presets derived from the catalog. Replaces the sync portions of the Devin skill `creality-custom-filament` with a UI-driven workflow.

Non-goals: NFC tag programming (stays manual, app shows checklists), multi-printer support, multi-user/auth beyond a single agent token.

## 2. Architecture

Catalog is the single source of truth. Printer DB entries and OrcaSlicer presets are derived views; drift is detected by comparing expected vs. actual state.

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

### Components

| Component | Runs on | Tech |
|---|---|---|
| Web app | Unraid Docker | Python 3.12, FastAPI, SQLAlchemy + SQLite, Jinja2 + HTMX |
| Companion agent | Desktop PC | Python CLI (`fm-agent`), stdlib + requests |
| Shared core | in app image | Modules lifted from skill: DB patching, version bump, preset matching, flatten, preset builder |

The existing skill stays untouched and remains available for agent-driven flows.

## 3. Data Model (SQLite)

```
profiles                      spools
├─ id                         ├─ id
├─ brand                      ├─ profile_id FK
├─ name (must contain brand)  ├─ color_override (spool color may differ
├─ type (PLA/PETG/…)          │   from profile default)
├─ min_temp / max_temp        ├─ remaining_pct
├─ density                    ├─ location
├─ drying_temp / drying_time  ├─ rfid_tag (optional)
├─ pa                         └─ notes
├─ flow_ratio
├─ max_volumetric             activity_log
├─ retraction_length          ├─ ts
├─ retraction_speed           ├─ action (push/verify/preset_apply/temp_sync/import)
├─ nozzle_temperature         ├─ detail (JSON)
├─ printer_db_id (99xxx)      └─ outcome (ok/warn/error)
└─ notes

sync_targets
├─ id
├─ profile_id FK
├─ kind: printer_db | orca_preset
├─ expected_hash   — hash of last successfully pushed state
├─ actual_state    — last reported/verified state
├─ status: in_sync | drift | pending | error
└─ last_synced_at
```

Notes:

- Retraction/PA/flow/max-volumetric live in the catalog even though the K2 firmware ignores most of them — they feed preset generation and serve as reference.
- Validation ports the skill's rules: temp 100–400 °C, min < max, density 0.9–1.6, dryingTemp 0–100, dryingTime 0–24 h, **name must contain vendor** (Rule 2).
- Custom printer IDs auto-increment from 99001; stock IDs protected (Rule 3).

## 4. Sync Engine

All mutating operations run as queued jobs executed server-side; the UI polls job/activity state. Exit-code semantics of the skill tools are preserved internally and rendered as actionable UI states.

### Push to printer (catalog → K2)

Lifted `cfs.py` path, **order matters**: MIN_DB_ENTRIES check → WebSocket busy-check → backup on printer → version auto-increment (≥ 9876543210; constant version breaks CFS RFID app update detection) → SCP upload → sleep 3 (flash flush) → `sync; sync; reboot` → wait for SSH online → verify pull. Busy-check runs BEFORE any upload — uploading without the ability to reboot exposes the DB to cloud-sync overwrite (~12 min window).

Guards kept verbatim:
- Refuse push if DB < 30 entries (`MIN_DB_ENTRIES`) — corruption guard from 2026-07-08 incident.
- Printer busy → job pauses with user options: wait / force-reboot (kills print) / no-reboot (cloud-sync risk). No silent choices.
- Stock-ID protection; backup rotation (keep 5).

### Preset generation (catalog → desktop OrcaSlicer)

Lifted `orca.py build_standalone_preset` + `preset_utils.flatten`: system/user template selection (user preferred, 131 fields), identity fields (`filament_vendor`/`filament_type` as arrays, `from="User"`, `filament_settings_id=[preset_name]`, `inherits=""`, unique `filament_id = "P" + md5(f"{name} {db_id}")[:13]` — note the space separator), temperature overrides from catalog, `.info` file with **empty** `sync_info` and empty `setting_id`/`base_id` (OrcaSlicer manages sync state and assigns `setting_id` on first Cloud push). Payload queued for companion agent.

Critical field-handling rules preserved when adapting to the catalog model (each is crash-causing or bug-causing if dropped):
- Remove the `type` field from generated user presets (OrcaSlicer crashes if present).
- `default_filament_colour`: `[""]` for non-hex color names; pass hex through (avoids white-swatch icon).
- Array-vs-scalar: `filament_density` stays scalar; most other fields are arrays of strings.
- kvParam override loop copies only keys already present in the template, skips identity keys (`inherits`, `name`, `filament_id`, `filament_vendor`, `filament_type`, `filament_settings_id`, `type`, `from`, `compatible_*`, `default_filament_colour`).
- After every printer-DB insert/patch/remove: recompute `result.count` (`count_autofix`).
- On `filament_id` collision or regeneration: check for and clean a `filament/base/` directory (OrcaSlicer would load stale values from it).

UI checklist mirrors the skill's manual steps: stop OrcaSlicer before applying (agent enforces too), delete Cloud preset if one exists, start OrcaSlicer to push to Cloud, verify with drift check.

### Temp sync (desktop presets → K2 DB)

Companion reports `nozzle_temperature[0]` (+ `initial_layer` mismatch flag) per preset, matched via score rule (+20 name substring, +10 vendor substring, type hard filter). Skip rules kept verbatim: tie at top score → ambiguous, skip with warning; top score < 30 → no_match, skip; winner is system preset → skip ("generate user preset first"); missing `nozzle_temperature` on either side → missing_temp, skip. Engine diffs against `kvParam.nozzle_temperature`, shows table, applies on confirm → normal printer push. Only `nozzle_temperature` syncs — verified as the only kvParam field persisted as print override (see skill docs).

Field relationship note: preset generation writes `nozzle_temperature = [max_temp]` from the catalog; the profile's separate `nozzle_temperature` field mirrors `kvParam.nozzle_temperature` — the temp-sync target that can diverge after Orca-side retuning.

### Drift detection

Per profile and target kind: compare `expected_hash` (last pushed) against `actual_state`. Hash input = canonical JSON of identity + parameter fields only (brand, name, type, temps, density, drying, pa, flow_ratio, max_volumetric, nozzle_temperature) — never full files. For presets, OrcaSlicer-assigned fields (`setting_id`, `base_id`, `updated_time`) are excluded so a correctly applied preset does not show false drift. For the printer, drift detection additionally keeps the skill's version-floor check (`int(actual_version) >= 9876543210`) plus entry presence — hash alone cannot see cloud-sync rollbacks of the version field. Dashboard renders green/yellow/red per profile.

## 5. Companion Agent Contract

Pull-based (Unraid cannot reach into the desktop):

1. `fm-agent poll` (or `--watch` daemon / systemd timer) → GET `/api/agent/jobs` with bearer token.
2. Job types:
   - `apply_preset`: payload contains preset JSON + `.info`; agent refuses with clear reason if an OrcaSlicer process is detected.
   - `report_presets`: scan the FIRST `~/.config/OrcaSlicer/user/<UUID>/filament/` directory that has a `filament/` subdir (same discovery as the skill's `find_orca_user_dir`; backup subdirs and secondary profile dirs ignored — scanning all dirs produces false ties).
3. Results POSTed to `/api/agent/results`; surfaced in UI ("applied", "refused: OrcaSlicer running", …).
4. Physical steps stay human: NFC tag writing, spool insertion, starting OrcaSlicer, Cloud-preset deletion — shown as UI checklists.

Config: app base URL + token in `~/.config/filament-manager/agent.json`.

## 6. UI Pages

Design work uses the **designer role** (omp designer agent) per AGENTS.md working instructions.

- **Dashboard** — spool stock summary, sync-health matrix, recent activity.
- **Profiles** — list/filter, create/edit forms grouped into identity / temps / extrusion / drying; validation warnings inline (vendor-in-name, tie warnings); delete behind double-confirm (typed token must match the profile name — Rule 5 semantics), refused while spools reference it unless force-confirmed.
- **Sync** — per-profile drift table; action buttons (push to printer / generate preset / sync temps); busy-printer decision dialogs; job progress.
- **Migration** (one-time) — pull printer DB + scan desktop presets via companion → propose catalog rows for existing entries (99001 Sunlu PLA+, 99002 eSUN PETG Basic, 99003 CAILAB PLA Silk, 99004 Cailab PLA+ Bio) → confirm import.

## 7. Error Handling

- Every job outcome lands in `activity_log`; failures render with cause and next-step options. Nothing silent.
- Reboot timeout → target marked `pending`, UI prompts manual reboot + re-verify.
- Agent unreachable / refused jobs → targets stay `pending` with visible reason.
- Cloud-sync overwrite detected at verify → status `error` with guidance (re-push; check reboot happened).

## 8. Testing & Local Test Facility

From day one, a proper local test facility backed by scripts (AGENTS.md rule: repeating actions go into scripts):

- `scripts/dev-start.sh` — starts the app locally (uvicorn, dev config, isolated SQLite in `.local/`), waits for readiness.
- `scripts/dev-stop.sh` — stops it deterministically (PID file).
- `scripts/test.sh` — full pytest suite.
- Unit tests ported/adapted from the skill suites (matching, flatten, version bump, guards, preset builder); new pytest coverage for API endpoints, job queue, drift computation, agent endpoints. SSH boundary mocked for engine tests.
- Manual smoke checklist after real deploy (health → migration import → one push cycle → verify), following the established verification protocol.

## 9. Deployment

Scripted (AGENTS.md rule: no hand-run deploy steps):

- `scripts/release.sh <version>` — semver tag, build Docker image (python-slim + sshpass + openssh-client + Python deps incl. `websocket-client` for the WS busy-check), save/loadable tarball or registry push.
- `scripts/deploy-unraid.sh` — copies image + updated XML template to Unraid (`ssh root@unraid`), restarts container.
- Config via `FM_*` env vars mapping the skill config fields: `FM_PRINTER_IP`, `FM_SSH_USER` (root), `FM_SSH_PASSWORD`, `FM_DB_REMOTE_PATH` (`/mnt/UDISK/creality/userdata/box/material_database.json`), `FM_WS_PORT` (9999), `FM_VERSION_OVERRIDE` (9876543210), `FM_ID_RANGE_START` (99001), `FM_ORCASLICER_CONFIG_DIR` (agent-side default `~/.config/OrcaSlicer`), `FM_AGENT_TOKEN`; data volume `/mnt/user/appdata/filament-manager` (SQLite + logs).
- Versioning: git tags `vX.Y.Z`; app exposes version in UI footer and `/api/version`.
- Docs synced on release: README + BrainVault note (documentation-sync convention).

## 10. AGENTS.md Working Instructions

Created as `AGENTS.md` in the repo root. Summary:

1. **Designer role for UI**: all UI design/refinement tasks use the omp designer agent.
2. **Scripts over repetition**: any action likely to repeat (dev server lifecycle, tests, builds, deploys, DB migrations) goes into a script under `scripts/`; agents call scripts, never ad-hoc command sequences.
3. Skill repo `~/Dokumente/Daten/Development/skills/Creality-custom-filament` is reference material — lift logic from there; do not modify it.
4. The iron rules are restated there verbatim so every agent session sees them without reading this spec.

## 11. Open Items for Implementation Planning

- Migration details: mapping current 4 entries + 3 presets into catalog rows; preserving `filament_id`s to avoid Cloud collisions.
- Whether `fm-agent --watch` ships in v1 or only manual `poll`.
- HTMX interaction patterns for busy-printer decision dialogs (designer input).
