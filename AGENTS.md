# AGENTS.md — Working Instructions for filament-manager

Instructions for AI agents (and humans) working in this repository.

## Project

Web app to manage 3D-printing filament profiles + spool inventory, with sync
to a Creality K2 printer DB and OrcaSlicer presets. OrcaSlicer is the sole
editor of print settings; the Manager displays them read-only after sync.
See `docs/superpowers/specs/2026-08-26-filament-manager-design.md` for the
design.

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
5. **OrcaSlicer is the editor**: Do not add print-settings fields to the
   Profile model or the profile edit form. Print settings come from OrcaSlicer
   via the Orca Cloud Sync API and are stored in
   `SyncTarget.actual_state` (JSON). The profile page displays them read-only.
6. **No model migrations**: Use existing columns and `SyncTarget.actual_state`
   for new data. Do not add columns to the Profile or Spool models.

## Iron Rules (non-negotiable, lifted from the skill)

1. Version ≥ 9876543210 + reboot mandatory after every printer-DB write;
   busy-check runs BEFORE any upload.
2. Profile name must contain the vendor ("Sunlu PLA+", not "PLA+").
3. Custom printer IDs only in 99xxx range; stock IDs protected.
4. Backup on printer before every write; rotate, keep 5.
5. Double-confirm on delete (typed token must match).
6. Refuse push when DB < 30 entries (`MIN_DB_ENTRIES`) unless explicitly forced.

## Key Files

| File | Purpose |
|---|---|
| `fm/api/ui.py` | UI routes (pages, forms, HTMX fragments). Restart dev server after adding routes. |
| `fm/api/templates/` | Jinja2 templates. Changes picked up without restart. |
| `fm/api/static/app.css` | Stylesheet. Changes picked up without restart. |
| `fm/orca_cloud.py` | Orca Cloud Sync API client (push/pull/delete, Supabase auth, token rotation). |
| `fm/service/sync_handlers.py` | Sync logic, `refresh_preset_drift`, printer DB operations, Orca Cloud push/pull. |
| `fm/presets/matching.py` | OrcaSlicer preset matching, `find_orca_user_dir`, `load_presets`. |
| `fm/presets/builder.py` | Preset builder, `_KV_MAP` (catalog → Orca key mapping). |
| `fm/settings_store.py` | Settings.json read/write (printer connection config, Orca Cloud token storage). |

## Verification

Before claiming any change works: run `scripts/test.sh`; for UI changes,
verify against the running app started via `scripts/dev-start.sh`. Real-printer
operations are verified with a push → verify cycle only against the test
printer setup described in the spec.
