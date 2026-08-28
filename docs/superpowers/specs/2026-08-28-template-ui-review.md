# Template Selection UI/UX Review

## Summary recommendation

Keep template selection in **01 Identity**, but treat it as a visually distinct, full-width “Preset source” group inside that section. Use two native selects—an optional vendor filter followed by the template choice—rather than a custom searchable control. Remove live brand-to-vendor auto-selection. Represent the current behavior with an explicit first option, **“Use Generic PETG defaults (recommended)”**, whose submitted value is empty and therefore persists as `template_override = null`.

This approach fits the existing server-rendered/HTMX architecture, keeps keyboard and screen-reader behavior native, and avoids adding a complex combobox for a list that is already reduced first by material type and then optionally by vendor.

## Answers to the seven design questions

### 1. Placement: Identity or a separate section?

**Keep it in 01 Identity.** A template is the source preset from which the profile is derived; it is closely tied to material type and brand and must be chosen before the read-only Orca values in section 03 make sense. A fifth top-level section would give one control disproportionate weight and lengthen an already clear four-step form.

It should not, however, look like another unrelated identity field. Place a full-width `template-picker` group after the Brand/Name/Type/Colors row and before Notes. Give it a short heading such as **Preset source** and explanatory text. This establishes the dependency `Type → Vendor filter → Template` without changing the form’s section model.

### 2. Two dropdowns or one searchable dropdown?

**Use two native dropdowns for this release.** After the automatic type filter, each list is much smaller than the 356-template total. The vendor select gives users a familiar facet and the template select remains directly usable with keyboard type-ahead. Native controls also preserve the app’s current visual and accessibility patterns.

A single searchable combobox would only be preferable if real usage shows that per-type lists remain too long after filtering. A production-quality combobox requires focus management, listbox keyboard semantics, active-descendant handling, result counts, empty states, and client-side behavior not currently present in the app. It is not justified by the current inventory and conflicts with the HTMX-first constraint.

Label vendor clearly as **Filter by vendor** and make **All vendors** the first option. Vendor is a filter, not persisted profile data.

### 3. Brand-to-vendor auto-suggestion

**Remove automatic brand-to-vendor selection from the initial design.** Changing another visible control while the user types is surprising, creates repeated requests, can collapse the template list mid-entry, and becomes ambiguous for partial or noncanonical brand names. It also risks replacing a deliberate vendor choice.

Brand and template vendor are related but not identical: a house brand may intentionally use a Generic or another compatible preset. Keep them independent. If assistance is desired later, use a non-destructive suggestion after blur—“Templates from Bambu Lab are available”—with an explicit **Filter** action. Do not auto-select or cascade from every input event.

### 4. Handling the Generic default

Use a separate, explicit first choice:

> **Use Generic PETG defaults (recommended)**

Its value is `""`, mapping to `template_override = null`. Do not make a concrete `Generic PETG` template look like a user override if null already means the type-based generic behavior. The wording communicates both the behavior and the safe default, survives a missing/rebuilt template index, and makes edit-state interpretation unambiguous.

Then list selectable templates below it. If the index contains other genuinely distinct Generic presets, put them in a **Generic** optgroup after the default. Do not duplicate the same type-level base as both “Use default” and “Generic PETG.” The default option remains visible even when a non-Generic vendor filter is active, so users can always return to inherited behavior.

### 5. Preview of key values

**Yes—show a compact, read-only preview after a specific template is selected.** Template names alone do not explain the practical consequence of the choice. Show the values most useful for comparison and most likely to differ:

- nozzle temperature (or initial/default range, according to available data),
- flow ratio,
- fan range,
- maximum volumetric speed.

Use four inline definition-list items, not another card and not all twelve fields from section 03. Add the note **“Base values; profile settings may override them.”** For “Use Generic … defaults,” either show the same four generic values or state “Uses the standard PETG base preset” if the endpoint cannot cheaply resolve them.

The preview is selection guidance. Section 03 remains the authoritative display of the profile’s synced Orca Cloud values; its heading should continue to show the actual selected template name as required by section 10. Avoid calling the preview “Current print settings,” which would blur that distinction.

### 6. Changing type after selecting a template

Treat a type change as an intentional reset:

1. Reload vendors and templates for the new type.
2. Reset vendor to **All vendors**.
3. Reset template to **Use Generic `<new type>` defaults** (`value=""`).
4. Replace the preview with the new generic preview/description.
5. Announce the reset in the fragment’s live status: **“Template reset to Generic PLA defaults because material type changed.”**

Never retain a hidden or incompatible override. This should apply immediately in the UI and be enforced server-side on submission/builder lookup. On a validation error unrelated to type, preserve the submitted compatible template rather than resetting it.

### 7. Accessibility concerns with HTMX updates

The design is accessible if the fragment follows these requirements:

- Every control has a persistent, unique `id` and an explicit `<label for>`; do not rely only on visual proximity or placeholder text.
- Keep the fragment wrapper in the DOM and swap its **inner HTML**, so the target and live-region identity remain stable.
- Add a concise `role="status" aria-live="polite" aria-atomic="true"` message for loaded result counts, type-driven resets, errors, and preview updates. Do not put the entire form fragment in a live region.
- Use `aria-describedby` to associate the template help text and preview note with the template select.
- Preserve focus after vendor/template requests. Because `innerHTML` replacement destroys the focused select, set `hx-swap="innerHTML focus-scroll:false"` and return an element with the same `id`; verify focus restoration. Prefer swapping only the dependent region for vendor changes if reliable focus preservation is not available.
- Expose loading without disabling the whole form: use an `aria-hidden="true"` spinner plus status text such as “Updating templates…”. Apply `aria-busy="true"` to the target during requests (via HTMX request lifecycle support). Do not encode state by color alone.
- On request failure, retain or restore usable controls and render a text error with a **Retry** button. Do not leave an empty select.
- Native `<select>` and `<optgroup label>` semantics should be retained. Do not create a faux searchable dropdown without implementing the complete ARIA combobox pattern.
- Server responses must preserve the currently selected compatible value on edits and after validation errors.

## Recommended HTML structure

The existing Type select remains in the main `.form-grid` and supplies its value using `hx-include`. Insert the following full-width group after the row containing Type/Colors and before Notes. The fragment endpoint renders the contents of `#template-picker-fields`; the example shows the initial server-rendered state.

```html
<div class="template-picker full-width"
     aria-labelledby="template-picker-title">
  <div class="template-picker-heading">
    <div>
      <h3 id="template-picker-title">Preset source</h3>
      <p id="template-picker-help">
        Choose the OrcaSlicer base tuning used when generating this profile.
      </p>
    </div>
    <span class="template-picker-indicator htmx-indicator" aria-hidden="true"></span>
  </div>

  <div id="template-picker-fields"
       class="template-picker-fields"
       aria-busy="false">
    <label for="template-vendor">
      <span>Filter by vendor</span>
      <select id="template-vendor"
              name="template_vendor"
              hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
              hx-trigger="change"
              hx-target="#template-picker-fields"
              hx-swap="innerHTML focus-scroll:false"
              hx-include="[name='type'], this, [name='template_override']"
              hx-indicator="closest .template-picker">
        <option value="">All vendors</option>
        {% for vendor in vendors %}
        <option value="{{ vendor }}" {% if vendor == selected_vendor %}selected{% endif %}>
          {{ vendor }}
        </option>
        {% endfor %}
      </select>
      <small>Optional filter; it is not saved with the profile.</small>
    </label>

    <label for="template-override">
      <span>Template</span>
      <select id="template-override"
              name="template_override"
              aria-describedby="template-picker-help template-preview-note"
              hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
              hx-trigger="change"
              hx-target="#template-picker-fields"
              hx-swap="innerHTML focus-scroll:false"
              hx-include="[name='type'], [name='template_vendor'], this"
              hx-indicator="closest .template-picker">
        <option value="">Use Generic {{ selected_type }} defaults (recommended)</option>
        {% for group in template_groups %}
        <optgroup label="{{ group.vendor }}">
          {% for template in group.templates %}
          <option value="{{ template.name }}"
                  {% if template.name == selected_template %}selected{% endif %}>
            {{ template.display_name }}
          </option>
          {% endfor %}
        </optgroup>
        {% endfor %}
      </select>
    </label>

    <div class="template-preview full-width" aria-labelledby="template-preview-title">
      <h4 id="template-preview-title">Template preview</h4>
      <dl class="template-preview-values">
        <div><dt>Nozzle</dt><dd>245 °C</dd></div>
        <div><dt>Flow ratio</dt><dd>0.95</dd></div>
        <div><dt>Fan</dt><dd>10–30%</dd></div>
        <div><dt>Max volumetric</dt><dd>12 mm³/s</dd></div>
      </dl>
      <p id="template-preview-note">Base values; profile settings may override them.</p>
    </div>

    <p class="template-picker-status sr-only"
       role="status"
       aria-live="polite"
       aria-atomic="true">
      {{ status_message }}
    </p>
  </div>
</div>
```

The Type control should be updated separately:

```html
<label for="profile-type">
  <span>Type</span>
  <select id="profile-type"
          name="type"
          required
          hx-get="/profiles/{{ pid_or_new }}/template-dropdown"
          hx-trigger="change"
          hx-target="#template-picker-fields"
          hx-swap="innerHTML focus-scroll:false"
          hx-include="this"
          hx-vals='{"reason":"type-change"}'
          hx-indicator=".template-picker-indicator">
    <!-- existing options -->
  </select>
</label>
```

`pid_or_new` should be a route shape supported for both create and edit forms (for example `/profiles/new/template-dropdown` and `/profiles/{pid}/template-dropdown`), or the fragment route should not require a persisted profile ID. A new profile has no `pid`, so section 7’s literal `/profiles/{pid}/…` endpoint is incomplete for that case.

## Required changes to spec section 7

Replace or amend section 7 as follows:

1. **Placement:** Keep the picker in Identity, but specify a full-width “Preset source” subgroup after Type/Colors and before Notes.
2. **Vendor behavior:** Define vendor as an optional, UI-only filter with **All vendors** selected initially. Remove brand input requests and automatic vendor selection.
3. **Default semantics:** Replace “Generic `<type>` is the first option” with an explicit empty-valued option, **Use Generic `<type>` defaults (recommended)**. State that the same type-level base must not be listed twice.
4. **Grouping and labels:** Always use `<optgroup label="Vendor">`. When a vendor is selected, removing a redundant vendor prefix from option text is acceptable only when the prefix match is exact and the resulting label is nonempty; the submitted value remains the canonical full template name. Otherwise show the full name. Do not derive display labels with a loose string replacement.
5. **Preview:** Add the compact four-value preview and define it as base-template guidance, distinct from synced values in section 03.
6. **Type changes:** Make reset behavior normative: type change clears `template_override`, clears the vendor filter, loads the new generic state, and announces the reset. The endpoint must distinguish a type-change request from an ordinary validation re-render so compatible submitted state is not lost.
7. **Create route:** Define a fragment URL that works before a profile exists. Do not require `{pid}` for new-profile filtering unless a dedicated `new` route is provided.
8. **Response format:** The profile form should call an HTML fragment endpoint, not the JSON `/api/templates` endpoint directly. Keep `/api/templates` for API consumers; browsers receive rendered `<option>`/preview markup.
9. **Loading, empty, and error states:** Specify them explicitly:
   - loading: keep current controls visible and mark the fragment busy;
   - no matching vendor templates: keep the Generic default and state “No vendor-specific templates for this filter”;
   - endpoint error: keep a usable Generic default, show an inline error, and offer Retry.
10. **Edit/validation state:** The endpoint must accept and preserve `template_override` when it remains valid for the selected type; incompatible or missing overrides must render a warning and select the generic default.
11. **Accessibility:** Add stable IDs, explicit labels, `aria-describedby`, a dedicated polite status region, focus preservation, and busy/error semantics.
12. **Section 10 wording:** Show **“Template: Generic PETG defaults”** for null overrides, not merely a filename-like system template name. For overrides, show the human-readable canonical template name. This keeps the picker and Orca section language consistent.

## CSS additions

These additions use only existing color, spacing, radius, and shadow tokens. The preview is deliberately a flattened inset region, not a nested card.

```css
.template-picker {
  display: grid;
  gap: var(--space-4);
  padding-top: var(--space-5);
  border-top: 1px solid var(--border);
}

.template-picker-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.template-picker-heading h3,
.template-preview h4 {
  margin: 0;
  font-family: var(--font-heading);
}

.template-picker-heading h3 {
  font-size: 14px;
  line-height: 1.3;
}

.template-picker-heading p {
  margin: var(--space-1) 0 0;
  font-size: 11px;
}

.template-picker-fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-4) var(--space-5);
}

.template-picker-indicator {
  width: 18px;
  height: 18px;
  flex: 0 0 auto;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: tc-spin .7s linear infinite;
}

.template-preview {
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-input);
  background: var(--surface-alt);
}

.template-preview h4 {
  margin-bottom: var(--space-3);
  color: var(--text-secondary);
  font-size: 11px;
}

.template-preview-values {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-4);
  margin: 0;
}

.template-preview-values div {
  min-width: 0;
}

.template-preview-values dt {
  color: var(--text-muted);
  font-size: 9px;
  font-weight: 600;
}

.template-preview-values dd {
  margin: var(--space-1) 0 0;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  overflow-wrap: anywhere;
}

.template-preview > p {
  margin: var(--space-3) 0 0;
  color: var(--text-muted);
  font-size: 9px;
}

.template-picker[aria-busy='true'] .template-picker-fields,
.template-picker-fields[aria-busy='true'] {
  opacity: .7;
}

.template-picker-error {
  grid-column: 1 / -1;
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid rgba(255, 90, 101, .25);
  border-radius: var(--radius-input);
  background: rgba(255, 90, 101, .08);
  color: #ff9ba2;
  font-size: 11px;
}

@media (max-width: 760px) {
  .template-picker-fields,
  .template-preview-values {
    grid-template-columns: 1fr;
  }
}
```

Implementation note: the existing `.htmx-indicator` behavior should control visibility if it is already defined globally. If it is not, add the standard opacity/display rule once rather than hiding the indicator with a new one-off mechanism. The literal error colors above match the existing alert palette; preferably promote these repeated values to semantic error tokens before using them in both places.

## HTMX interaction flow

### Initial render

- The server renders Type plus the initial `#template-picker-fields`; no request is necessary merely to make the form usable.
- New profile: use the initial Type value, vendor `""`, template `""`, and generic preview/description.
- Edit profile: derive the template’s vendor for display, but keep **All vendors** unless deliberately choosing to show the saved template’s vendor as the active filter. In either case, the saved template must remain visible and selected.

### Type change

- **Trigger:** `#profile-type` on `change`.
- **Request:** `GET /profiles/new/template-dropdown?type=PLA&reason=type-change` or the edit equivalent.
- **Server behavior:** ignore the previous vendor/template values; return vendors for PLA, the empty Generic PLA default selected, generic preview, and a reset status message.
- **Swap:** `innerHTML` of `#template-picker-fields`.
- **Focus:** restore focus to `#profile-type` or leave it there; announce the reset via the returned status region.

### Vendor change

- **Trigger:** `#template-vendor` on `change`.
- **Request:** fragment endpoint with `type`, `template_vendor`, and current `template_override`.
- **Server behavior:** return the same vendor select state and a filtered template list. Preserve the current template only if it is present under that filter; otherwise select the Generic default and announce that the previous template was cleared by the filter.
- **Swap:** `innerHTML` of `#template-picker-fields`. If focus restoration proves unreliable, split the fragment so this request targets only a `#template-choice-region` containing Template, Preview, and Status; that is the more robust accessibility fallback.

### Template change

- **Trigger:** `#template-override` on `change`.
- **Request:** fragment endpoint with `type`, `template_vendor`, and selected `template_override`.
- **Server behavior:** validate that the selected template belongs to the selected type, resolve the preview values, and return the controls with the selection preserved.
- **Swap:** `innerHTML` of `#template-picker-fields` (or only `#template-preview-region` if split targets are implemented).
- **Announcement:** “Bambu PETG Translucent selected. Preview updated.”

### Brand input

- **No HTMX request.** Brand remains an ordinary required text input. This removes the 300 ms request stream and avoids unexpected control changes.

### Failure and empty results

- Listen for HTMX response errors at the fragment boundary using the project’s existing HTMX error mechanism. Render an inline `.template-picker-error` and Retry control while retaining the previous valid selection when possible.
- A valid type with no indexed vendor-specific templates is not an error: return the Generic default, disable or omit the vendor filter as appropriate, and state that no additional templates are available.

### Form submission and section 03

- Submit only `template_override` as persisted template state; `template_vendor` may be submitted but is ignored by persistence.
- Empty `template_override` becomes null.
- Server-side validation rejects or clears a template that does not belong to the submitted Type; the builder fallback remains defense in depth, not the primary UX.
- After save/sync, section 03 displays the canonical selected template name, or **Generic `<type>` defaults** for null, alongside the existing Orca Cloud source label.
