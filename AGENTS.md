# AGENTS.md — Working Instructions for filament-manager

Instructions for AI agents (and humans) working in this repository.

## Project

Web app to manage 3D-printing filament profiles + spool inventory, with sync
to a Creality K2 printer DB and OrcaSlicer presets. See
`docs/superpowers/specs/2026-08-26-filament-manager-design.md` for the design.

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

## Iron Rules (non-negotiable, lifted from the skill)

1. Version ≥ 9876543210 + reboot mandatory after every printer-DB write;
   busy-check runs BEFORE any upload.
2. Profile name must contain the vendor ("Sunlu PLA+", not "PLA+").
3. Custom printer IDs only in 99xxx range; stock IDs protected.
4. Backup on printer before every write; rotate, keep 5.
5. Double-confirm on delete (typed token must match).
6. Refuse push when DB < 30 entries (`MIN_DB_ENTRIES`) unless explicitly forced.
7. Never write OrcaSlicer presets while OrcaSlicer is running; `.info` gets
   empty `sync_info`/`setting_id`.

## Verification

Before claiming any change works: run `scripts/test.sh`; for UI changes,
verify against the running app started via `scripts/dev-start.sh`. Real-printer
operations are verified with a push → verify cycle only against the test
printer setup described in the spec.
