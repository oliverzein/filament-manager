# AGENTS.md — Working Instructions for filament-manager

Instructions for AI agents (and humans) working in this repository.

## Project
Web app to manage 3D-printing filament spool inventory and sync to a
Creality K2 printer DB from Orca Cloud filament presets. Orca Cloud is
the sole source of presets — presets are created and tuned in OrcaSlicer
Desktop, synced to Orca Cloud, and the Manager caches them read-only.
See `docs/superpowers/specs/2026-09-04-filament-manager-rework.md` for the
rework design (original design spec archived under `docs/archive/superpowers/specs/`).

## Working Rules

1. **Designer role for UI**: All UI design/refinement work uses the omp
   designer agent. Do not hand-roll UI decisions ad hoc.
2. **Scripts over repetition**: Any action likely to repeat goes into a script
   under `scripts/` and agents call that script — never re-type ad-hoc command
   sequences. This includes: dev server lifecycle (`scripts/dev-start.sh`,
   `scripts/dev-stop.sh`), tests (`scripts/test.sh`), release
   (`scripts/release.sh`), deploy (`scripts/deploy-unraid.sh`), DB migrations.
   If you run something twice manually, promote it to a script.
3. **Skill repo is reference material**: Logic is lifted from
   `~/Dokumente/Daten/Development/skills/Creality-custom-filament`
   (`skill/cfs.py`, `skill/orca.py`, `skill/preset_utils.py`, `SKILL.md`,
   `docs/cfs.md`). Never modify that repo.
4. **Dev server restart**: After changing UI routes (`fm/api/ui.py`), restart
   the dev server to pick up new endpoints. Templates and CSS are picked up
   without restart.
5. **Orca Cloud is the sole source**: Do not add local profile editing,
   preset generation, or catalog CRUD. Presets are created and tuned in
   OrcaSlicer Desktop and synced to Orca Cloud. The Manager caches them
   read-only in `profile_cache`. Spools link to cached presets via
   `cloud_resource_id`.
6. **No model migrations**: Use existing columns and `SyncTarget.actual_state`
   for new data. Do not add columns to the Profile or Spool models.

## Iron Rules (non-negotiable, lifted from the skill)

1. Version ≥ 9876543210 + reboot mandatory after every printer-DB write;
   busy-check runs BEFORE any upload.
2. Custom printer IDs only in 99xxx range; stock IDs protected.
3. Backup on printer before every write; rotate, keep 5.
4. Double-confirm on delete (typed token must match).
5. Refuse push when DB < 30 entries (`MIN_DB_ENTRIES`) unless explicitly forced.

## Key Files

| `fm/api/ui.py` | UI routes (pages, forms, HTMX fragments). Restart dev server after adding routes. |
| `fm/api/templates/` | Jinja2 templates. Changes picked up without restart. |
| `fm/api/static/app.css` | Stylesheet. Changes picked up without restart. |
| `fm/orca_cloud.py` | Orca Cloud Sync API client (pull, Supabase auth, token rotation). |
| `fm/service/sync_handlers.py` | Sync logic: push, verify, github_backup, `compute_printer_db_drift`, printer DB operations. |
| `fm/service/cache_sync.py` | Orca Cloud cache sync: `sync_profile_cache` (pull presets into `profile_cache`). |
| `fm/presets/flattener.py` | Preset flattening, `find_preset_by_name`, `find_system_preset`. |
| `fm/github_backup.py` | GitHub backup client (commit presets as JSON). |
| `fm/settings_store.py` | Settings.json read/write (printer config, Orca Cloud token, GitHub config). |

## Verification

Before claiming any change works: run `scripts/test.sh`; for UI changes,
verify against the running app started via `scripts/dev-start.sh`. Real-printer
operations are verified with a push → verify cycle only against the test
printer setup described in the spec.
