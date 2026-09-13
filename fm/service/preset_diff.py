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

from ..orca_cloud import OrcaCloudClient
from ..presets.flattener import find_preset_by_name, flatten_preset
from . import sync_handlers


class PresetDiffError(Exception):
    """User-facing failure while building a preset diff."""


# Keys that describe the preset resource rather than tunable values.
_META_KEYS = frozenset({
    "type", "name", "inherits", "from", "setting_id", "instantiation",
    "version", "compatible_printers", "filament_settings_id",
    "updated_time", "_path", "_system",
})

# Display order + field -> category mapping. Unmapped keys land in "Other".
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
    """Human label: strip ``filament_`` prefix, underscores -> spaces."""
    return key.removeprefix("filament_").replace("_", " ").capitalize()


def _flatten_dict(preset: dict) -> dict:
    """Flatten a preset dict via ``flatten_preset`` (temp-file round-trip)."""
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w"
    ) as f:
        path = Path(f.name)
        json.dump(preset, f)
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
    counts per status, and ``categories`` -- a list of ``{"name", "fields"}``
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
