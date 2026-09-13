# Orca Cloud Preset Visual Diff — Feature Spec

**Date:** 2026-09-13
**Status:** Approved (planning)
**Repo:** `filament-manager`
**Plan:** `docs/superpowers/plans/2026-09-13-preset-visual-diff.md`

---

## 1. Purpose

Users tune filament presets in OrcaSlicer and want to know **how two presets
differ** — e.g. "eSUN PETG Basic Optimized" vs "SUNLU PETG Optimized"
(different max volumetric speed, bed temps, pressure advance). Today the Sync
Hub lists cached presets but offers no way to inspect or compare their
contents.

This feature adds a **two-preset visual diff** to the Sync Hub: pick Preset A
and Preset B from the cached Orca Cloud presets, and a read-only modal shows
both presets' **effective** field values side by side — inheritance chains
resolved via `flatten_preset` — grouped by category, with per-field status
(**diff** / **same** / **only_a** / **only_b**).

Comparing a preset against its own base template is supported as a special
case: Preset B can be the pseudo-id `@base`.

## 2. Global Constraints

- **Orca Cloud is sole source:** read-only pull via `sync_pull`; NO profile
  editing or write-back in this feature.
- **No model migrations:** do not add columns to `Profile` or `Spool` (or any
  model). The diff reads live cloud data; nothing is persisted.
- **Design system:** UI must integrate with the existing design system —
  DM Sans headings, Manrope UI font, dark palette, CSS variables in
  `fm/api/static/app.css` (`--surface`, `--border`, `--accent`, `--green`,
  `--amber`, `--red`, `--blue`, `--text-muted`, spacing/radius vars).
- **Tests:** `scripts/test.sh` must pass cleanly with zero regressions.

1. User opens `/sync` (Sync Hub). The "Preset & Status" panel features a
   dedicated **compare toolbar** (`.compare-toolbar`): two `<select>` dropdowns
   (Preset A, Preset B) separated by a visual `vs` connector, listing all
   cached presets, plus a "Compare" button with an icon. Preset B's dropdown
   also offers `— Base template of A —` (submits `preset_b_id="@base"`).
2. Submitting the picker opens a dedicated diff modal (`#diff-modal`,
   separate from the job `#action-modal`) with a spinner, then HTMX POSTs
   `preset_a_id` + `preset_b_id` to `POST /sync/compare-modal` and swaps the
   returned fragment into `#diff-modal-body`.
3. Each non-orphan preset row also gets a ghost **"Compare"** button that
   POSTs only `preset_a_id`; the endpoint returns a **Preset-B picker
   fragment** inside the modal. The modal prominently displays Preset A's
   identity in a confirmation card (`.diff-pick-preset-a`) and provides a
   dropdown for Preset B, providing clear context on what is being compared.
4. The diff modal provides a professional side-by-side inspection workspace:
   - **Sticky header**: Category pill eyebrow, `A vs B` heading, summary
     pills (`N different`, `M identical`, `X only in A`, `Y only in B`), and
     an accessible "Only differences" toggle switch.
   - **Sticky table header**: Pins `Parameter`, `Preset A: [name]`,
     `Preset B: [name]`, and `Status` while scrolling.
   - **Categorized parameter table**: Category divider bars with parameter
     counts, monospace parameter keys under formatted labels, tabular numbers
     with subtle diff row tints, and formatted `<pre><code>` blocks for
     multiline G-code scripts.
5. Esc, backdrop click, or Close button dismisses the modal with focus restored.
   No state is written; the hub is not refreshed (nothing changed).
## 4. Architecture

```
Compare picker / row "Compare" button
  → POST /sync/compare-modal (preset_a_id[, preset_b_id])   [fm/api/ui.py]
  → preset_b_id absent → Preset-B picker fragment (ProfileCache list)
  → build_preset_diff(preset_a_id, preset_b_id)             [fm/service/preset_diff.py]
      → sync_handlers._orca_client_factory()  (module-attr call; tests monkeypatch it)
      → client.sync_pull(cursor=0)                          [read-only, one pull]
      → _cloud_presets_to_local(upserts) → {resource_id: preset}
      → preset_b_id == "@base" → flattened inherits chain of A
      → diff_presets(preset_a, preset_b, flatten=True)
          → _flatten_dict: flatten_preset temp-file round-trip per side
          → normalize + compare per field → status
  → _preset_diff_modal.html fragment → #diff-modal-body
```

### 4.1 Diff semantics

- `diff_presets(preset_a, preset_b, flatten=True)` compares the **effective**
  values of both presets: each side is flattened via `flatten_preset`
  (temp-file round-trip, resolving `inherits` against bundled templates in
  `fm/presets/templates/`, multi-level). With `flatten=False` the raw field
  sets are compared (used by tests; keeps the engine honest about what is
  explicitly set vs inherited).
- **Value normalization:** unwrap single-element lists (`["220"]` → `"220"`),
  `None`/empty list → `""`, `True`/`False` → `"1"`/`"0"`, everything else →
  `str(v).strip()`. Equality tries `float()` on both sides first (so `"21"`
  equals `"21.0"`), falls back to string equality.
- **Field status** per key (union of both sides' keys, minus meta keys):
  - `diff` — in both, normalized values differ
  - `same` — in both, normalized values equal
  - `only_a` — present only in preset A
  - `only_b` — present only in preset B
- **Meta keys excluded** from the field list: `type`, `name`, `inherits`,
  `from`, `setting_id`, `instantiation`, `version`, `compatible_printers`,
  `filament_settings_id`, `updated_time`, `_path`, `_system`.
- **Categories** (fixed display order): Identity, Temperature,
  Flow & Pressure, Retraction, Cooling, G-Code, Other. Mapping table in
  `preset_diff.py`; unmapped keys → Other. Empty categories are omitted.
- **Field labels:** derived from the key — strip `filament_` prefix,
  underscores → spaces, capitalize (`nozzle_temperature` → "Nozzle
  temperature").
- **Multiline values** (e.g. `filament_start_gcode`): fields whose value on
  either side contains `\n` get `multiline: True`; the template renders them
  in a scrollable `<pre>` instead of inline text.
- **`@base` special case:** `build_preset_diff(a_id, "@base")` resolves B to
  A's flattened `inherits` chain (`find_preset_by_name` → `flatten_preset`).
  Raises `PresetDiffError` when A is standalone or the base template is not
  bundled.

### 4.2 Error handling

All failure modes render an error fragment inside the modal (HTTP 200,
consistent with existing modal endpoints):

| Condition | Message |
|---|---|
| Orca Cloud not configured | "Orca Cloud not connected — set up the connection in Settings" |
| `sync_pull` raises | "Cloud pull failed: <err>" |
| Resource id not in pull result | "Preset not found in Orca Cloud: <id>" |
| `@base` on standalone/unresolvable preset | "Preset has no resolvable base template" |

## 5. Interfaces

`fm/service/preset_diff.py` produces:

```python
class PresetDiffError(Exception):
    """User-facing failure while building a preset diff."""

def fetch_cloud_presets(client) -> dict[str, dict]
    # {resource_id: local-format preset} — one sync_pull, filament-only
    # raises PresetDiffError on pull failure

def diff_presets(preset_a: dict, preset_b: dict, flatten: bool = True) -> dict
    # {"name_a": str, "name_b": str,
    #  "summary": {"diff": int, "same": int, "only_a": int, "only_b": int},
    #  "categories": [{"name": str, "fields": [
    #      {"key": str, "label": str, "status": str,
    #       "value_a": str, "value_b": str, "multiline": bool}]}]}
    # status: diff | same | only_a | only_b

def build_preset_diff(preset_a_id: str, preset_b_id: str) -> dict
    # orchestrates factory → fetch → diff; "@base" resolves to A's base chain
    # raises PresetDiffError
```

`fm/api/ui.py` consumes `build_preset_diff` + `PresetDiffError` and renders
`_preset_diff_modal.html` with:
- `{"diff": diff}` on success
- `{"error": str}` on error
- `{"pick_b": True, "preset_a_id": str, "preset_a": ProfileCache | None, "presets": list[ProfileCache]}`
  when `preset_b_id` is omitted (row compare action).

## 6. UI Details

### 6.1 Design Tokens & Typography
- **Typography**: Headings use `font-family: var(--font-heading)` ('DM Sans'),
  UI and body text use `font-family: var(--font-ui)` ('Manrope'), and G-Code /
  parameter keys use monospaced code fonts. Numeric values enforce
  `font-variant-numeric: tabular-nums;`.
- **Dark palette tokens**:
  - Backgrounds: `--bg: #081028`, `--surface: #0b1739`, `--surface-alt: #0a1330`, `--surface-raised: #101d42`.
  - Borders: `--border: rgba(217, 225, 250, .14)`, `--border-strong: rgba(209, 219, 249, .25)`.
  - Status & diff accents: Diff warning amber (`#ffd070`, background `rgba(253,181,42,.12)`),
    Identical muted (`var(--text-muted)` / `var(--text-secondary)`),
    Only A / Only B cyan-blue (`#77c3ff`, background `rgba(56,160,255,.12)`).
  - All foreground colors exceed WCAG AAA contrast ratios (>= 7:1) on dark surfaces.

### 6.2 Compare Toolbar (`_sync_hub_grid.html`)
- Integrated into the top of the `#preset-table.panel` right under `.panel-heading` as `.compare-toolbar`.
- Structured `<form class="compare-picker">` containing:
  - `.compare-inputs`: Flexbox container wrapping Preset A label/select, `.compare-vs-badge` separator ("VS"), and Preset B label/select.
  - Options display formatted vendor + preset name; Preset B select includes `— Base template of A —` with value `@base`.
  - Submit button: `.button.button-secondary.compare-submit-btn` with split-pane comparison icon.
  - HTMX wired: `hx-post="/sync/compare-modal"`, `hx-target="#diff-modal-body"`, `hx-swap="innerHTML"`, `hx-on::before-request="openDiffModal()"`.

### 6.3 Row Action (`_sync_hub_grid.html`)
- Non-orphan rows gain a `.button.button-ghost` labeled "Compare" in the actions cell, posting `preset_a_id` to `/sync/compare-modal`.

### 6.4 Diff Modal Scaffolding (`sync_hub.html`)
- Overlay `#diff-modal` mirrors `#action-modal` a11y standards:
  `role="dialog"`, `aria-modal="true"`, `aria-label="Preset diff"`, `tabindex="-1"`, Esc/backdrop close, focus restoration.
- Container: `.diff-modal-dialog` sized at `width: min(1040px, 95vw); max-height: 88vh;` with flex column layout.

### 6.5 Modal Fragment (`_preset_diff_modal.html`)
Three states:
1. **`error`**:
   - Reuses `.push-modal-state.push-modal-error` pattern with error icon and message.
2. **`pick_b`**:
   - Header with title "Choose Preset B" and subtitle.
   - Confirmation card `.diff-pick-preset-a` showing Preset A's vendor and name so the user knows what they are comparing against.
   - Select dropdown for Preset B with `@base` option.
   - Actions: Cancel button (`button-ghost`) and Compare button (`button-primary`).
3. **`diff`**:
   - **Header (`.diff-modal-head`)**:
     - Title row with "Preset Comparison" eyebrow, side-by-side preset names with visual `vs` badge, and quick close button (`✕`).
     - Controls row with summary pills (`N different`, `M identical`, `X only in A`, `Y only in B`) and an accessible custom toggle switch (`.diff-only-toggle`) for "Only differences".
   - **Table (`.diff-table-wrap > table.diff-table`)**:
     - Sticky `thead` (`position: sticky; top: 0; z-index: 10; background: var(--surface-raised);`) showing Parameter, Preset A (with preset name subtitle), Preset B (with preset name subtitle), and Status.
     - Category divider rows (`tr.diff-cat > td[colspan=4]`) displaying category name and field count.
     - Field rows (`tr.diff-row.diff-row-<status>`):
       - Parameter cell: human-readable label (`.field-label`) and technical key (`.field-key`).
       - Values: tabular numbers for numerics, `—` for empty values, and scrollable `<pre class="diff-pre"><code>` for multiline G-code scripts.
       - Highlight: diff rows feature subtle amber tinting on row hover and values.
       - Status cell: `.status-badge.status-<status>` with symbolic prefix (`≠ Diff`, `= Same`, `Only A`, `Only B`).
   - **Footer (`.diff-modal-foot`)**:
     - Fixed footer with Close button.

### 6.6 "Only Differences" Toggle Behavior
- Checkbox input triggers `toggleDiffOnly(this)`.
- Toggles `.diff-only` class on `#diff-modal-body`.
- CSS hides identical rows: `.diff-only .diff-row-same { display: none; }`.
- JavaScript recalculates category visibility: any `.diff-cat` with 0 visible rows is hidden, keeping the view clean.

### 6.7 Responsive Specifications
- **Desktop (>= 1100px)**: 4-column side-by-side grid, sticky header, full width comparison.
- **Tablet (760px - 1099px)**: Form controls stack vertically in toolbar; table preserves side-by-side comparison with smooth horizontal scrolling inside `.table-wrap`.
- **Mobile (< 760px)**:
  - Modal expands to full-screen sheet (`width: 100vw; height: 100vh; max-height: 100vh; border-radius: 0;`).
  - Controls row stacks summary pills and toggle switch.
  - Table maintains side-by-side layout with min-width and horizontal scroll so users can compare values on one screen without confusing stacked interleaving.
## 7. Non-goals

- Diffing preset vs printer-DB entry (drift already covered by
  `compute_printer_db_drift` / `_entry_drift_detail`).
- Editing, adopting, merging, or writing back any preset values.
- Caching diff results; every comparison fetches fresh cloud state.
- Orphan presets (deleted from cloud) are excluded from the picker and get
  no Compare button — the cloud fetch would fail by definition.
- Comparing more than two presets.

## 8. Acceptance Criteria

1. `POST /sync/compare-modal` with two known ids returns a categorized
   side-by-side diff fragment; inherited fields compare by effective value
   (flattened), not raw presence.
2. `preset_b_id="@base"` diffs A against its resolved base template.
3. Missing/unknown ids, unconfigured cloud, and unresolvable `@base` render
   error fragments (HTTP 200).
4. Posting only `preset_a_id` renders the Preset-B picker fragment.
5. `/sync` renders the compare picker (both selects populated from cached
   presets), the diff-modal scaffolding, and a Compare button per non-orphan
   row.
6. `scripts/test.sh` passes with zero regressions.
