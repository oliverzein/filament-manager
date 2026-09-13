# Orca Cloud Preset Visual Diff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only "Compare" feature to the Sync Hub that diffs any two Orca Cloud presets side by side — effective (inheritance-flattened) values, categorized, with per-field status (diff/same/only_a/only_b) — plus a `@base` special case comparing a preset against its resolved base template.

**Architecture:** A new service module `fm/service/preset_diff.py` fetches all presets via one read-only `sync_pull`, converts them with the existing `_cloud_presets_to_local`, flattens each side's `inherits` chain via `flatten_preset` against bundled templates, and produces a categorized field-diff dict. A new HTMX endpoint `POST /sync/compare-modal` in `fm/api/ui.py` accepts `preset_a_id` + optional `preset_b_id` and renders `_preset_diff_modal.html` (diff, error, or pick-B state) into a dedicated `#diff-modal` overlay on the sync page.

**Tech Stack:** FastAPI, Jinja2, HTMX 1.9.x, SQLAlchemy (read-only), pytest + TestClient, `fm/presets/flattener.py`, `fm/orca_cloud.py`.

**Spec:** `docs/superpowers/specs/2026-09-13-preset-visual-diff.md`

## Global Constraints

- Orca Cloud is sole source: read-only pull via `sync_pull`; NO profile editing or write-back in this feature.
- No model migrations: do not add columns to Profile or Spool.
- UI styling must integrate seamlessly with existing design system (DM Sans, Manrope, dark palette, CSS variables in `fm/api/static/app.css`).
- Must pass `scripts/test.sh` cleanly with zero regressions.
- Test seam: `build_preset_diff` must call `sync_handlers._orca_client_factory()` via the module attribute (not a `from`-imported alias) so existing monkeypatch patterns keep working.

---

### Task 1: Core diff engine

**Files:**
- Create: `fm/service/preset_diff.py`
- Test: `tests/test_preset_diff.py`

**Interfaces:**
- Consumes: `fm.presets.flattener.flatten_preset(path)`, `fm.presets.flattener.find_preset_by_name(name, hint_dir=None)`; bundled templates in `fm/presets/templates/` (e.g. `eSUN PLA+ @System` → `eSUN PLA+ @base` → `fdm_filament_pla`, where `filament_flow_ratio` resolves to `0.98`, `filament_vendor` to `eSUN`, `filament_density` to `1.25`).
- Produces:
  - `diff_presets(preset_a: dict, preset_b: dict, flatten: bool = True) -> dict` returning `{"name_a": str, "name_b": str, "summary": {"diff": int, "same": int, "only_a": int, "only_b": int}, "categories": [{"name": str, "fields": [{"key": str, "label": str, "status": str, "value_a": str, "value_b": str, "multiline": bool}]}]}` where `status` is one of `diff|same|only_a|only_b`.
  - `_values_equal(a: object, b: object) -> bool` — normalized comparison with numeric coercion.
  - `_normalize(value: object) -> str` — unwraps single-element lists, `None`→`""`, bool→`"1"`/`"0"`, else `str(v).strip()`.
  - `_flatten_dict(preset: dict) -> dict` — `flatten_preset` temp-file round-trip (also used by Task 2 for `@base`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_preset_diff.py`:

```python
# tests/test_preset_diff.py
"""Preset diff engine tests — normalization, categories, A-vs-B comparison."""
from __future__ import annotations

import pytest

from fm.service import preset_diff


def _field(diff: dict, key: str) -> dict:
    for cat in diff["categories"]:
        for f in cat["fields"]:
            if f["key"] == key:
                return f
    raise AssertionError(f"field {key} not in diff")


class TestValuesEqual:
    def test_numeric_coercion(self):
        assert preset_diff._values_equal(["21"], "21.0")
        assert preset_diff._values_equal("0.98", ["0.98"])

    def test_string_inequality(self):
        assert not preset_diff._values_equal(["PLA"], "PETG")
        assert not preset_diff._values_equal("", "x")


class TestDiffPresets:
    def test_diff_same_only_statuses(self):
        a = {
            "name": "A", "type": "filament",
            "filament_flow_ratio": ["1.0"],
            "filament_vendor": ["eSUN"],
            "filament_notes": ["only in a"],
        }
        b = {
            "name": "B", "type": "filament",
            "filament_flow_ratio": ["1.05"],
            "filament_vendor": ["eSUN"],
            "filament_density": ["1.24"],
        }
        diff = preset_diff.diff_presets(a, b)
        assert diff["name_a"] == "A"
        assert diff["name_b"] == "B"
        assert _field(diff, "filament_flow_ratio")["status"] == "diff"
        assert _field(diff, "filament_flow_ratio")["value_a"] == "1.0"
        assert _field(diff, "filament_flow_ratio")["value_b"] == "1.05"
        assert _field(diff, "filament_vendor")["status"] == "same"
        assert _field(diff, "filament_notes")["status"] == "only_a"
        assert _field(diff, "filament_density")["status"] == "only_b"
        assert _field(diff, "filament_density")["value_a"] == ""
        assert diff["summary"] == {"diff": 1, "same": 1, "only_a": 1, "only_b": 1}

    def test_flatten_compares_effective_values(self):
        """A inherits flow_ratio 0.98 from its base; B sets 1.05 → diff."""
        a = {
            "name": "Inherited A", "type": "filament",
            "inherits": "eSUN PLA+ @System",
        }
        b = {
            "name": "B", "type": "filament",
            "filament_flow_ratio": ["1.05"],
        }
        diff = preset_diff.diff_presets(a, b)
        f = _field(diff, "filament_flow_ratio")
        assert f["status"] == "diff"
        assert f["value_a"] == "0.98"
        assert f["value_b"] == "1.05"
        # A's inherited vendor resolves to eSUN; B has none → only_a
        assert _field(diff, "filament_vendor")["status"] == "only_a"
        assert _field(diff, "filament_vendor")["value_a"] == "eSUN"

    def test_no_flatten_uses_raw_fields(self):
        a = {
            "name": "A", "type": "filament",
            "inherits": "eSUN PLA+ @System",
        }
        b = {"name": "B", "type": "filament", "filament_flow_ratio": ["1.05"]}
        diff = preset_diff.diff_presets(a, b, flatten=False)
        # Raw A has no flow_ratio (it lives in the base) → only_b
        assert _field(diff, "filament_flow_ratio")["status"] == "only_b"

    def test_meta_keys_excluded(self):
        a = {
            "name": "A", "type": "filament", "version": "2.4.0.3",
            "setting_id": "abc", "filament_settings_id": "x",
            "filament_flow_ratio": ["1.0"],
        }
        b = {"name": "B", "type": "filament", "filament_flow_ratio": ["1.0"]}
        diff = preset_diff.diff_presets(a, b)
        keys = {f["key"] for c in diff["categories"] for f in c["fields"]}
        assert "type" not in keys
        assert "version" not in keys
        assert "setting_id" not in keys
        assert "filament_settings_id" not in keys
        assert "filament_flow_ratio" in keys

    def test_categories_group_fields(self):
        a = {
            "name": "A", "type": "filament",
            "nozzle_temperature": ["220"],
            "filament_flow_ratio": ["1.0"],
            "filament_start_gcode": ["G90\nM83"],
        }
        b = {"name": "B", "type": "filament"}
        diff = preset_diff.diff_presets(a, b)
        cats = {c["name"]: [f["key"] for f in c["fields"]] for c in diff["categories"]}
        assert "nozzle_temperature" in cats["Temperature"]
        assert "filament_flow_ratio" in cats["Flow & Pressure"]
        assert "filament_start_gcode" in cats["G-Code"]

    def test_multiline_flag(self):
        a = {"name": "A", "type": "filament", "filament_start_gcode": ["G90\nM83"], "filament_flow_ratio": ["1.0"]}
        b = {"name": "B", "type": "filament", "filament_start_gcode": ["G90"], "filament_flow_ratio": ["1.0"]}
        diff = preset_diff.diff_presets(a, b)
        f = _field(diff, "filament_start_gcode")
        assert f["multiline"] is True
        assert f["status"] == "diff"
        assert _field(diff, "filament_flow_ratio")["multiline"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/test.sh tests/test_preset_diff.py -x`
Expected: FAIL — `ModuleNotFoundError: No module named 'fm.service.preset_diff'` (collection error).

- [ ] **Step 3: Write minimal implementation**

Create `fm/service/preset_diff.py`:

```python
# fm/service/preset_diff.py
"""Visual side-by-side diff of two Orca Cloud presets.

Read-only: the caller supplies two local-format preset dicts (parameter
fields as single-element arrays); ``diff_presets`` flattens each side's
``inherits`` chain against bundled templates and reports per-field status
(diff / same / only_a / only_b) grouped by display category.  Rendered by
``_preset_diff_modal.html`` via ``POST /sync/compare-modal``.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..presets.flattener import find_preset_by_name, flatten_preset


# Keys that describe the preset resource rather than tunable values.
_META_KEYS = frozenset({
    "type", "name", "inherits", "from", "setting_id", "instantiation",
    "version", "compatible_printers", "filament_settings_id",
    "updated_time", "_path", "_system",
})

# Display order + field → category mapping. Unmapped keys land in "Other".
_CATEGORY_ORDER = (
    "Identity", "Temperature", "Flow & Pressure",
    "Retraction", "Cooling", "G-Code", "Other",
)
_FIELD_CATEGORY = {
    "filament_type": "Identity",
    "filament_vendor": "Identity",
    "filament_id": "Identity",
    "filament_density": "Identity",
    "filament_diameter": "Identity",
    "filament_cost": "Identity",
    "default_filament_colour": "Identity",
    "nozzle_temperature": "Temperature",
    "nozzle_temperature_initial_layer": "Temperature",
    "nozzle_temperature_range_low": "Temperature",
    "nozzle_temperature_range_high": "Temperature",
    "cool_plate_temp": "Temperature",
    "cool_plate_temp_initial_layer": "Temperature",
    "eng_plate_temp": "Temperature",
    "eng_plate_temp_initial_layer": "Temperature",
    "hot_plate_temp": "Temperature",
    "hot_plate_temp_initial_layer": "Temperature",
    "bed_temperature_difference": "Temperature",
    "filament_flow_ratio": "Flow & Pressure",
    "filament_max_volumetric_speed": "Flow & Pressure",
    "pressure_advance": "Flow & Pressure",
    "enable_pressure_advance": "Flow & Pressure",
    "filament_retraction_length": "Retraction",
    "filament_retract_length": "Retraction",
    "filament_retraction_speed": "Retraction",
    "filament_retract_speed": "Retraction",
    "fan_min_speed": "Cooling",
    "fan_max_speed": "Cooling",
    "slow_down_layer_time": "Cooling",
    "overhang_fan_speed": "Cooling",
    "reduce_fan_stop_start_freq": "Cooling",
    "filament_start_gcode": "G-Code",
    "filament_end_gcode": "G-Code",
}


def _normalize(value: object) -> str:
    """Normalize a preset value for comparison and display.

    Local preset format stores parameter fields as single-element arrays;
    cloud format uses flat strings.  Both collapse to a stripped string.
    """
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip()


def _values_equal(a: object, b: object) -> bool:
    """True when normalized values match, coercing numerics (21 == 21.0)."""
    na, nb = _normalize(a), _normalize(b)
    try:
        return float(na) == float(nb)
    except (TypeError, ValueError):
        return na == nb


def _field_label(key: str) -> str:
    """Human label: strip ``filament_`` prefix, underscores → spaces."""
    return key.removeprefix("filament_").replace("_", " ").capitalize()


def _flatten_dict(preset: dict) -> dict:
    """Flatten a preset dict via ``flatten_preset`` (temp-file round-trip)."""
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as f:
        json.dump(preset, f)
        path = Path(f.name)
    try:
        return flatten_preset(path)
    finally:
        path.unlink(missing_ok=True)


def diff_presets(preset_a: dict, preset_b: dict, flatten: bool = True) -> dict:
    """Diff two local-format preset dicts field by field.

    With ``flatten=True`` (default) both sides are resolved through their
    ``inherits`` chains first, so inherited values compare by effective
    value.  ``flatten=False`` compares the raw field sets.

    Returns a display-ready dict: ``name_a``, ``name_b``, ``summary``
    counts per status, and ``categories`` — a list of ``{"name", "fields"}``
    in ``_CATEGORY_ORDER`` where each field is ``{"key", "label", "status",
    "value_a", "value_b", "multiline"}`` and ``status`` is
    ``diff|same|only_a|only_b``.
    """
    a = _flatten_dict(preset_a) if flatten else preset_a
    b = _flatten_dict(preset_b) if flatten else preset_b

    keys = sorted((set(a) | set(b)) - _META_KEYS)
    buckets: dict[str, list[dict]] = {}
    summary = {"diff": 0, "same": 0, "only_a": 0, "only_b": 0}
    for key in keys:
        in_a = key in a
        in_b = key in b
        if in_a and in_b:
            status = "same" if _values_equal(a[key], b[key]) else "diff"
        elif in_a:
            status = "only_a"
        else:
            status = "only_b"
        summary[status] += 1
        value_a = _normalize(a.get(key))
        value_b = _normalize(b.get(key))
        category = _FIELD_CATEGORY.get(key, "Other")
        buckets.setdefault(category, []).append({
            "key": key,
            "label": _field_label(key),
            "status": status,
            "value_a": value_a,
            "value_b": value_b,
            "multiline": "\n" in value_a or "\n" in value_b,
        })

    return {
        "name_a": _normalize(preset_a.get("name")),
        "name_b": _normalize(preset_b.get("name")),
        "summary": summary,
        "categories": [
            {"name": cat, "fields": buckets[cat]}
            for cat in _CATEGORY_ORDER if cat in buckets
        ],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/test.sh tests/test_preset_diff.py -x`
Expected: PASS — 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add fm/service/preset_diff.py tests/test_preset_diff.py
git commit -m "feat: add two-preset diff engine with inheritance flattening"
```

---

### Task 2: Cloud preset fetcher & diff orchestration

**Files:**
- Modify: `fm/service/preset_diff.py`
- Test: `tests/test_preset_diff.py`

**Interfaces:**
- Consumes: `fm.service.sync_handlers._orca_client_factory()` (module-attribute call — tests monkeypatch `sync_handlers._orca_client_factory`), `fm.service.sync_handlers._cloud_presets_to_local(upserts)` (converts cloud upserts to local format, filters `type != "filament"`, sets `_path` = resource id), `OrcaCloudClient.sync_pull(cursor=0)` → `{"next_cursor", "upserts", "deletes"}`, `find_preset_by_name` + `_flatten_dict` (Task 1) for the `@base` case.
- Produces:
  - `PresetDiffError(Exception)` — user-facing failure.
  - `fetch_cloud_presets(client) -> dict[str, dict]` — `{resource_id: local-format preset}`; raises `PresetDiffError` on pull failure.
  - `build_preset_diff(preset_a_id: str, preset_b_id: str) -> dict` — same shape as `diff_presets` result; `preset_b_id == "@base"` resolves to A's flattened inherits chain; raises `PresetDiffError` when Orca Cloud is unconfigured, an id is unknown, or `@base` is unresolvable.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_preset_diff.py` (add `from fm.orca_cloud import OrcaCloudError` to the imports at the top):

```python
class TestFetchCloudPresets:
    def _client(self, upserts):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.sync_pull.return_value = {
            "next_cursor": 0, "upserts": upserts, "deletes": [],
        }
        return c

    def test_returns_presets_by_id(self):
        client = self._client([{
            "id": "uuid-1",
            "content": {
                "type": "filament",
                "name": "Cloud PLA",
                "filament_type": "PLA",
                "nozzle_temperature": "220",
            },
        }])
        presets = preset_diff.fetch_cloud_presets(client)
        assert presets["uuid-1"]["name"] == "Cloud PLA"
        assert presets["uuid-1"]["nozzle_temperature"] == ["220"]

    def test_pull_error_raises(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.sync_pull.side_effect = OrcaCloudError(500, "boom")
        with pytest.raises(preset_diff.PresetDiffError, match="Cloud pull failed"):
            preset_diff.fetch_cloud_presets(client)

    def test_non_filament_presets_skipped(self):
        client = self._client([{
            "id": "uuid-print",
            "content": {"type": "print", "name": "0.2mm"},
        }])
        assert preset_diff.fetch_cloud_presets(client) == {}


class TestBuildPresetDiff:
    def _client(self, upserts):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.sync_pull.return_value = {
            "next_cursor": 0, "upserts": upserts, "deletes": [],
        }
        return c

    def test_not_connected_raises(self, monkeypatch):
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: None
        )
        with pytest.raises(preset_diff.PresetDiffError, match="not connected"):
            preset_diff.build_preset_diff("uuid-1", "uuid-2")

    def test_unknown_id_raises(self, monkeypatch):
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory",
            lambda: self._client([]),
        )
        with pytest.raises(preset_diff.PresetDiffError, match="not found"):
            preset_diff.build_preset_diff("uuid-1", "uuid-2")

    def test_end_to_end(self, monkeypatch):
        client = self._client([
            {
                "id": "uuid-a",
                "content": {
                    "type": "filament",
                    "name": "eSUN PLA",
                    "inherits": "eSUN PLA+ @System",
                    "filament_flow_ratio": "1.10",
                },
            },
            {
                "id": "uuid-b",
                "content": {
                    "type": "filament",
                    "name": "SUNLU PLA",
                    "filament_flow_ratio": "0.95",
                    "filament_max_volumetric_speed": "12",
                },
            },
        ])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        diff = preset_diff.build_preset_diff("uuid-a", "uuid-b")
        assert diff["name_a"] == "eSUN PLA"
        assert diff["name_b"] == "SUNLU PLA"
        assert _field(diff, "filament_flow_ratio")["status"] == "diff"
        # A inherits max_volumetric 16 from base; B sets 12 → diff
        f = _field(diff, "filament_max_volumetric_speed")
        assert f["status"] == "diff"
        assert f["value_a"] == "16"
        assert f["value_b"] == "12"

    def test_base_pseudo_id(self, monkeypatch):
        client = self._client([{
            "id": "uuid-a",
            "content": {
                "type": "filament",
                "name": "eSUN PLA",
                "inherits": "eSUN PLA+ @System",
                "filament_flow_ratio": "1.10",
            },
        }])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        diff = preset_diff.build_preset_diff("uuid-a", "@base")
        assert diff["name_b"] == "eSUN PLA+ @System"
        f = _field(diff, "filament_flow_ratio")
        assert f["status"] == "diff"
        assert f["value_a"] == "1.10"
        assert f["value_b"] == "0.98"

    def test_base_on_standalone_raises(self, monkeypatch):
        client = self._client([{
            "id": "uuid-a",
            "content": {
                "type": "filament",
                "name": "Solo",
                "inherits": "",
                "filament_flow_ratio": "1.0",
            },
        }])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        with pytest.raises(preset_diff.PresetDiffError, match="base template"):
            preset_diff.build_preset_diff("uuid-a", "@base")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/test.sh tests/test_preset_diff.py -x`
Expected: FAIL — `AttributeError: module 'fm.service.preset_diff' has no attribute 'fetch_cloud_presets'`.

- [ ] **Step 3: Write minimal implementation**

In `fm/service/preset_diff.py`, the import block becomes exactly these lines (the flattener import is kept; `OrcaCloudError` is intentionally not imported — pull failures are caught broadly below, matching `cache_sync.py`):

```python
from ..orca_cloud import OrcaCloudClient
from ..presets.flattener import find_preset_by_name, flatten_preset
from . import sync_handlers


class PresetDiffError(Exception):
    """User-facing failure while building a preset diff."""
```

Append at the end of the file:

```python
def _resolve_base(preset: dict) -> dict:
    """Flattened inherits chain of ``preset``; raises when unresolvable."""
    inherits = _normalize(preset.get("inherits"))
    base_path = find_preset_by_name(inherits) if inherits else None
    if base_path is None:
        raise PresetDiffError("Preset has no resolvable base template")
    return flatten_preset(base_path)


def fetch_cloud_presets(client: OrcaCloudClient) -> dict[str, dict]:
    """Fetch all filament presets from Orca Cloud (read-only pull).

    Returns ``{resource_id: preset}`` in local format (parameter fields
    wrapped in arrays, ``_path``/``_system`` markers added).  Raises
    :class:`PresetDiffError` on pull failure.
    """
    try:
        pull_result = client.sync_pull(cursor=0)
    except Exception as exc:
        raise PresetDiffError(f"Cloud pull failed: {exc}") from exc
    upserts = pull_result.get("upserts", [])
    return {
        p["_path"]: p
        for p in sync_handlers._cloud_presets_to_local(upserts)
        if p.get("_path")
    }


def build_preset_diff(preset_a_id: str, preset_b_id: str) -> dict:
    """Fetch two cloud presets and build their categorized field diff.

    ``preset_b_id == "@base"`` diffs preset A against its resolved
    inheritance base.  Raises :class:`PresetDiffError` when Orca Cloud is
    not configured, the pull fails, an id is unknown, or the base is
    unresolvable.
    """
    client = sync_handlers._orca_client_factory()
    if client is None:
        raise PresetDiffError(
            "Orca Cloud not connected — set up the connection in Settings"
        )
    presets = fetch_cloud_presets(client)
    preset_a = presets.get(preset_a_id)
    if preset_a is None:
        raise PresetDiffError(f"Preset not found in Orca Cloud: {preset_a_id}")
    if preset_b_id == "@base":
        preset_b = _resolve_base(preset_a)
    else:
        preset_b = presets.get(preset_b_id)
        if preset_b is None:
            raise PresetDiffError(
                f"Preset not found in Orca Cloud: {preset_b_id}"
            )
    return diff_presets(preset_a, preset_b)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/test.sh tests/test_preset_diff.py -x`
Expected: PASS — 16 tests pass.

- [ ] **Step 5: Commit**

```bash
git add fm/service/preset_diff.py tests/test_preset_diff.py
git commit -m "feat: add cloud preset fetcher and two-preset diff orchestration"
```

---

### Task 3: Modal endpoint `POST /sync/compare-modal`

**Files:**
- Modify: `fm/api/ui.py`
- Create: `fm/api/templates/_preset_diff_modal.html`
- Test: `tests/test_ui_sync.py`

**Interfaces:**
- Consumes: `preset_diff.build_preset_diff(preset_a_id, preset_b_id) -> dict`, `preset_diff.PresetDiffError` (Task 2); `_templates(request).TemplateResponse(request, name, ctx)` pattern already in `ui.py`; `ProfileCache` rows for the pick-B select (`cloud_resource_id`, `vendor`, `name`, `deleted_from_cloud`).
- Produces: `POST /sync/compare-modal` (form fields `preset_a_id: str = Form(...)`, `preset_b_id: str | None = Form(None)`) → HTML fragment for `#diff-modal-body`. Context: `{"diff": diff}` on success, `{"error": str}` on `PresetDiffError`, `{"pick_b": True, "preset_a_id": str, "presets": list[ProfileCache]}` when `preset_b_id` is absent. Always HTTP 200 (modal renders errors inline, same as existing modal endpoints).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ui_sync.py` (file already imports `SessionLocal`, `ProfileCache`, `datetime`, `timezone`, `_fake_orca`, `sh`):

```python
# ---------------------------------------------------------------------------
# Compare modal — two-preset diff fragment
# ---------------------------------------------------------------------------

class TestCompareModal:
    """Tests for POST /sync/compare-modal — preset A vs preset B diff."""

    def _orca_with_presets(self, upserts):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.sync_pull.return_value = {
            "next_cursor": 0, "upserts": upserts, "deletes": [],
        }
        return c

    def test_compare_modal_renders_diff(self, client, monkeypatch):
        orca = self._orca_with_presets([
            {"id": "uuid-a", "content": {
                "type": "filament", "name": "eSUN PETG",
                "filament_flow_ratio": "0.98",
                "filament_max_volumetric_speed": "12",
            }},
            {"id": "uuid-b", "content": {
                "type": "filament", "name": "SUNLU PETG",
                "filament_flow_ratio": "0.98",
                "filament_max_volumetric_speed": "8",
            }},
        ])
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        r = client.post("/sync/compare-modal",
                        data={"preset_a_id": "uuid-a",
                              "preset_b_id": "uuid-b"})
        assert r.status_code == 200
        assert b"eSUN PETG" in r.content
        assert b"SUNLU PETG" in r.content
        assert b"status-diff" in r.content
        assert b"status-same" in r.content
        assert b"diff-table" in r.content
        assert b"toggleDiffOnly" in r.content
        assert b"closeDiffModal" in r.content

    def test_compare_modal_pick_b(self, client):
        """Posting only preset_a_id renders the Preset-B picker."""
        with SessionLocal() as s:
            s.add(ProfileCache(
                cloud_resource_id="uuid-b",
                name="Target PLA",
                vendor="SUNLU",
                type="PLA",
                last_seen_at=datetime.now(timezone.utc),
            ))
            s.commit()
        r = client.post("/sync/compare-modal",
                        data={"preset_a_id": "uuid-a"})
        assert r.status_code == 200
        assert b"Choose Preset B" in r.content
        assert b'name="preset_b_id"' in r.content
        assert b'value="uuid-a"' in r.content
        assert b"Target PLA" in r.content

    def test_compare_modal_base_pseudo_id(self, client, monkeypatch):
        orca = self._orca_with_presets([{
            "id": "uuid-a",
            "content": {
                "type": "filament", "name": "eSUN PLA",
                "inherits": "eSUN PLA+ @System",
                "filament_flow_ratio": "1.10",
            },
        }])
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: orca)
        r = client.post("/sync/compare-modal",
                        data={"preset_a_id": "uuid-a",
                              "preset_b_id": "@base"})
        assert r.status_code == 200
        assert b"eSUN PLA+ @System" in r.content
        assert b"status-diff" in r.content

    def test_compare_modal_preset_not_found(self, client, monkeypatch):
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: _fake_orca())
        r = client.post("/sync/compare-modal",
                        data={"preset_a_id": "missing",
                              "preset_b_id": "missing2"})
        assert r.status_code == 200
        assert b"Compare failed" in r.content
        assert b"not found" in r.content

    def test_compare_modal_not_connected(self, client, monkeypatch):
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        r = client.post("/sync/compare-modal",
                        data={"preset_a_id": "uuid-a",
                              "preset_b_id": "uuid-b"})
        assert r.status_code == 200
        assert b"not connected" in r.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/test.sh tests/test_ui_sync.py::TestCompareModal -x`
Expected: FAIL — 404/405 on `POST /sync/compare-modal` (route does not exist).

- [ ] **Step 3: Write minimal implementation**

In `fm/api/ui.py`, add the import next to `from ..service import sync_handlers` (line ~25):

```python
from ..service import preset_diff
```

Append the endpoint after `sync_backup_modal_poll` (after line ~713, before `sync_resolve_modal`):

```python
@router.post("/sync/compare-modal", response_class=HTMLResponse)
def sync_compare_modal(
    request: Request, db: DbSession,
    preset_a_id: str = Form(...),
    preset_b_id: str | None = Form(None),
) -> HTMLResponse:
    """Render the two-preset diff modal body.

    Read-only: pulls live presets from Orca Cloud, resolves each side's
    ``inherits`` chain against bundled templates, and renders the
    categorized side-by-side diff.  ``preset_b_id == "@base"`` compares A
    against its resolved base template.  Without ``preset_b_id`` the
    fragment offers a Preset-B picker (row "Compare" buttons pre-select A).
    All failures render an error fragment (HTTP 200) inside the modal.
    """
    if not preset_b_id:
        presets = list(db.scalars(
            select(ProfileCache)
            .where(ProfileCache.deleted_from_cloud == False)  # noqa: E712
            .order_by(ProfileCache.vendor, ProfileCache.name)
        ))
        preset_a = db.scalar(
            select(ProfileCache).where(ProfileCache.cloud_resource_id == preset_a_id)
        )
        return _templates(request).TemplateResponse(
            request, "_preset_diff_modal.html",
            {
                "pick_b": True,
                "preset_a_id": preset_a_id,
                "preset_a": preset_a,
                "presets": presets,
            },
        )
    try:
        diff = preset_diff.build_preset_diff(preset_a_id, preset_b_id)
    except preset_diff.PresetDiffError as exc:
        return _templates(request).TemplateResponse(
            request, "_preset_diff_modal.html", {"error": str(exc)},
        )
    return _templates(request).TemplateResponse(
        request, "_preset_diff_modal.html", {"diff": diff},
    )

Create `fm/api/templates/_preset_diff_modal.html`:

```html
{# Fragment: two-preset diff modal body.
   Rendered by POST /sync/compare-modal into #diff-modal-body.
   States: error | pick_b | diff. #}
{% if error %}
<div class="push-modal-state push-modal-error">
  <div class="push-modal-icon push-modal-icon-err">✗</div>
  <h3>Compare failed</h3>
  <p class="push-modal-error-msg">{{ error }}</p>
  <button class="button button-secondary" onclick="closeDiffModal()">Close</button>
</div>
{% elif pick_b %}
<div class="diff-modal-head">
  <div class="diff-title-row">
    <div class="diff-title-content">
      <span class="eyebrow">Compare preset</span>
      <h3>Choose Preset B</h3>
      <p class="diff-inherits">Select a second preset or base template to compare against.</p>
    </div>
    <button type="button" class="diff-close-btn" onclick="closeDiffModal()" aria-label="Close">✕</button>
  </div>
</div>
<form class="diff-pick" hx-post="/sync/compare-modal" hx-target="#diff-modal-body" hx-swap="innerHTML">
  <input type="hidden" name="preset_a_id" value="{{ preset_a_id }}">
  {% if preset_a %}
  <div class="diff-pick-preset-a">
    <span class="diff-pick-preset-a-label">Preset A (selected)</span>
    <span class="diff-pick-preset-a-name">{{ preset_a.vendor }} {{ preset_a.name }}</span>
  </div>
  {% endif %}
  <label>
    <span>Preset B</span>
    <select name="preset_b_id" required>
      <option value="" disabled selected>Select a preset…</option>
      <option value="@base">— Base template of A —</option>
      {% for p in presets %}
      <option value="{{ p.cloud_resource_id }}">{{ p.vendor }} {{ p.name }}</option>
      {% endfor %}
    </select>
  </label>
  <div class="button-group">
    <button type="button" class="button button-ghost" onclick="closeDiffModal()">Cancel</button>
    <button type="submit" class="button button-primary">Compare</button>
  </div>
</form>
{% else %}
<div class="diff-modal-head">
  <div class="diff-title-row">
    <div class="diff-title-content">
      <span class="eyebrow">Preset Comparison</span>
      <h3 class="diff-vs">
        <span class="preset-name preset-name-a">{{ diff.name_a }}</span>
        <span class="diff-vs-badge">vs</span>
        <span class="preset-name preset-name-b">{{ diff.name_b }}</span>
      </h3>
    </div>
    <button type="button" class="diff-close-btn" onclick="closeDiffModal()" aria-label="Close diff modal">✕</button>
  </div>
  <div class="diff-controls-row">
    <div class="diff-summary">
      <span class="diff-pill diff-pill-diff"><strong>{{ diff.summary.diff }}</strong> different</span>
      <span class="diff-pill"><strong>{{ diff.summary.same }}</strong> identical</span>
      {% if diff.summary.only_a > 0 %}
      <span class="diff-pill diff-pill-only-a"><strong>{{ diff.summary.only_a }}</strong> only in A</span>
      {% endif %}
      {% if diff.summary.only_b > 0 %}
      <span class="diff-pill diff-pill-only-b"><strong>{{ diff.summary.only_b }}</strong> only in B</span>
      {% endif %}
    </div>
    <label class="diff-only-toggle" title="Show only differing parameters">
      <input type="checkbox" onchange="toggleDiffOnly(this)">
      <span class="toggle-track" aria-hidden="true"><span class="toggle-thumb"></span></span>
      <span class="toggle-label">Only differences</span>
    </label>
  </div>
</div>
<div class="table-wrap diff-table-wrap">
  <table class="diff-table">
    <thead>
      <tr>
        <th class="col-field">Parameter</th>
        <th class="col-val col-val-a">
          <span class="col-side-tag">Preset A</span>
          <span class="col-preset-name" title="{{ diff.name_a }}">{{ diff.name_a }}</span>
        </th>
        <th class="col-val col-val-b">
          <span class="col-side-tag">Preset B</span>
          <span class="col-preset-name" title="{{ diff.name_b }}">{{ diff.name_b }}</span>
        </th>
        <th class="col-status">Status</th>
      </tr>
    </thead>
    <tbody>
    {% for cat in diff.categories %}
      <tr class="diff-cat">
        <td colspan="4">
          <div class="diff-cat-header">
            <span class="diff-cat-name">{{ cat.name }}</span>
            <span class="diff-cat-count">{{ cat.fields | length }} parameters</span>
          </div>
        </td>
      </tr>
      {% for f in cat.fields %}
      <tr class="diff-row diff-row-{{ f.status }}">
        <td class="cell-field">
          <span class="field-label">{{ f.label }}</span>
          <span class="field-key">{{ f.key }}</span>
        </td>
        <td class="cell-val cell-val-a">
          {% if f.multiline %}
            <pre class="diff-pre"><code>{{ f.value_a }}</code></pre>
          {% else %}
            <span class="val-text">{{ f.value_a if f.value_a else '—' }}</span>
          {% endif %}
        </td>
        <td class="cell-val cell-val-b">
          {% if f.multiline %}
            <pre class="diff-pre"><code>{{ f.value_b }}</code></pre>
          {% else %}
            <span class="val-text">{{ f.value_b if f.value_b else '—' }}</span>
          {% endif %}
        </td>
        <td class="cell-status">
          <span class="status-badge status-{{ f.status }}">
            {% if f.status == 'diff' %}≠ Diff
            {% elif f.status == 'same' %}= Same
            {% elif f.status == 'only_a' %}Only A
            {% elif f.status == 'only_b' %}Only B
            {% else %}{{ f.status | replace('_', ' ') }}{% endif %}
          </span>
        </td>
      </tr>
      {% endfor %}
    {% endfor %}
    </tbody>
  </table>
</div>
<div class="diff-modal-foot">
  <button class="button button-secondary" onclick="closeDiffModal()">Close</button>
</div>
{% endif %}

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/test.sh tests/test_ui_sync.py::TestCompareModal -x`
Expected: PASS — 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add fm/api/ui.py fm/api/templates/_preset_diff_modal.html tests/test_ui_sync.py
git commit -m "feat: add POST /sync/compare-modal two-preset diff endpoint"
```

---

### Task 4: UI integration — compare picker, row button, diff modal scaffolding, CSS

**Files:**
- Modify: `fm/api/templates/_sync_hub_grid.html` (compare picker in the preset panel + row action, ~lines 11 and 40)
- Modify: `fm/api/templates/sync_hub.html` (`<style>` block ~line 48, modal markup after line 65, `<script>` block)
- Modify: `fm/api/static/app.css` (append diff-modal styles at end of file)
- Test: `tests/test_ui_sync.py`

**Interfaces:**
- Consumes: `POST /sync/compare-modal` (Task 3) with form fields `preset_a_id` / `preset_b_id`; `presets` list already in the `_sync_hub_grid.html` context (`p.name`, `p.vendor`, `p.cloud_resource_id`, `p.status`).
- Produces: `#diff-modal` / `#diff-modal-body` DOM ids; `openDiffModal()` / `closeDiffModal()` / `toggleDiffOnly(cb)` JS functions; CSS classes `.compare-picker`, `.diff-modal-dialog`, `.diff-modal-body`, `.diff-modal-head`, `.diff-vs`, `.diff-inherits`, `.diff-summary`, `.diff-pill`, `.diff-pill-diff`, `.diff-only-toggle`, `.diff-table`, `.diff-cat`, `.diff-pre`, `.diff-modal-foot`, `.diff-pick`, `.diff-only`, `.diff-row-*`, `.status-diff`, `.status-same`, `.status-only_a`, `.status-only_b`.

- [ ] **Step 1: Write the failing test**

Append inside `TestCompareModal` in `tests/test_ui_sync.py`:

```python
    def test_sync_page_has_diff_modal_scaffolding(self, client):
        """The /sync page contains the diff-modal overlay container and JS."""
        r = client.get("/sync")
        assert r.status_code == 200
        assert b'id="diff-modal"' in r.content
        assert b'id="diff-modal-body"' in r.content
        assert b'openDiffModal' in r.content
        assert b'closeDiffModal' in r.content
        assert b'toggleDiffOnly' in r.content

    def test_sync_page_has_compare_picker(self, client):
        """The preset panel offers Preset A / Preset B selects + Compare."""
        with SessionLocal() as s:
            s.add(ProfileCache(
                cloud_resource_id="uuid-cmp",
                name="Compare Me PLA",
                vendor="Generic",
                type="PLA",
                last_seen_at=datetime.now(timezone.utc),
            ))
            s.commit()
        r = client.get("/sync")
        assert r.status_code == 200
        assert b'name="preset_a_id"' in r.content
        assert b'name="preset_b_id"' in r.content
        assert b'/sync/compare-modal' in r.content
        assert b'value="uuid-cmp"' in r.content
        assert b'value="@base"' in r.content

    def test_preset_row_has_compare_button(self, client):
        """Each non-orphan preset row gets a Compare button wired to the diff modal."""
        with SessionLocal() as s:
            s.add(ProfileCache(
                cloud_resource_id="uuid-cmp",
                name="Compare Me PLA",
                vendor="Generic",
                type="PLA",
                last_seen_at=datetime.now(timezone.utc),
            ))
            s.commit()
        r = client.get("/sync")
        assert r.status_code == 200
        assert b'hx-vals=\'{"preset_a_id": "uuid-cmp"}\'' in r.content
        assert b'#diff-modal-body' in r.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/test.sh tests/test_ui_sync.py::TestCompareModal -x`
Expected: FAIL — scaffolding/picker/button assertions fail on missing `id="diff-modal"`, `name="preset_a_id"`, and `preset_a_id` hx-vals markup.

- [ ] **Step 3: Write minimal implementation**

In `fm/api/templates/_sync_hub_grid.html`, add the compare toolbar inside the preset-table panel, right after the `<p class="target-note">…</p>` line (~line 11) and before `{% if presets %}`:

```html
      {% if presets %}
      <div class="compare-toolbar">
        <form class="compare-picker" hx-post="/sync/compare-modal" hx-target="#diff-modal-body" hx-swap="innerHTML" hx-on::before-request="openDiffModal()">
          <div class="compare-inputs">
            <label class="compare-label">
              <span>Preset A</span>
              <select name="preset_a_id" required>
                <option value="" disabled selected>Select preset A…</option>
                {% for p in presets %}{% if not p.deleted_from_cloud %}
                <option value="{{ p.cloud_resource_id }}">{{ p.vendor }} {{ p.name }}</option>
                {% endif %}{% endfor %}
              </select>
            </label>
            <span class="compare-vs-badge" aria-hidden="true">vs</span>
            <label class="compare-label">
              <span>Preset B</span>
              <select name="preset_b_id" required>
                <option value="" disabled selected>Select preset B…</option>
                <option value="@base">— Base template of A —</option>
                {% for p in presets %}{% if not p.deleted_from_cloud %}
                <option value="{{ p.cloud_resource_id }}">{{ p.vendor }} {{ p.name }}</option>
                {% endif %}{% endfor %}
              </select>
            </label>
          </div>
          <button type="submit" class="button button-secondary compare-submit-btn">
            <svg viewBox="0 0 16 16" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M1 2.5A1.5 1.5 0 0 1 2.5 1h3A1.5 1.5 0 0 1 7 2.5v11A1.5 1.5 0 0 1 5.5 15h-3A1.5 1.5 0 0 1 1 13.5v-11zM9 2.5A1.5 1.5 0 0 1 10.5 1h3A1.5 1.5 0 0 1 15 2.5v11a1.5 1.5 0 0 1-1.5 1.5h-3A1.5 1.5 0 0 1 9 13.5v-11z"/></svg>
            Compare
          </button>
        </form>
      </div>
      {% endif %}
```

In the same file, inside the `{% else %}` branch of the row-actions cell (non-orphan rows, ~line 40), add the Compare button before the Push button:

```html
                  <button class="button button-ghost" hx-post="/sync/compare-modal" hx-vals='{"preset_a_id": "{{ p.cloud_resource_id }}"}' hx-target="#diff-modal-body" hx-swap="innerHTML" hx-on::before-request="openDiffModal()">Compare</button>
                  <button class="button button-ghost" hx-post="/sync/push-modal" hx-vals='{"cloud_resource_ids": "{{ p.cloud_resource_id }}"}' hx-target="#action-modal-body" hx-swap="innerHTML" hx-on::before-request="openActionModal()">Push to Printer</button>
```

In `fm/api/templates/sync_hub.html`, add to the `<style>` block after the `.push-modal-awaiting` rule (~line 47):

```css
    /* Diff modal overlay — same pattern as #action-modal */
    #diff-modal { position: fixed; inset: 0; z-index: 2000; display: flex; align-items: center; justify-content: center; background: rgba(1,5,17,.7); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); }
    #diff-modal[hidden] { display: none; }
```

Add the modal markup after the `#action-modal` div (after line 65, before `<script>`):

```html
<div id="diff-modal" hidden role="dialog" aria-modal="true" aria-label="Preset diff" tabindex="-1">
  <div class="diff-modal-dialog">
    <div id="diff-modal-body" class="diff-modal-body" aria-live="polite">
      <div class="push-modal-state"><div class="spinner spinner-lg"></div><p>Loading…</p></div>
    </div>
  </div>
</div>
```

Append to the existing `<script>` block (after the `#action-modal` click listener, ~line 99):

```javascript
var _diffModalPriorFocus = null;
function openDiffModal() {
  var m = document.getElementById('diff-modal');
  if (!m) return;
  _diffModalPriorFocus = document.activeElement;
  m.removeAttribute('hidden');
  m.focus();
  var b = document.getElementById('diff-modal-body');
  if (b) b.innerHTML = '<div class="push-modal-state"><div class="spinner spinner-lg"></div><p>Comparing…</p></div>';
  document.addEventListener('keydown', _diffModalKeydown);
}
function closeDiffModal() {
  var m = document.getElementById('diff-modal');
  if (!m) return;
  m.setAttribute('hidden', '');
  document.removeEventListener('keydown', _diffModalKeydown);
  if (_diffModalPriorFocus && _diffModalPriorFocus.focus) _diffModalPriorFocus.focus();
  _diffModalPriorFocus = null;
}
function _diffModalKeydown(e) {
  if (e.key !== 'Escape') return;
  var m = document.getElementById('diff-modal');
  if (!m || m.hasAttribute('hidden')) return;
  var body = document.getElementById('diff-modal-body');
  if (body && body.querySelector('.spinner')) return;
  closeDiffModal();
}
document.getElementById('diff-modal').addEventListener('click', function(e) {
  if (e.target !== this) return;
  var body = document.getElementById('diff-modal-body');
  if (body && body.querySelector('.spinner')) return;
  closeDiffModal();
});
function toggleDiffOnly(cb) {
  var body = document.getElementById('diff-modal-body');
  if (!body) return;
  body.classList.toggle('diff-only', cb.checked);
  body.querySelectorAll('.diff-cat').forEach(function(cat) {
    var row = cat.nextElementSibling;
    var anyVisible = false;
    while (row && !row.classList.contains('diff-cat')) {
      if (!cb.checked || !row.classList.contains('diff-row-same')) anyVisible = true;
      row = row.nextElementSibling;
    }
    cat.style.display = anyVisible ? '' : 'none';
  });
}
```

Append to `fm/api/static/app.css` (end of file):

```css
/* ──────────────────────────────────────────────
   Preset diff — /sync compare toolbar & modal
   ────────────────────────────────────────────── */

/* Toolbar inside preset panel */
.compare-toolbar {
  padding: 14px 28px 18px;
  border-bottom: 1px solid var(--border);
  background: rgba(10, 19, 48, 0.4);
}
.compare-picker {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 14px;
}
.compare-inputs {
  display: flex;
  align-items: flex-end;
  flex: 1 1 540px;
  gap: 12px;
}
.compare-label {
  flex: 1 1 220px;
  min-width: 180px;
}
.compare-label > span {
  display: block;
  margin-bottom: 6px;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .02em;
}
.compare-vs-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 42px;
  padding: 0 4px;
  color: var(--text-muted);
  font-family: var(--font-heading);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  user-select: none;
}
.compare-submit-btn {
  flex: 0 0 auto;
  gap: 8px;
}

/* Diff modal layout */
.diff-modal-dialog {
  display: flex;
  flex-direction: column;
  width: min(1040px, 95vw);
  max-height: 88vh;
  padding: 0;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: 0 24px 60px rgba(0, 0, 0, .6), var(--shadow-card);
  overflow: hidden;
}

/* Modal header (sticky at top of dialog) */
.diff-modal-head {
  flex: 0 0 auto;
  padding: 24px 28px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.diff-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.diff-title-content {
  min-width: 0;
  flex: 1;
}
.diff-title-content .eyebrow {
  margin-bottom: 4px;
}
.diff-vs {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px 10px;
  margin: 0;
  font-size: 19px;
  line-height: 1.3;
}
.diff-vs .preset-name {
  color: var(--text);
  font-weight: 700;
}
.diff-vs-badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 4px;
  background: rgba(174, 185, 225, .1);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
}
.diff-close-btn {
  display: inline-grid;
  width: 32px;
  height: 32px;
  place-items: center;
  flex: 0 0 auto;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-input);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font-size: 14px;
  transition: color .15s, border-color .15s, background .15s;
}
.diff-close-btn:hover {
  border-color: var(--border-strong);
  background: rgba(174, 185, 225, .08);
  color: var(--text);
}
.diff-inherits {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

/* Filter / Summary toolbar */
.diff-controls-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid rgba(217, 225, 250, .08);
}
.diff-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.diff-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: rgba(10, 19, 48, .6);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 500;
}
.diff-pill strong {
  color: var(--text);
  font-weight: 700;
}
.diff-pill-diff {
  border-color: rgba(253, 181, 42, .35);
  background: rgba(253, 181, 42, .1);
  color: #ffd070;
}
.diff-pill-diff strong {
  color: #ffd070;
}
.diff-pill-only-a, .diff-pill-only-b {
  border-color: rgba(56, 160, 255, .3);
  background: rgba(56, 160, 255, .08);
  color: #77c3ff;
}
.diff-pill-only-a strong, .diff-pill-only-b strong {
  color: #77c3ff;
}

/* Only-differences toggle switch */
.diff-only-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  user-select: none;
}
.diff-only-toggle input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
  pointer-events: none;
}
.diff-only-toggle .toggle-track {
  position: relative;
  display: inline-block;
  width: 32px;
  height: 18px;
  border-radius: 999px;
  background: var(--surface-raised);
  border: 1px solid var(--border-strong);
  transition: background .2s ease, border-color .2s ease;
}
.diff-only-toggle .toggle-thumb {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--text-muted);
  transition: transform .2s cubic-bezier(0.2, 0, 0, 1), background .2s ease;
}
.diff-only-toggle input:checked + .toggle-track {
  background: rgba(203, 60, 255, .25);
  border-color: var(--accent);
}
.diff-only-toggle input:checked + .toggle-track .toggle-thumb {
  transform: translateX(14px);
  background: var(--accent);
}
.diff-only-toggle input:focus-visible + .toggle-track {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

/* Modal body (scrollable) */
.diff-modal-body {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

/* Diff table & sticky headers */
.diff-table-wrap {
  width: 100%;
}
.diff-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
}
.diff-table thead th {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--surface-raised);
  box-shadow: 0 2px 8px rgba(0, 0, 0, .25);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.diff-table .col-field {
  width: 24%;
  min-width: 170px;
}
.diff-table .col-val {
  width: 33%;
  min-width: 190px;
}
.diff-table .col-side-tag {
  display: block;
  font-size: 9px;
  color: var(--text-muted);
  letter-spacing: .06em;
}
.diff-table .col-preset-name {
  display: block;
  margin-top: 1px;
  color: var(--text);
  font-size: 12px;
  font-weight: 600;
  text-transform: none;
  letter-spacing: normal;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.diff-table .col-status {
  width: 10%;
  min-width: 90px;
  text-align: right;
}

/* Category separator row */
.diff-cat td {
  padding: 9px 18px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid rgba(217, 225, 250, .08);
  background: rgba(16, 29, 66, .85);
}
.diff-cat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.diff-cat-name {
  color: #c8d4f5;
  font-family: var(--font-heading);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.diff-cat-count {
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 500;
}

/* Field row styling */
.diff-row td {
  height: auto;
  padding: 10px 18px;
  border-bottom: 1px solid rgba(217, 225, 250, .06);
  font-size: 12px;
  vertical-align: top;
}
.diff-row:hover td {
  background: rgba(174, 185, 225, .03);
}
.diff-row-diff td {
  background: rgba(253, 181, 42, .03);
}
.diff-row-diff:hover td {
  background: rgba(253, 181, 42, .06);
}
.diff-row-diff .cell-val-a .val-text,
.diff-row-diff .cell-val-b .val-text {
  color: #ffd070;
  font-weight: 600;
}
.diff-row-same .cell-val {
  color: var(--text-secondary);
}
.cell-field {
  overflow-wrap: break-word;
}
.field-label {
  display: block;
  color: var(--text);
  font-weight: 600;
  line-height: 1.35;
}
.field-key {
  display: block;
  margin-top: 2px;
  color: var(--text-muted);
  font-family: monospace;
  font-size: 10px;
}
.cell-val {
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.val-text {
  display: inline-block;
  line-height: 1.4;
}
.cell-status {
  text-align: right;
  white-space: nowrap;
}

/* Multiline G-code styling */
.diff-pre {
  max-height: 160px;
  margin: 0;
  padding: 8px 10px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-input);
  background: var(--bg);
  color: #c8d4f5;
  font-family: monospace;
  font-size: 11px;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-all;
}
.diff-pre code {
  color: inherit;
  font-family: inherit;
}

/* Diff row hiding when "Only differences" is checked */
.diff-only .diff-row-same {
  display: none;
}

/* Status badges */
.status-diff {
  color: #ffd070;
  background: rgba(253, 181, 42, .14);
  border: 1px solid rgba(253, 181, 42, .3);
}
.status-same {
  color: var(--text-muted);
  background: rgba(174, 185, 225, .07);
  border: 1px solid rgba(217, 225, 250, .1);
}
.status-only_a, .status-only_b {
  color: #77c3ff;
  background: rgba(56, 160, 255, .12);
  border: 1px solid rgba(56, 160, 255, .3);
}

/* Pick-B dialog layout */
.diff-pick {
  display: grid;
  gap: 18px;
  padding: 24px 28px;
}
.diff-pick-preset-a {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-input);
  background: var(--surface-alt);
}
.diff-pick-preset-a-label {
  display: block;
  margin-bottom: 4px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.diff-pick-preset-a-name {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}
.diff-pick .button-group {
  justify-content: flex-end;
  margin-top: 6px;
}

/* Modal footer */
.diff-modal-foot {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 16px 28px;
  border-top: 1px solid var(--border);
  background: var(--surface);
}

/* ──────────────────────────────────────────────
   Responsive rules
   ────────────────────────────────────────────── */
@media (max-width: 900px) {
  .compare-inputs {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .compare-vs-badge {
    height: auto;
    padding: 2px 0;
  }
  .compare-submit-btn {
    width: 100%;
  }
  .diff-table thead th,
  .diff-row td,
  .diff-cat td {
    padding-inline: 12px;
  }
}

@media (max-width: 760px) {
  .compare-toolbar {
    padding: 14px 18px;
  }
  .diff-modal-dialog {
    width: 100vw;
    height: 100%;
    max-height: 100vh;
    border-radius: 0;
    border: 0;
  }
  .diff-modal-head {
    padding: 18px 18px 14px;
  }
  .diff-controls-row {
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
  }
  .diff-pick {
    padding: 18px;
  }
  .diff-modal-foot {
    padding: 14px 18px;
  }
  .diff-table {
    min-width: 640px;
  }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/test.sh tests/test_ui_sync.py::TestCompareModal -x`
Expected: PASS — 8 tests pass.

Then run the full suite:

Run: `scripts/test.sh`
Expected: PASS — zero regressions.

- [ ] **Step 5: Commit**

```bash
git add fm/api/templates/_sync_hub_grid.html fm/api/templates/sync_hub.html fm/api/static/app.css tests/test_ui_sync.py
git commit -m "feat(ui): add compare picker and two-preset diff modal to Sync Hub"
```

---

## Self-Review

**1. Spec coverage:**
- §3 user flow (picker → modal → categorized side-by-side table → dismiss; row Compare → pick-B): Tasks 3–4. ✓
- §4.1 diff semantics (flatten both sides, normalization, statuses, meta keys, categories incl. G-Code, labels, multiline, `@base`): Tasks 1–2. ✓
- §4.2 error handling (not configured / pull failure / not found / unresolvable `@base` → error fragment): Tasks 2–3. ✓
- §5 interfaces (`PresetDiffError`, `fetch_cloud_presets`, `diff_presets`, `build_preset_diff`): Tasks 1–2. ✓
- §6 UI details (picker form, pick-B fragment, pills, only-differences toggle, `<pre>` G-code, a11y overlay, app.css classes, sync_hub `<style>` overlay rules): Tasks 3–4. ✓
- §7 non-goals: orphans excluded from picker (`deleted_from_cloud == False` query) and have no Compare button (non-orphan `{% else %}` branch only); no persistence anywhere. ✓
- §8 acceptance: covered by `TestCompareModal` (8 tests) + `test_preset_diff.py` (16 tests) + full-suite run in Task 4 Step 4. ✓

**2. Placeholder scan:** No TODO/TBD/"similar to" — every step carries complete code. ✓

**3. Type consistency:** `diff_presets` result keys (`name_a`, `name_b`, `summary`, `categories` → `fields` → `key/label/status/value_a/value_b/multiline`) are identical in Task 1 implementation, Task 3 template (`diff.name_a`, `diff.summary.diff`, `cat.fields`, `f.status`, `f.value_a`, `f.multiline`), and all tests. `build_preset_diff(preset_a_id, preset_b_id)` signature matches the endpoint call; `"@base"` handled inside it. `PresetDiffError` raised in Task 2, caught in Task 3. `status-*` classes emitted by the template (`status-diff`, `status-same`, `status-only_a`, `status-only_b`) all have CSS rules in Task 4; `diff-row-<status>` row classes match the `.diff-only .diff-row-same` rule and `toggleDiffOnly` JS. Monkeypatch seam: tests patch `sync_handlers._orca_client_factory`; `build_preset_diff` calls it via `sync_handlers._orca_client_factory()` module attribute — consistent. ✓
