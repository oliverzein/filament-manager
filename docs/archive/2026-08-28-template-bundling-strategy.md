# OrcaSlicer Filament Templates — Bundling Strategy

## Date
2026-08-28

## Problem
Filament Manager generates OrcaSlicer presets by overlaying catalog profile fields (name, brand, type, temperature, density) onto a base template. The template provides default values for all other fields (fan speeds, retraction, plate temps, flow ratio, volumetric speed, etc.).

The template is selected by filament type (PLA → `fdm_filament_pla.json`, PETG → `fdm_filament_pet.json`, etc.). Currently only the 22 OrcaFilamentLibrary base templates are bundled (88KB).

Users want vendor/printer-specific templates available (e.g. "Creality Generic PLA @K2-all" instead of the generic `fdm_filament_pla`), and the ability to select which template to use per-profile.

## Findings

### OrcaSlicer Preset Inventory
- **7,053 filament JSON files** across all printer profiles in `/opt/orca-slicer/resources/profiles/`
- **6,650 unique by name**
- **30MB total** for filament presets only
- **78MB** for the entire profiles directory (includes process/printer presets)

### Preset Categories
1. **OrcaFilamentLibrary base** (22 files, 88KB) — `fdm_filament_*.json`, type-level defaults (PLA, PETG, ABS, etc.). No vendor or printer specificity. These are the universal defaults.
2. **Vendor "Generic" presets** (~500 files, ~3MB) — e.g. "Creality Generic PLA @K2-all", "Anker Generic ABS". Tuned per vendor + printer model. Still "Generic" — not brand-specific filament.
3. **Nozzle-specific variants** — e.g. "Anker Generic PLA 0.2 nozzle", "0.25 nozzle". Same material, different nozzle diameter tuning.
4. **Printer-specific variants** — e.g. "Creality Generic PLA @Ender-3V3-all", "@K1-all", "@K2-all". Same material, tuned for specific printer hardware.
5. **No third-party filament presets** — OrcaSlicer does not ship Sunlu, eSUN, Polymaker, or other third-party brand presets. Only "Generic" per vendor/printer.

### Key Insight
OrcaSlicer has no "Sunlu PLA+" template. All third-party filament presets are user-created (via the skill's `orca.py preset` command or manual entry). The "template" in OrcaSlicer terms is always a Generic base — the user tunes from there.

## Options

### Option 1: Bundle all filament presets (30MB)
Copy every `filament/*.json` into `fm/presets/templates/`. User picks from a dropdown of 6,650 names.

**Pros:** Complete coverage, any printer/vendor combination available.
**Cons:** 30MB bundle size. Mostly noise — printer-specific and nozzle-specific variants that aren't useful as templates. Dropdown would need filtering/searching. Many presets inherit from each other (requires flattening).

### Option 2: Bundle OrcaFilamentLibrary base only (22 files, 88KB) — current
Only type-level defaults. One template per filament type.

**Pros:** Tiny, deterministic, no ambiguity. Already implemented.
**Cons:** No vendor/printer specificity. A Creality K2 user gets the same PLA defaults as an Anker user.

### Option 3: Bundle per-vendor "Generic" presets (~500 files, ~3MB)
One "Generic PLA" per vendor+printer (e.g. "Creality Generic PLA @K2-all"). User picks the vendor/printer that matches their setup.

**Pros:** Printer-specific tuning available. Manageable size. Meaningful choices.
**Cons:** Requires flattening inherited presets. Need to filter to one per vendor+printer+type (deduplicate nozzle variants). ~3MB bundle.

### Option 4: Bundle per-vendor "Generic" presets, flattened and deduplicated (~100 files, ~500KB)
Same as Option 3 but flatten inheritance, remove nozzle variants, keep only one preset per vendor+type (not per printer model). E.g. "Creality Generic PLA" (flattened from the best available variant).

**Pros:** Small, meaningful, no inheritance complexity.
**Cons:** Loses printer-model specificity. Requires deciding which variant to keep when multiple exist.

## Recommendation
**Option 3** — bundle per-vendor "Generic" presets. The K2 is the target printer, so "Creality Generic *" presets are the most relevant. ~3MB is acceptable for a Docker image. The UI would show a template dropdown filtered by the profile's filament type, with vendor-prefixed names.

## UI Design (for any option with multiple templates)
- Profile edit page, section 03 ("Print settings"): show a `<select>` dropdown with available templates filtered by the profile's `type` field.
- Default selection: the OrcaFilamentLibrary base template for that type.
- Template name stored on the `Profile` model as `template_override` (nullable — null = use type-based default).
- On push (`handle_generate_preset`): use `profile.template_override` if set, otherwise fall back to `find_system_preset(profile.type)`.
- Changing the type dropdown updates the template dropdown via HTMX (filter by type).

## Technical Notes
- Presets that `inherit` from a parent must be flattened (parent fields as base, child overrides on top) before use as a template. The `flatten_preset()` function in `flattener.py` already does this.
- Nozzle-specific variants (0.2mm, 0.25mm) should be excluded — the Filament Manager doesn't track nozzle diameter.
- The `@base` variants are intermediate inheritance nodes, not standalone presets — exclude them.
- Printer-specific variants (`@K2-all`, `@Ender-3V3-all`) are useful if the user's printer is known. Could be filtered by a printer model setting.
