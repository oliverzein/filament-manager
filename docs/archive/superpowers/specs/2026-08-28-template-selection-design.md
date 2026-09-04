# OrcaSlicer Template Selection — Design Spec

## Date
2026-08-28

## Problem
Filament Manager generates OrcaSlicer presets by overlaying catalog profile fields onto a base template. Currently only the 22 OrcaFilamentLibrary type-level templates are bundled (`fdm_filament_pla.json`, etc.). Users cannot select vendor/brand-specific templates.

**Use case:** User gets a "Bambu PETG Translucent" spool. They create a profile (brand=Bambu Lab, name=PETG Translucent, type=PETG). They should be able to pick "Bambu PETG Translucent" from a template dropdown — just like in OrcaSlicer — so the generated preset has Bambu-specific tuning (flow ratio 0.95, fan 30/10, nozzle 245°C, plate temps, etc.).

## Findings

### OrcaSlicer Preset Hierarchy
```
fdm_filament_pet                    ← type-level base (22 files, already bundled)
  └─ Bambu PETG Translucent @base   ← brand-specific tuning (THE template)
       ├─ @BBL A1                   ← printer-specific (adds compatible_printers)
       │    └─ @BBL A1 0.2 nozzle   ← nozzle-specific (minor overrides)
       └─ @BBL A1M ...
```

### @base Preset Inventory
- **463 `@base` filament presets** across all vendor directories
- **356 unique display names** (some duplicated across vendor dirs)
- **~1.5MB total** for `@base` files only
- `instantiation: "false"` — designed as templates, not standalone presets
- Inherit from `fdm_filament_*` (already bundled) or from another `@base` (6 cases)
- Contain the real tuning data (flow ratio, fan speeds, temps, plate temps)
- Printer-agnostic (no `compatible_printers` field)

### Vendor Metadata
- **369 of 463** `@base` presets have `filament_vendor` field directly
- **94 missing** `filament_vendor` — all inherit from `fdm_filament_*` which also lacks it
- Missing vendors derivable from preset name prefix: Snapmaker (43), Generic (28), Anker (13), OrcaArena (8), PolyLite (1), Bambu (1)
- **22 unique vendors**: Bambu Lab (88), Polymaker (79), BETA (44), Snapmaker (44), SUNLU (21), Orca Arena (20), Overture (14), Elegoo (14), COEX 3D (14), DREMC (14), Cubicon (9), Generic (9), FILL3D (7), eSUN (6), FDplast (6), AliZ (5), Elas (4), Valment (4), NIT (3), FilAr (3), FusRock (2), Numakers (2), GreenGate3D (1)

### Filament Type Metadata
- **136 of 463** `@base` presets have `filament_type` field directly
- **327 missing** — type is inherited from parent (`fdm_filament_pet` → PETG, etc.)
- Type must be resolved at bundle time by flattening the inheritance chain

### Duplication
Some `@base` presets exist in both vendor-specific dirs and `OrcaFilamentLibrary/filament/<subdir>/`. The vendor-specific copy may have additional fields (e.g., `filament_start_gcode`). **Deduplication strategy: prefer vendor-specific copy** (BBL/Creality/etc. dir) over OrcaFilamentLibrary copy.

## Architecture

### Overview
1. **Bundle `@base` presets** into `fm/presets/templates/` alongside existing `fdm_filament_*.json`
2. **Generate a template index** (`fm/presets/template_index.json`) at bundle time with vendor + type metadata
3. **Add `template_override` column** to `Profile` model (nullable string)
4. **API endpoint** `GET /api/templates?type=PETG&vendor=Bambu+Lab` for filtered template listing
5. **UI template dropdown** in profile form with intelligent type + vendor filtering
6. **Builder integration** — use `template_override` when building presets

### Data Flow
```
User creates/edits profile
  → selects type (PETG) → template dropdown filters by type
  → types brand (Bambu) → template dropdown further filters by vendor
  → picks template "Bambu PETG Translucent"
  → saves profile with template_override = "Bambu PETG Translucent"
  
Generate preset job:
  → _preset_template_factory(profile_dict)
  → if template_override: find_preset_by_name(template_override + " @base")
  → flatten_preset() → resolves fdm_filament_pet → Bambu PETG Translucent @base
  → build_standalone_preset(profile_dict, flattened_template)
  → push to Orca Cloud
```

## Components

### 1. Bundle Script (`scripts/build_templates.py`)

**Purpose:** One-time script to extract, deduplicate, and index `@base` presets from the local OrcaSlicer installation.

**Process:**
1. Scan `/opt/orca-slicer/resources/profiles/**/filament/**@base.json`
2. Deduplicate by display name (strip ` @base` suffix). When the same name exists in both a vendor dir and `OrcaFilamentLibrary/filament/<subdir>/`, prefer the vendor dir copy.
3. Copy each deduplicated `@base` file to `fm/presets/templates/` keeping the ` @base.json` suffix.
4. For each file, flatten via `flatten_preset()` to resolve `filament_type` and `filament_vendor` from the inheritance chain.
5. For presets still missing `filament_vendor` after flattening, derive from name prefix using a known-brands set.
6. Write `fm/presets/template_index.json`:
```json
{
  "templates": [
    {
      "name": "Bambu PETG Translucent",
      "vendor": "Bambu Lab",
      "type": "PETG",
      "file": "Bambu PETG Translucent @base.json"
    },
    {
      "name": "Generic PETG",
      "vendor": "Generic",
      "type": "PETG",
      "file": "fdm_filament_pet.json"
    }
  ]
}
```
7. The 22 existing `fdm_filament_*.json` files are included in the index with vendor="Generic", type mapped from `_TYPE_TO_TEMPLATE`.

**Output:**
- ~356 `@base` files in `fm/presets/templates/` (~1.5MB)
- 1 `template_index.json` (~15KB)
- Existing 22 `fdm_filament_*.json` files unchanged

**Note:** This script runs once during development. The generated files are committed to git. The script is kept for reproducibility when OrcaSlicer updates its presets.

### 2. Template Index Loader (`fm/presets/template_index.py`)

**Purpose:** Load and query the template index at runtime.

```python
from pathlib import Path
import json

_INDEX_PATH = Path(__file__).parent / "template_index.json"
_index_cache: dict | None = None

def load_index() -> dict:
    """Load template_index.json (cached after first call)."""
    global _index_cache
    if _index_cache is None:
        _index_cache = json.loads(_INDEX_PATH.read_text())
    return _index_cache

def templates_for_type(filament_type: str) -> list[dict]:
    """Return templates matching the given filament type, sorted by vendor then name.
    Generic templates first, then alphabetical by vendor."""
    idx = load_index()
    matches = [t for t in idx["templates"] if t["type"] == filament_type]
    # Sort: Generic first, then by vendor, then by name
    matches.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    return matches

def templates_for_type_and_vendor(filament_type: str, vendor: str) -> list[dict]:
    """Return templates matching type and vendor (case-insensitive partial match)."""
    idx = load_index()
    vendor_lower = vendor.lower().strip()
    matches = [t for t in idx["templates"] if t["type"] == filament_type]
    if vendor_lower:
        matches = [t for t in matches if vendor_lower in t["vendor"].lower()]
    matches.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    return matches

def all_vendors_for_type(filament_type: str) -> list[str]:
    """Return unique vendor names that have templates for the given type."""
    idx = load_index()
    vendors = sorted({t["vendor"] for t in idx["templates"] if t["type"] == filament_type})
    # Generic first
    if "Generic" in vendors:
        vendors.remove("Generic")
        vendors.insert(0, "Generic")
    return vendors
```

### 3. Model Change (`fm/models.py`)

Add `template_override` to `Profile`:
```python
template_override: Mapped[str | None] = mapped_column(String(255), default=None)
```

Nullable. Null = use type-based default (current behavior). Non-null = display name of selected template (e.g., "Bambu PETG Translucent").

### 4. DB Migration (`fm/db.py`)

Add to `_migrate()`:
```python
cols = {r[1] for r in conn.execute(text("PRAGMA table_info(profiles)"))}
if "template_override" not in cols:
    conn.execute(text("ALTER TABLE profiles ADD COLUMN template_override VARCHAR(255)"))
    conn.commit()
```

### 5. API Schema Updates (`fm/api/catalog.py`)

Add `template_override` to `ProfileCreate`, `ProfileUpdate`, `ProfileOut`:
```python
template_override: str | None = None
```

### 6. API Templates Endpoint (`fm/api/catalog.py`)

```python
@router.get("/templates")
def list_templates(type: str | None = None, vendor: str | None = None) -> dict:
    """List available preset templates, optionally filtered by type and vendor."""
    from ..presets.template_index import load_index, all_vendors_for_type
    idx = load_index()
    templates = idx["templates"]
    if type:
        templates = [t for t in templates if t["type"] == type]
    if vendor:
        vendor_lower = vendor.lower()
        templates = [t for t in templates if vendor_lower in t["vendor"].lower()]
    templates.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))
    vendors = all_vendors_for_type(type) if type else sorted({t["vendor"] for t in idx["templates"]})
    return {"templates": templates, "vendors": vendors}
```

### 6a. Template Preview Data (`fm/presets/template_index.py`)

Add a function to extract preview values from a flattened template:

```python
def preview_for_template(name: str) -> dict | None:
    """Flatten the named template and extract 4 key preview values.
    Returns None if the template file cannot be found."""
    from .flattener import find_preset_by_name, flatten_preset
    # Generic templates use the fdm_filament_* base directly
    idx = load_index()
    entry = next((t for t in idx["templates"] if t["name"] == name), None)
    if entry is None:
        return None
    path = find_preset_by_name(entry["file"])
    if path is None:
        return None
    flat = flatten_preset(path)
    def _first(key):
        v = flat.get(key)
        if isinstance(v, list) and v:
            return v[0]
        return v
    return {
        "nozzle_temperature": _first("nozzle_temperature"),
        "filament_flow_ratio": _first("filament_flow_ratio"),
        "fan_min_speed": _first("fan_min_speed"),
        "fan_max_speed": _first("fan_max_speed"),
        "filament_max_volumetric_speed": _first("filament_max_volumetric_speed"),
    }
```

### 7. UI: Template Picker (profile form)

**Placement:** Section 01 (Identity), as a full-width "Preset source" subgroup after the Type/Colors row and before Notes. The template choice determines which base tuning the generated preset uses.

**HTML structure:** A `.template-picker.full-width` div containing a heading ("Preset source"), a `#template-picker-fields` fragment target with two `<select>` controls and a preview area. See the designer review (`docs/superpowers/specs/2026-08-28-template-ui-review.md`) for the full HTML template.

**Controls:**
1. **Vendor filter** (`#template-vendor`): Optional `<select>`, first option "All vendors" (value=""), then vendor names for the selected type. UI-only — not persisted.
2. **Template** (`#template-override`): `<select>` with explicit first option "Use Generic `<type>` defaults (recommended)" (value=""), then templates grouped by vendor via `<optgroup>`. Full template names always shown.
3. **Template preview**: Compact `<dl>` showing 4 base values (nozzle temp, flow ratio, fan range, max volumetric) with note "Base values; profile settings may override them." Distinct from section 03's synced Orca Cloud values.

**Behavior:**
1. **Type filter (automatic):** Type `<select>` `change` → HTMX reloads `#template-picker-fields`. Server resets vendor to "All vendors", template to generic default, updates preview, announces "Template reset to Generic `<type>` defaults because material type changed."
2. **Vendor filter:** Vendor `<select>` `change` → HTMX reloads `#template-picker-fields`. Template list filters by type + vendor. Current template preserved only if present under filter; otherwise selects generic default.
3. **Template selection:** Template `<select>` `change` → HTMX reloads `#template-picker-fields` with updated preview values. Announces "`<template name>` selected. Preview updated."
4. **Brand input:** No HTMX request. Brand remains an ordinary required text input. Brand and template vendor are independent — a house brand may use a Generic preset.
5. **Default selection:** "Use Generic `<type>` defaults (recommended)" with value="" → `template_override = null`. The same type-level base must not be listed twice (not as both "Use default" and a "Generic" optgroup entry).
6. **Type change reset:** Type change clears `template_override`, clears vendor filter, loads new generic state, announces reset. Server-side validation also rejects/clears a template that doesn't belong to the submitted type.

**HTMX flow:**
- Type `<select>` `change` → `GET /profiles/{pid_or_new}/template-dropdown?type=PLA&reason=type-change` → swap `#template-picker-fields` innerHTML
- Vendor `<select>` `change` → `GET /profiles/{pid_or_new}/template-dropdown?type=PETG&template_vendor=Bambu+Lab&template_override=` → swap `#template-picker-fields` innerHTML
- Template `<select>` `change` → `GET /profiles/{pid_or_new}/template-dropdown?type=PETG&template_vendor=&template_override=Bambu+PETG+Translucent` → swap `#template-picker-fields` innerHTML
- All requests use `hx-swap="innerHTML focus-scroll:false"` to preserve focus

**Fragment endpoint:** `GET /profiles/{pid}/template-dropdown` and `GET /profiles/new/template-dropdown` — returns rendered HTML fragment (not JSON) with vendor select, template select, preview, and status message. Parameters: `type`, `template_vendor`, `template_override`, `reason` (optional, "type-change" triggers reset behavior).

**Form fields added:**
- `template_override: str = Form("")` — empty string = null (use generic default)
- `template_vendor: str = Form("")` — UI-only filter, not persisted (may be submitted but ignored)

**Accessibility:**
- Stable, unique `id` and explicit `<label for>` on every control
- `aria-describedby` associating template help text and preview note with template select
- `role="status" aria-live="polite" aria-atomic="true"` status region for result counts, resets, errors, preview updates
- `aria-busy="true"` on fragment target during requests; spinner with `aria-hidden="true"`
- Focus preservation via `innerHTML focus-scroll:false` swap + same `id` elements
- On request failure: retain usable generic default, show inline `.template-picker-error` with Retry button
- Native `<select>` and `<optgroup>` semantics retained throughout

**Loading/empty/error states:**
- Loading: keep current controls visible, mark fragment busy, show spinner
- No vendor-specific templates for filter: keep generic default, state "No vendor-specific templates for this filter"
- Endpoint error: keep generic default, show inline error with Retry button

**CSS:** See designer review (`2026-08-28-template-ui-review.md`) for complete CSS additions using existing design tokens (`.template-picker`, `.template-picker-fields`, `.template-preview`, `.template-preview-values`, `.template-picker-error`, responsive breakpoints).

### 8. Builder Integration (`fm/service/sync_handlers.py`)

Update `_preset_template_factory`:
```python
def _preset_template_factory(profile_dict: dict) -> dict | None:
    override = profile_dict.get("template_override")
    if override:
        from ..presets.flattener import find_preset_by_name, flatten_preset
        path = find_preset_by_name(override + " @base")
        if path:
            return flatten_preset(path)
        # Fall back to type-based default if override file not found
    return find_system_preset(profile_dict["type"])
```

Update `handle_generate_preset` to include `template_override` in `profile_dict`:
```python
profile_dict = {
    ...
    "template_override": profile.template_override,
}
```

### 9. Form Handler Updates (`fm/api/ui.py`)

Add `template_override` parameter to `profile_create_form` and `profile_update_form`:
```python
template_override: str = Form(""),
```

Add to `_profile_from_form`:
```python
"template_override": template_override or None,
```

Add to `_submitted_namespace`:
```python
template_override=template_override or None,
```

Server-side validation: if `template_override` is set, verify it belongs to the submitted `type` via `template_index.py`. If mismatched, clear it (set to None) and add a validation warning.

### 10. Orca Fragment Display (`fm/api/ui.py` + `orca_fragment.html`)

Update `orca_fragment` and `sync_from_orca` handlers to show human-readable template name:
```python
if profile.template_override:
    template_name = profile.template_override  # e.g. "Bambu PETG Translucent"
else:
    template_name = f"Generic {profile.type} defaults"  # e.g. "Generic PETG defaults"
```

Section 03 heading shows: "From Orca Cloud · template: `Bambu PETG Translucent`" or "From Orca Cloud · template: `Generic PETG defaults`".

## Edge Cases

1. **Template file missing at runtime:** If `template_override` references a file that doesn't exist (e.g., templates were rebuilt and the name changed), fall back to the type-based default. Log a warning.
2. **Type changed after template_override set:** UI resets `template_override` to generic on type change. Server-side validation also clears mismatched overrides. Builder falls back to type-based default as defense in depth.
3. **Multi-level @base inheritance (6 cases):** `flatten_preset()` already handles recursive inheritance. No special handling needed.
4. **New profile (not yet saved):** Fragment endpoint `/profiles/new/template-dropdown` works without a pid. Template dropdown filtered by type, vendor empty, shows all templates for the type. `template_override` saved with profile on form submit.
5. **No vendor-specific templates for a type:** Vendor filter shows only "All vendors". Template dropdown shows only the generic default. State "No vendor-specific templates available."
6. **Edit profile with template_override:** Saved template remains visible and selected. Vendor filter starts at "All vendors" unless the saved template's vendor is deliberately shown as active filter.
7. **Validation error re-render:** If validation fails for unrelated reasons, preserve the submitted compatible `template_override` rather than resetting it. Only reset on type mismatch.

## Testing

1. **Bundle script:** Run `scripts/build_templates.py`, verify `template_index.json` has ~356 entries with correct vendor + type metadata. Verify no missing types.
2. **Template index loader:** Test `templates_for_type("PETG")` returns Bambu PETG variants. Test `templates_for_type_and_vendor("PETG", "Bambu")` filters correctly. Test `all_vendors_for_type("PLA")` returns all PLA vendors. Test `preview_for_template("Bambu PETG Translucent")` returns flow_ratio=0.95, fan_max_speed=30.
3. **API endpoint:** `GET /api/templates?type=PETG` returns filtered list with vendors. `GET /api/templates?type=PETG&vendor=Bambu` returns Bambu-only.
4. **Fragment endpoint:** `GET /profiles/new/template-dropdown?type=PETG` returns HTML with vendor select + template select + generic preview. `GET /profiles/new/template-dropdown?type=PETG&template_override=Bambu+PETG+Translucent` returns HTML with Bambu preview values. `GET /profiles/new/template-dropdown?type=PLA&reason=type-change` returns reset state with status message.
5. **Model + migration:** Create profile with `template_override`, verify it persists. Verify migration adds column to existing DBs.
6. **Builder integration:** Generate preset with `template_override="Bambu PETG Translucent"`, verify the generated preset has Bambu-specific values (flow_ratio=0.95, fan_max_speed=30). Generate without override, verify it uses generic defaults.
7. **UI:** Create profile with type=PETG, verify template dropdown shows PETG templates grouped by vendor. Select vendor filter "Bambu Lab", verify template list narrows. Select "Bambu PETG Translucent", verify preview shows 4 values. Save, verify `template_override` persists. Edit profile, verify saved selection is shown.
8. **Type change reset:** Select type=PETG + template "Bambu PETG Translucent", change type to PLA, verify template resets to generic default and status message appears.
9. **Fallback:** Set `template_override` to a non-existent template name, verify preset generation falls back to type-based default.
10. **Server-side validation:** Submit form with `type=PLA` and `template_override="Bambu PETG Translucent"` (type mismatch), verify override is cleared and warning is shown.

## Non-Goals
- Printer-specific variants (`@BBL A1`, `@K2-all`) — not bundled. The `@base` presets are printer-agnostic.
- Nozzle-specific variants — not bundled. Filament Manager doesn't track nozzle diameter.
- Non-`@base` presets (the 6500+ instantiation=true presets) — not bundled. They only add `compatible_printers` metadata.
- Brand-to-vendor auto-suggestion — removed per designer review. Brand and template vendor are independent. A non-destructive suggestion after blur is a future enhancement.
- Searchable combobox — not needed for current inventory. Per-type lists are small enough for native `<select>` with `<optgroup>`.
