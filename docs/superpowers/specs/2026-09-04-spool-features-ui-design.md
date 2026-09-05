# Spool Features — UI/UX Design Specification

**Date:** 2026-09-04
**Status:** Design complete — ready for implementation
**Repo:** `/home/oliverzein/Dokumente/Daten/Development/3D Printing/filament-manager`
**Route:** `GET /spools` (list + add form + inline edit forms)
**Template:** `fm/api/templates/spools.html`
**Stylesheet:** `fm/api/static/app.css`

---

## 1. Purpose

Design the UI/UX for five new features in the Spool Inventory page and
its "Add spool" / "Edit spool" dialogs:

1. **Hex Code Input** — text input for hex color codes, bidirectionally
   synced with the preset dropdown, native color picker, and swatch.
2. **Direct Number Input for "Remaining"** — numeric input alongside the
   range slider, bidirectionally synced.
3. **Quantity Input ("Menge")** — field in the Add form to create N
   identical spools in one submission.
4. **Color Name Attribute (`color_name`)** — descriptive text field,
   auto-populated from preset selection, displayed in the Active Spools
   table.
5. **Profile Dropdown Vendor Deduplication** — fix "Sunlu Sunlu PLA+
   Optimized" by conditionally omitting the vendor when it is already a
   prefix of the profile name.

All snippets reuse existing CSS variables (`--surface`, `--border`,
`--accent`, `--text-secondary`, `--text-muted`, `--surface-alt`,
`--surface-raised`, `--border-strong`, `--radius-input`, etc.) and
existing component classes (`.form-grid`, `.color-row`, `.swatch`,
`.range-field`, `.button`, `.panel`, etc.).

---

## 2. Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Hex input placed below the `.color-row` inside the same `<label>` | Keeps the color controls compact; hex is a secondary input, not a peer of the dropdown |
| Number input placed inline with the range slider in a `.range-row` flex container | Visual pairing; user sees both representations simultaneously |
| `name="remaining_pct"` moves to the number input; slider becomes nameless companion | Number input is always submittable (works without JS); slider is a progressive enhancement |
| Quantity field placed in column 3 of row 2 in the Add form | Balances the grid; groups with Remaining (both numeric); only in Add form (not Edit — editing one spool's quantity is meaningless) |
| Color Name placed in column 1 of row 2 (directly below Color) | Vertical alignment with Color field signals semantic grouping |
| Color Name auto-populate uses a `nameModified` flag, not `lastAutoName` comparison | Simpler, handles partial typing correctly, resets on field clear |
| Vendor dedup uses a Jinja2 macro `plabel(p)` defined once at template top | DRY — same logic in filter dropdown, Add dropdown, Edit dropdown, and orphan option |
| Vendor dedup checks `name.lower().startswith(vendor.lower() + ' ')` or exact equality | Prevents false positives like "Sunlu" matching "SunluLite"; handles case-insensitivity |
| Edit form Notes moves from `.full-width` to a regular grid cell | Fills the 6th slot in the 3-col grid cleanly; no empty gaps |
| `color_name` has no model column (AGENTS.md Rule 6) | Backend stores it in the existing `notes` column using a `color_name:` prefix convention, or the implementer coordinates an alternative — see §11 |

---

## 3. Form Layout (Add Form)

### Current layout (4 fields, 3-col grid)

```
Row 1: Profile   | Color          | Location
Row 2: Remaining | (empty)        | (empty)
```

### New layout (6 fields, 3-col grid)

```
Row 1: Profile    | Color          | Location
Row 2: Color Name | Remaining      | Quantity
```

Every cell is filled. The grid is balanced with exactly 2 rows × 3
columns. Color Name sits directly below Color (same column) for visual
grouping. Remaining and Quantity are both numeric fields, placed
adjacent in row 2.

### Full Add form HTML

```html
<details>
  <summary class="button button-primary">＋ Add spool</summary>
  <form method="post" action="/spools/form" class="add-form">
    <div class="form-grid three-col">

      {# ── Row 1 ── #}

      {# Profile #}
      <label>
        <span>Profile</span>
        <select name="cloud_resource_id" required>
          {% for p in profiles %}
          <option value="{{ p.cloud_resource_id }}">{{ plabel(p) }}</option>
          {% endfor %}
        </select>
      </label>

      {# Color: swatch + preset dropdown + native picker + hex input #}
      <label class="color-field">
        <span>Color</span>
        <div class="color-row">
          <span class="swatch color-row-swatch" id="add-color-swatch"
                style="background:#cccccc"></span>
          <select name="color_override" id="add-color-select">
            <option value="#cccccc">Custom</option>
            <option value="#ffffff">White</option>
            <option value="#f5f5dc">Beige</option>
            <option value="#d9d9d9">Light Grey</option>
            <option value="#808080">Grey</option>
            <option value="#404040">Dark Grey</option>
            <option value="#1a1a1a">Black</option>
            <option value="#ff0000">Red</option>
            <option value="#c0392b">Dark Red</option>
            <option value="#ff5e4d">Coral</option>
            <option value="#ff8c00">Orange</option>
            <option value="#e67e22">Dark Orange</option>
            <option value="#ffd700">Gold</option>
            <option value="#f1c40f">Yellow</option>
            <option value="#9acd32">Yellow Green</option>
            <option value="#2ecc71">Green</option>
            <option value="#27ae60">Dark Green</option>
            <option value="#00b894">Teal</option>
            <option value="#1abc9c">Cyan</option>
            <option value="#3498db">Blue</option>
            <option value="#2980b9">Dark Blue</option>
            <option value="#2c3e50">Navy</option>
            <option value="#9b59b6">Purple</option>
            <option value="#8e44ad">Dark Purple</option>
            <option value="#e91e63">Magenta</option>
            <option value="#e8b4b8">Pink</option>
            <option value="#964b00">Brown</option>
            <option value="#5d4037">Dark Brown</option>
            <option value="#c0c0c0">Silver</option>
            <option value="#d4af37">Gold Metallic</option>
          </select>
          <input type="color" id="add-color-custom" value="#cccccc"
                 title="Custom color picker" class="color-custom-btn"
                 aria-label="Open native color picker">
        </div>
        <input type="text" id="add-color-hex" value="#cccccc"
               placeholder="#RRGGBB" class="hex-input" maxlength="7"
               inputmode="text" autocomplete="off"
               aria-label="Hex color code">
      </label>

      {# Location #}
      <label>
        <span>Location</span>
        <input name="location" value="" placeholder="e.g. Dry box A">
      </label>

      {# ── Row 2 ── #}

      {# Color Name #}
      <label>
        <span>Color name</span>
        <input type="text" name="color_name" id="add-color-name"
               value="" placeholder="e.g. Vivid Yellow"
               maxlength="60" autocomplete="off">
      </label>

      {# Remaining: slider + number input, bidirectionally synced #}
      <label class="range-field">
        <span>Remaining <output id="add-remaining-output">100%</output></span>
        <div class="range-row">
          <input type="range" id="add-range" min="0" max="100" value="100"
                 aria-label="Remaining material (slider)">
          <input type="number" name="remaining_pct" id="add-number"
                 min="0" max="100" value="100" class="range-number"
                 aria-label="Remaining material (percent)">
        </div>
      </label>

      {# Quantity — Add form only #}
      <label>
        <span>Quantity</span>
        <input type="number" name="quantity" id="add-quantity"
               min="1" max="50" value="1" class="range-number"
               aria-label="Number of spools to create">
        <small id="add-quantity-hint"></small>
      </label>

    </div>{# /.form-grid #}

    <div class="form-actions">
      <button type="submit" class="button button-primary">Add spool</button>
      <button type="button" class="button button-secondary"
              onclick="this.closest('details').removeAttribute('open')">Cancel</button>
    </div>
  </form>
</details>
```

**Key changes from current:**
- `id` prefixes changed from `color-*` to `add-color-*` for namespace
  clarity (the shared JS initializer uses these IDs).
- Hex text input added below `.color-row` inside the same `<label>`.
- Color Name field added as a new grid cell.
- Remaining field restructured: `name="remaining_pct"` moved to the
  `<input type="number">`; slider is a nameless companion with `id` only.
- Quantity field added with dynamic hint text.
- `{{ p.vendor }} {{ p.name }}` replaced with `{{ plabel(p) }}` (see §7).

---

## 4. Form Layout (Inline Edit Form)

### New layout (6 fields, 3-col grid)

```
Row 1: Profile    | Color          | Location
Row 2: Color Name | Remaining      | Notes
```

Notes moves from `.full-width` to a regular grid cell to fill the 6th
slot. This eliminates the empty gap and keeps the grid balanced.

### Full Edit form HTML (per spool row)

```html
<tr class="spool-edit-row hidden" id="spool-edit-{{ s.id }}">
  <td colspan="5">
    <form method="post" action="/spools/{{ s.id }}/edit" class="spool-edit-form">
      <div class="form-grid three-col">

        {# ── Row 1 ── #}

        {# Profile — includes orphan option with dedup #}
        <label>
          <span>Profile</span>
          <select name="cloud_resource_id" required>
            {% if s.cached_profile and s.cached_profile.deleted_from_cloud %}
            <option value="{{ s.cached_profile.cloud_resource_id }}" selected>
              {{ plabel(s.cached_profile) }} (Orphan)
            </option>
            {% endif %}
            {% for p in profiles %}
            <option value="{{ p.cloud_resource_id }}"
                    {% if p.cloud_resource_id == s.cloud_resource_id %}selected{% endif %}>
              {{ plabel(p) }}
            </option>
            {% endfor %}
          </select>
        </label>

        {# Color #}
        <label class="color-field">
          <span>Color</span>
          <div class="color-row">
            <span class="swatch color-row-swatch"
                  id="edit-color-swatch-{{ s.id }}"
                  style="background:{{ s.color_override or '#cccccc' }}"></span>
            <select name="color_override" id="edit-color-select-{{ s.id }}">
              <option value="{{ s.color_override or '#cccccc' }}"
                      {% if not s.color_override %}selected{% endif %}>
                Profile default
              </option>
              <option value="#ffffff">White</option>
              <option value="#f5f5dc">Beige</option>
              <option value="#d9d9d9">Light Grey</option>
              <option value="#808080">Grey</option>
              <option value="#404040">Dark Grey</option>
              <option value="#1a1a1a">Black</option>
              <option value="#ff0000">Red</option>
              <option value="#c0392b">Dark Red</option>
              <option value="#ff5e4d">Coral</option>
              <option value="#ff8c00">Orange</option>
              <option value="#e67e22">Dark Orange</option>
              <option value="#ffd700">Gold</option>
              <option value="#f1c40f">Yellow</option>
              <option value="#9acd32">Yellow Green</option>
              <option value="#2ecc71">Green</option>
              <option value="#27ae60">Dark Green</option>
              <option value="#00b894">Teal</option>
              <option value="#1abc9c">Cyan</option>
              <option value="#3498db">Blue</option>
              <option value="#2980b9">Dark Blue</option>
              <option value="#2c3e50">Navy</option>
              <option value="#9b59b6">Purple</option>
              <option value="#8e44ad">Dark Purple</option>
              <option value="#e91e63">Magenta</option>
              <option value="#e8b4b8">Pink</option>
              <option value="#964b00">Brown</option>
              <option value="#5d4037">Dark Brown</option>
              <option value="#c0c0c0">Silver</option>
              <option value="#d4af37">Gold Metallic</option>
            </select>
            <input type="color" id="edit-color-custom-{{ s.id }}"
                   value="{{ s.color_override or '#cccccc' }}"
                   title="Custom color picker" class="color-custom-btn"
                   aria-label="Open native color picker">
          </div>
          <input type="text" id="edit-color-hex-{{ s.id }}"
                 value="{{ s.color_override or '#cccccc' }}"
                 placeholder="#RRGGBB" class="hex-input" maxlength="7"
                 inputmode="text" autocomplete="off"
                 aria-label="Hex color code">
        </label>

        {# Location #}
        <label>
          <span>Location</span>
          <input name="location" value="{{ s.location or '' }}"
                 placeholder="e.g. Dry box A">
        </label>

        {# ── Row 2 ── #}

        {# Color Name #}
        <label>
          <span>Color name</span>
          <input type="text" name="color_name"
                 id="edit-color-name-{{ s.id }}"
                 value="{{ s.color_name or '' }}"
                 placeholder="e.g. Vivid Yellow"
                 maxlength="60" autocomplete="off">
        </label>

        {# Remaining: slider + number input #}
        <label class="range-field">
          <span>Remaining <output id="edit-remaining-output-{{ s.id }}">{{ s.remaining_pct }}%</output></span>
          <div class="range-row">
            <input type="range" id="edit-range-{{ s.id }}"
                   min="0" max="100" value="{{ s.remaining_pct }}"
                   aria-label="Remaining material (slider)">
            <input type="number" name="remaining_pct"
                   id="edit-number-{{ s.id }}"
                   min="0" max="100" value="{{ s.remaining_pct }}"
                   class="range-number"
                   aria-label="Remaining material (percent)">
          </div>
        </label>

        {# Notes — moved from .full-width to regular grid cell #}
        <label>
          <span>Notes</span>
          <input name="notes" value="{{ s.notes or '' }}"
                 placeholder="Optional notes">
        </label>

      </div>{# /.form-grid #}

      <div class="form-actions">
        <button type="submit" class="button button-primary">Save</button>
        <button type="button" class="button button-secondary"
                onclick="document.getElementById('spool-edit-{{ s.id }}').classList.add('hidden')">
          Cancel
        </button>
      </div>
    </form>
  </td>
</tr>
```

**Key changes from current:**
- All inline `onchange`/`oninput` handlers removed — replaced by shared
  JS initializer (see §9).
- Hex text input added below `.color-row`.
- Color Name field added.
- Remaining field restructured with slider + number input pair.
- Notes moved from `.full-width` to a regular grid cell.
- `{{ p.vendor }} {{ p.name }}` replaced with `{{ plabel(p) }}`.
- Orphan option uses `{{ plabel(s.cached_profile) }} (Orphan)`.

---

## 5. Feature 1: Hex Code Input

### Interaction Model

Three input channels — preset dropdown, native color picker, and hex
text input — all bidirectionally synced through a single `syncColor()`
function. A fourth element (the swatch) is display-only.

```
                    ┌──────────┐
                    │  Swatch  │  (display only)
                    └────▲─────┘
                         │
           ┌─────────────┼─────────────┐
           │             │             │
    ┌──────┴──────┐ ┌────┴─────┐ ┌─────┴──────┐
    │  Dropdown   │ │  Picker  │ │  Hex Input │
    │  (select)   │ │ (color)  │ │  (text)    │
    └─────────────┘ └──────────┘ └────────────┘
```

### Hex normalization rules

| User input | Normalized | Matches preset? | Dropdown action |
|------------|------------|-----------------|-----------------|
| `#0000ff`  | `#0000ff`  | Check options   | Select match or set "Custom" |
| `0000ff`   | `#0000ff`  | Check options   | Select match or set "Custom" |
| `#00f`     | `#00ffee`  | Check options   | Select match or set "Custom" |
| `00f`      | `#00ffee`  | Check options   | Select match or set "Custom" |
| `#ggg`     | (invalid)  | —               | No update; show error styling |
| `#12345`   | (invalid)  | —               | No update; show error styling |
| (empty)    | (invalid)  | —               | No update |

3-digit hex expansion: each digit doubled (`#abc` → `#aabbcc`).

### Preset matching

After normalizing the hex value, iterate the dropdown `<option>`s
(skipping the first "Custom"/"Profile default" option). If an option's
value equals the normalized hex, select it — this also triggers
`color_name` auto-populate (see §8). If no match, set the first option's
value to the hex, label it "Custom", and select it.

### Error styling

Invalid hex input gets a subtle red border via a CSS class:

```css
.hex-input.invalid {
  border-color: var(--red);
  box-shadow: 0 0 0 3px rgba(255, 90, 101, .1);
}
```

The class is removed as soon as the input becomes valid or empty.

---

## 6. Feature 2: Direct Number Input for "Remaining"

### Layout

The range slider and number input sit side by side in a `.range-row`
flex container:

```
Remaining  100%
[████████████████████] [ 100 ]
```

### Sync rules

| Event | Action |
|-------|--------|
| Slider `input` | Update number input value; update `<output>` text |
| Number `input` | Update slider value; update `<output>` text |
| Number `change` (blur/Enter) | Clamp to 0–100; sync slider + output |

The `<output>` element (already present in the current design) shows the
current percentage with a `%` suffix.

### Name attribute placement

`name="remaining_pct"` is on the `<input type="number">`, not the
slider. Rationale:
- The number input is the precise, always-submittable field.
- The slider is a visual companion (progressive enhancement).
- Without JS, the number input still submits correctly.

### Table inline slider (optional enhancement)

The table row's inline remaining slider (HTMX `hx-post`) can optionally
get a compact number companion. This is **not required** for v1 — the
table slider is a quick-adjust control and adding a number input would
change the `.remaining-cell` grid layout. If desired:

```html
<div class="remaining-cell">
  <div>
    <span id="remaining-{{ s.id }}" class="remaining">{{ s.remaining_pct }}%</span>
    <span class="remaining-label">available</span>
  </div>
  <div class="range-row">
    <input type="range" min="0" max="100" value="{{ s.remaining_pct }}"
           name="remaining_pct"
           hx-post="/spools/{{ s.id }}/remaining"
           hx-trigger="change"
           hx-target="#remaining-{{ s.id }}"
           hx-swap="outerHTML"
           hx-include="this"
           aria-label="Remaining material for spool {{ s.id }}">
  </div>
</div>
```

The table inline slider keeps `name="remaining_pct"` because it uses
`hx-include="this"` (submits only itself). No number companion is added
here to avoid cluttering the table.

---

## 7. Feature 5: Profile Dropdown Vendor Deduplication

### The Bug

Profile `<option>`s render `{{ p.vendor }} {{ p.name }}`, producing
duplicated text when the profile name already starts with the vendor
name:

| vendor | name | Current output | Fixed output |
|--------|------|----------------|--------------|
| Sunlu | Sunlu PLA+ Optimized | Sunlu Sunlu PLA+ Optimized | Sunlu PLA+ Optimized |
| Sunlu | PLA+ Optimized | Sunlu PLA+ Optimized | Sunlu PLA+ Optimized |
| Polymaker | PolyLite PLA | Polymaker PolyLite PLA | Polymaker PolyLite PLA |
| (empty) | Generic PLA |  Generic PLA | Generic PLA |

### Dedup rule

Show `{{ p.name }}` alone when:
1. `p.vendor` is non-empty **and**
2. `p.name.lower()` starts with `p.vendor.lower() + ' '` (vendor
   followed by a space) **or** `p.name.lower()` equals
   `p.vendor.lower()` (name is exactly the vendor).

Otherwise show `{{ p.vendor }} {{ p.name }}`.

The space-delimited check prevents false positives: "Sunlu" does not
match "SunluLite PLA" because `"sunlulite pla".startswith("sunlu ")` is
`False`.

### Jinja2 macro (defined once at template top)

Place this macro immediately after `{% block content %}` and before any
HTML that uses it:

```jinja2
{# ── Profile label macro: deduplicates vendor prefix in profile name ── #}
{% macro plabel(p) -%}
{%- if p.vendor and (p.name.lower().startswith(p.vendor.lower() ~ ' ') or p.name.lower() == p.vendor.lower()) -%}
{{ p.name }}
{%- else -%}
{{ p.vendor }} {{ p.name }}
{%- endif -%}
{%- endmacro %}
```

The `-` whitespace-control markers prevent leading/trailing whitespace
from polluting the option text.

### Application sites

**a) Filter dropdown (`.filters`):**

```jinja2
{# Before #}
<option value="{{ p.cloud_resource_id }}" {% if filter_cloud_resource_id == p.cloud_resource_id %}selected{% endif %}>{{ p.vendor }} {{ p.name }}</option>

{# After #}
<option value="{{ p.cloud_resource_id }}" {% if filter_cloud_resource_id == p.cloud_resource_id %}selected{% endif %}>{{ plabel(p) }}</option>
```

**b) Add Spool profile dropdown:**

```jinja2
{# Before #}
<option value="{{ p.cloud_resource_id }}">{{ p.vendor }} {{ p.name }}</option>

{# After #}
<option value="{{ p.cloud_resource_id }}">{{ plabel(p) }}</option>
```

**c) Edit Spool profile dropdown (including orphan option):**

```jinja2
{# Before — orphan option #}
<option value="{{ s.cached_profile.cloud_resource_id }}" selected>{{ s.cached_profile.vendor }} {{ s.cached_profile.name }} (Orphan)</option>

{# After — orphan option #}
<option value="{{ s.cached_profile.cloud_resource_id }}" selected>{{ plabel(s.cached_profile) }} (Orphan)</option>
```

```jinja2
{# Before — profile loop #}
<option value="{{ p.cloud_resource_id }}" {% if p.cloud_resource_id == s.cloud_resource_id %}selected{% endif %}>{{ p.vendor }} {{ p.name }}</option>

{# After — profile loop #}
<option value="{{ p.cloud_resource_id }}" {% if p.cloud_resource_id == s.cloud_resource_id %}selected{% endif %}>{{ plabel(p) }}</option>
```

---

## 8. Feature 4: Color Name Attribute

### Auto-populate behavior

When a named preset is selected from the dropdown (any option except the
first "Custom"/"Profile default"), the `color_name` field is
auto-populated with that option's text content — **but only if the user
has not manually typed a custom value**.

Detection uses a `nameModified` boolean flag:

| State | `nameModified` | Dropdown selection | Action |
|-------|---------------|-------------------|--------|
| Fresh form | `false` | "Coral" | `color_name` = "Coral" |
| After auto-populate | `false` | "Blue" | `color_name` = "Blue" |
| User types "Fire Red" | `true` | "Blue" | `color_name` stays "Fire Red" |
| User clears field | `false` | "Blue" | `color_name` = "Blue" |

Rules:
- `color_name` `input` event: if value is non-empty → `nameModified = true`; if empty → `nameModified = false`.
- Dropdown `change` event: if `!nameModified` and selected index > 0 → set `color_name` to option text.
- Hex input and color picker do **not** trigger auto-populate (only dropdown selection does).
- If hex input matches a preset and programmatically selects that dropdown option, the `change` event fires and auto-populate runs (desired — the user effectively chose that preset by typing its hex).

### Edit form initialization

For edit forms, `nameModified` is initialized to `true` if the spool
already has a `color_name` value (treat existing values as user-set).
If `color_name` is empty, `nameModified = false` (auto-populate will
work on dropdown change).

### Table display (Active Spools "Color" column)

**Current:**
```html
<td>
  {% if s.color_override %}
  <span class="color-value">
    <span class="swatch" style="background:{{ s.color_override }}"></span>
    {{ s.color_override }}
  </span>
  {% else %}
  <span class="muted">Profile default</span>
  {% endif %}
</td>
```

**New:**
```html
<td>
  {% if s.color_override %}
  <span class="color-value">
    <span class="swatch" style="background:{{ s.color_override }}"></span>
    <span class="color-info">
      <span class="color-name-text">{{ s.color_name or s.color_override }}</span>
      {% if s.color_name %}<span class="color-hex-sub">{{ s.color_override }}</span>{% endif %}
    </span>
  </span>
  {% else %}
  <span class="muted">Profile default</span>
  {% endif %}
</td>
```

Display logic:
- If `color_name` is set: show swatch + color_name (bold) + hex (muted subtitle).
- If `color_name` is empty but `color_override` is set: show swatch + hex (bold, no subtitle) — same as current behavior.
- If no `color_override`: show "Profile default" — unchanged.

### Visual layout of the color cell

```
●  Vivid Yellow
   #f1c40f
```

or (no color_name):

```
●  #f1c40f
```

---

## 9. JavaScript Event Handlers

### Shared initialization functions

Replace the current inline `onchange`/`oninput` handlers and the
add-form-only `<script>` block with two shared functions. These are
called once on page load for the add form and for each edit form.

```javascript
<script>
(function() {
  'use strict';

  // ── Hex normalization ──
  function normalizeHex(raw) {
    var v = String(raw || '').replace(/^#/, '').trim();
    if (/^[0-9a-fA-F]{3}$/.test(v)) {
      v = v[0] + v[0] + v[1] + v[1] + v[2] + v[2];
    }
    if (/^[0-9a-fA-F]{6}$/.test(v)) {
      return '#' + v.toLowerCase();
    }
    return null;
  }

  // ── Color field initializer ──
  // Works for both Add form (prefix='add-') and Edit forms (prefix='edit-<id>-').
  function initColorField(prefix, initialColorName) {
    var sel     = document.getElementById(prefix + 'color-select');
    var swatch  = document.getElementById(prefix + 'color-swatch');
    var picker  = document.getElementById(prefix + 'color-custom');
    var hex     = document.getElementById(prefix + 'color-hex');
    var nameInp = document.getElementById(prefix + 'color-name');
    if (!sel || !swatch) return;

    var nameModified = !!(initialColorName && initialColorName.length > 0);

    // Sync all visual elements from the dropdown's current value.
    function syncFromDropdown() {
      var val = sel.value;
      swatch.style.background = val;
      if (picker) picker.value = val;
      if (hex) {
        hex.value = val;
        hex.classList.remove('invalid');
      }
    }

    // Try to match a hex value to a preset option; if no match, set "Custom".
    function syncFromHex(hexVal) {
      // Find a matching preset option (skip index 0 = Custom/Profile default)
      for (var i = 1; i < sel.options.length; i++) {
        if (sel.options[i].value === hexVal) {
          sel.selectedIndex = i;
          syncFromDropdown();
          tryAutoPopulate(sel.options[i].text);
          return;
        }
      }
      // No preset match → set first option to Custom with this hex
      sel.options[0].value = hexVal;
      sel.options[0].text = 'Custom';
      sel.selectedIndex = 0;
      swatch.style.background = hexVal;
      if (picker) picker.value = hexVal;
      // Do NOT auto-populate color_name for custom hex
    }

    // Auto-populate color_name if user hasn't typed a custom value.
    function tryAutoPopulate(presetName) {
      if (nameInp && !nameModified) {
        nameInp.value = presetName;
      }
    }

    // ── Dropdown change → sync everything + auto-populate name ──
    sel.addEventListener('change', function() {
      syncFromDropdown();
      if (sel.selectedIndex > 0) {
        tryAutoPopulate(sel.options[sel.selectedIndex].text);
      }
    });

    // ── Native color picker input → sync hex + dropdown ──
    if (picker) {
      picker.addEventListener('input', function() {
        var normalized = normalizeHex(picker.value);
        if (normalized) {
          hex.value = normalized;
          hex.classList.remove('invalid');
          syncFromHex(normalized);
        }
      });
    }

    // ── Hex text input → normalize + sync ──
    if (hex) {
      hex.addEventListener('input', function() {
        var normalized = normalizeHex(hex.value);
        if (normalized) {
          hex.classList.remove('invalid');
          syncFromHex(normalized);
        } else if (hex.value.trim() === '') {
          hex.classList.remove('invalid');
          // Don't update swatch/dropdown on empty input
        } else {
          hex.classList.add('invalid');
        }
      });

      // On blur, normalize the displayed value (add # prefix, lowercase)
      hex.addEventListener('blur', function() {
        var normalized = normalizeHex(hex.value);
        if (normalized) {
          hex.value = normalized;
          hex.classList.remove('invalid');
        }
      });
    }

    // ── Color name input → track user modification ──
    if (nameInp) {
      nameInp.addEventListener('input', function() {
        nameModified = nameInp.value.trim().length > 0;
      });
    }

    // Initialize display from current dropdown value
    syncFromDropdown();
  }

  // ── Range + number sync initializer ──
  function initRangeField(rangeId, numberId, outputId) {
    var range  = document.getElementById(rangeId);
    var number = document.getElementById(numberId);
    var output = document.getElementById(outputId);
    if (!range || !number) return;

    function updateOutput(val) {
      if (output) output.textContent = val + '%';
    }

    range.addEventListener('input', function() {
      number.value = range.value;
      updateOutput(range.value);
    });

    number.addEventListener('input', function() {
      var v = parseInt(number.value, 10);
      if (isNaN(v)) return;
      v = Math.max(0, Math.min(100, v));
      range.value = v;
      updateOutput(v);
    });

    number.addEventListener('change', function() {
      var v = parseInt(number.value, 10);
      if (isNaN(v)) v = 100;
      v = Math.max(0, Math.min(100, v));
      number.value = v;
      range.value = v;
      updateOutput(v);
    });
  }

  // ── Quantity hint ──
  function initQuantityHint(qtyId, hintId) {
    var qty  = document.getElementById(qtyId);
    var hint = document.getElementById(hintId);
    if (!qty || !hint) return;
    function update() {
      var n = parseInt(qty.value, 10) || 1;
      hint.textContent = n > 1 ? 'Creates ' + n + ' identical spools' : '';
    }
    qty.addEventListener('input', update);
    qty.addEventListener('change', update);
    update();
  }

  // ── Initialize Add form ──
  initColorField('add-', '');
  initRangeField('add-range', 'add-number', 'add-remaining-output');
  initQuantityHint('add-quantity', 'add-quantity-hint');

  // ── Initialize Edit forms (one per spool row) ──
  {% for s in spools %}
  initColorField('edit-{{ s.id }}-', '{{ s.color_name or '' }}');
  initRangeField('edit-range-{{ s.id }}', 'edit-number-{{ s.id }}', 'edit-remaining-output-{{ s.id }}');
  {% endfor %}

})();
</script>
```

### Notes on the JS approach

- **Single IIFE** wraps everything — no global namespace pollution.
- **`initColorField(prefix, initialColorName)`** handles both add and
  edit forms via a prefix string. The add form uses `'add-'`; edit forms
  use `'edit-<id>-'`.
- **`initRangeField(rangeId, numberId, outputId)`** syncs slider ↔ number
  ↔ output. Called for both add and edit forms.
- **`initQuantityHint`** dynamically shows "Creates N identical spools"
  when quantity > 1.
- The Jinja2 `{% for s in spools %}` loop inside the `<script>` emits
  one `initColorField` + `initRangeField` call per spool. This is the
  same pattern the template already uses to generate edit rows.
- All inline `onchange`/`oninput` handlers from the current edit form
  are removed — the initializer handles everything.

---

## 10. CSS Additions

Add these rules to `fm/api/static/app.css`, after the existing
`.color-field` / `.color-row` block (around line 135):

```css
/* ──────────────────────────────────────────────
   Spool forms — hex input, range-number pair, color info
   ────────────────────────────────────────────── */

/* Hex text input — sits below the .color-row inside .color-field */
.hex-input {
  margin-top: 8px;
  padding: 8px 12px;
  font-family: 'Manrope', monospace;
  font-size: 12px;
  letter-spacing: .04em;
  text-transform: uppercase;
}
.hex-input.invalid {
  border-color: var(--red);
  box-shadow: 0 0 0 3px rgba(255, 90, 101, .1);
}

/* Range + number input pair */
.range-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.range-row input[type='range'] {
  flex: 1;
  min-width: 0;
}
.range-number {
  width: 64px;
  min-width: 64px;
  flex: 0 0 auto;
  text-align: center;
  -moz-appearance: textfield;
}
.range-number::-webkit-outer-spin-button,
.range-number::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}

/* Color info in table cells — name + hex subtitle */
.color-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.color-name-text {
  color: var(--text);
  font-weight: 600;
  font-size: 12px;
}
.color-hex-sub {
  color: var(--text-muted);
  font-size: 10px;
  font-family: 'Manrope', monospace;
  letter-spacing: .03em;
}

/* Quantity hint text */
#add-quantity-hint {
  margin-top: 6px;
  color: var(--accent);
  font-size: 9px;
  font-weight: 600;
}
#add-quantity-hint:empty {
  display: none;
  margin-top: 0;
}
```

### Responsive adjustments

The existing media queries at `@media (max-width: 760px)` already
collapse `.form-grid.three-col` to `1fr` (single column). No additional
responsive rules are needed — all new fields are standard inputs that
flow naturally into the single-column layout.

The `.range-row` flex container will stack gracefully on narrow screens
because `input[type='range']` has `flex: 1` and the number input has a
fixed `64px` width — they remain side by side even in single-column
mode, which is the desired behavior (the slider shrinks, the number
input stays compact).

---

## 11. Backend Implications

> **This is a UI design spec.** The following notes are for the
> implementer — they are NOT part of this spec's deliverable but are
> called out to prevent integration surprises.

### `color_name` storage (AGENTS.md Rule 6 constraint)

The Spool model has no `color_name` column, and Rule 6 prohibits model
migrations. Options for the implementer:

1. **`notes` field convention**: Store `color_name` in the existing
   `notes` VARCHAR using a structured prefix, e.g.
   `color_name:Vivid Yellow|User notes here`. The UI sends
   `color_name` as a separate form field; the backend encodes/decodes
   it into `notes`. This is the least-invasive approach but conflates
   two concepts in one column.

2. **`color_override` composite**: Encode as
   `#ff0000:Vivid Yellow` in `color_override`. The backend parses the
   hex and name on read, splits on `:`. This keeps it in one existing
   column but changes the `color_override` format (must update all
   readers, including the table display and printer DB sync).

3. **Relax Rule 6**: If the project owner agrees, add a
   `color_name: Mapped[str | None]` column to the Spool model with a
   migration. This is the cleanest long-term solution.

**Recommendation**: Option 1 (`notes` convention) for v1, with a TODO
to migrate to a dedicated column when Rule 6 is relaxed. The UI
spec treats `color_name` as a first-class field regardless of storage.

### `quantity` parameter

The `spool_create_form` route handler needs a new parameter:

```python
quantity: int = Form(1),
```

When `quantity > 1`, create N identical `Spool` objects in a loop
(same `cloud_resource_id`, `color_override`, `remaining_pct`,
`location`, `notes`, `color_name`). Clamp to 1–50. The edit route
does NOT accept `quantity` (editing a single spool's quantity is
meaningless).

### "Profile default" option value bug (existing)

The edit form's "Profile default" option has
`value="{{ s.color_override or '#cccccc' }}"`. When selected and
submitted, the backend receives a hex string (truthy) and sets
`color_override` to that hex — changing the spool from "no override"
to "override with #cccccc". The fix is to use `value=""` for the
"Profile default" option so the backend's `color_override or None`
correctly clears the override. This is a backend/template fix, not
a UI design decision, but the spec notes it for the implementer.

---

## 12. Accessibility

| Element | Label | ARIA | Keyboard |
|---------|-------|------|----------|
| Hex text input | `aria-label="Hex color code"` | `inputmode="text"`, `autocomplete="off"`, `maxlength="7"` | Type to edit; Enter/blur normalizes |
| Native color picker | `aria-label="Open native color picker"` | `title="Custom color picker"` | Space/Enter opens OS picker |
| Color name input | `<span>Color name</span>` label | `maxlength="60"`, `autocomplete="off"` | Standard text input |
| Range slider | `aria-label="Remaining material (slider)"` | `min`, `max` set | Arrow keys adjust by 1; PageUp/Down by 10 |
| Number input (remaining) | `aria-label="Remaining material (percent)"` | `min="0"`, `max="100"` | Type or spinner arrows |
| Number input (quantity) | `aria-label="Number of spools to create"` | `min="1"`, `max="50"` | Type or spinner arrows |
| Quantity hint | `<small id="add-quantity-hint">` | `:empty` hides via CSS | Non-interactive; informational only |
| Color cell (table) | — | `.color-info` is semantic text | Non-interactive; read by screen readers |

All inputs have visible `<span>` labels (the existing `label > span`
pattern) or `aria-label` attributes where a visible label would be
redundant (hex input, color picker button). Focus rings use the
existing `:focus-visible { outline: 2px solid var(--accent); }` rule.

The `.sr-only` class (already defined in `app.css`) is available if
any additional screen-reader-only labels are needed.

---

## 13. Implementation Checklist

- [ ] Add `{% macro plabel(p) %}` at top of `spools.html` template
- [ ] Replace `{{ p.vendor }} {{ p.name }}` with `{{ plabel(p) }}` in all 4 locations (filter, add, edit loop, edit orphan)
- [ ] Add hex `<input type="text">` to Add form color field
- [ ] Add hex `<input type="text">` to Edit form color field (per spool)
- [ ] Add `color_name` `<input type="text">` to Add form (row 2, col 1)
- [ ] Add `color_name` `<input type="text">` to Edit form (row 2, col 1)
- [ ] Restructure Remaining field: add `.range-row` with slider + number input in Add form
- [ ] Restructure Remaining field: add `.range-row` with slider + number input in Edit form
- [ ] Move `name="remaining_pct"` from slider to number input in both forms
- [ ] Add Quantity `<input type="number">` to Add form (row 2, col 3)
- [ ] Move Notes from `.full-width` to regular grid cell in Edit form
- [ ] Update Color column in Active Spools table to show `color_name` + hex subtitle
- [ ] Remove all inline `onchange`/`oninput` handlers from edit forms
- [ ] Replace `<script>` block with shared `initColorField` + `initRangeField` + `initQuantityHint` functions
- [ ] Add CSS rules (`.hex-input`, `.range-row`, `.range-number`, `.color-info`, `.color-name-text`, `.color-hex-sub`, `#add-quantity-hint`)
- [ ] Backend: add `quantity` parameter to `spool_create_form` route
- [ ] Backend: add `color_name` storage strategy (see §11)
- [ ] Backend: fix "Profile default" option `value=""` in edit form
- [ ] Verify: run `scripts/dev-start.sh`, test add/edit/color sync/quantity in browser
- [ ] Verify: run `scripts/test.sh`
