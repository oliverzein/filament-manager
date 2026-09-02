# fm/presets/builder.py
"""builder.py — OrcaSlicer preset builder.

Lifted from skill/orca.py (generate_filament_id, build_standalone_preset,
write_info_file). Adapted per the plan: input is a catalog profile dict
(brand/name/type/min_temp/max_temp/...), not a raw printer DB entry.
Field rules from Global Constraint 8 preserved: filament_id space-separated
md5, type removed, filament_density scalar, vendor/type arrays, .info with
empty sync_info/setting_id/base_id.
"""
import copy
import hashlib
import json
import time
from pathlib import Path


def generate_filament_id(name, db_id=""):
    """Generate a unique filament_id from name + DB id: MD5 hash, 'P' prefix, 14 chars.
    Including db_id prevents collisions when regenerating presets for the same filament name
    (e.g. after Cloud deletion + re-creation)."""
    h = hashlib.md5(f"{name} {db_id}".encode()).hexdigest()
    return "P" + h[:13]


# Catalog column -> OrcaSlicer preset key mapping for kvParam-style overrides.
# These catalog fields mirror kvParam entries in the printer DB and override
# the corresponding template values when building a standalone preset.
_KV_MAP = {
    "nozzle_temperature": "nozzle_temperature",
    "pa": "pressure_advance",
    "flow_ratio": "filament_flow_ratio",
    "max_volumetric": "filament_max_volumetric_speed",
    "retraction_length": "retraction_length",
    "retraction_speed": "retraction_speed",
}


def _kv_from_profile(profile_dict):
    """Build a kvParam-equivalent dict from catalog profile optional fields."""
    kv = {}
    for col, preset_key in _KV_MAP.items():
        v = profile_dict.get(col)
        if v is not None:
            kv[preset_key] = v
    return kv


def _parse_colors(colors):
    """Parse the colors field (JSON string or list) into a list."""
    if isinstance(colors, str):
        try:
            return json.loads(colors)
        except (json.JSONDecodeError, ValueError):
            return []
    if isinstance(colors, list):
        return colors
    return []


def build_standalone_preset(profile_dict, template):
    """Build a standalone preset from a catalog profile dict + system preset template.

    Uses the system preset as base (fan, retraction, plate temps, etc.),
    overrides identity + temperature fields from the catalog profile.
    """
    flat = copy.deepcopy(template)
    b = profile_dict
    kv = _kv_from_profile(profile_dict)

    # Strip system-specific fields (keep compatible_printers if template is a user preset)
    is_user_template = template.get("from") == "User"
    strip_keys = ["_path", "setting_id", "instantiation"]
    if not is_user_template:
        strip_keys += ["compatible_printers", "compatible_printers_condition"]
    for key in strip_keys:
        flat.pop(key, None)

    # Identity from catalog -- Rule 2: name already includes vendor, so use as-is
    # Preset fields that are arrays must stay arrays; scalar fields stay scalar.
    preset_name = b['name'] if b['name'].lower().startswith(b['brand'].lower()) else f"{b['brand']} {b['name']}"
    flat["name"] = preset_name
    flat["filament_id"] = b.get("filament_id") or generate_filament_id(preset_name, b.get("printer_db_id") or "")
    flat["filament_vendor"] = [b["brand"]]  # array in OrcaSlicer presets
    flat["filament_type"] = [b["type"]]  # array in OrcaSlicer presets
    flat["inherits"] = ""
    flat["from"] = "User"
    flat["filament_settings_id"] = [preset_name]
    flat.pop("type", None)  # user presets don't have 'type' field; OrcaSlicer infers from context

    # Temperature from catalog (base fields -- authoritative for printer firmware)
    flat["nozzle_temperature"] = [str(b["max_temp"])]
    flat["nozzle_temperature_range_low"] = [str(b["min_temp"])]
    flat["nozzle_temperature_range_high"] = [str(b["max_temp"])]

    # Density from catalog if available
    if b.get("density"):
        flat["filament_density"] = str(b["density"])

    # default_filament_colour -- OrcaSlicer expects hex color or empty string.
    # Catalog colors may be text names (e.g. "Midnight Black") -- use empty to avoid white swatch.
    colors = _parse_colors(b.get("colors", "[]"))
    if colors:
        c = colors[0]
        if c.startswith("#") and len(c) in (7, 4):
            flat["default_filament_colour"] = [c]
        else:
            flat["default_filament_colour"] = [""]
    else:
        flat["default_filament_colour"] = [""]

    # kvParam override -- copy all non-nil kvParam values from catalog to preset.
    # Preset fields are arrays of strings; catalog kvParam values are numbers/strings.
    # Only override keys that already exist in the preset (OrcaSlicer schema).
    # Skip identity/inheritance fields -- these are set explicitly above for standalone preset.
    SKIP_KEYS = {"inherits", "name", "filament_id", "filament_vendor", "filament_type",
                 "filament_settings_id", "type", "from",
                 "compatible_printers", "compatible_printers_condition",
                 "compatible_prints", "compatible_prints_condition",
                 "default_filament_colour"}
    # Scalar fields in OrcaSlicer presets (not arrays). kvParam override must
    # keep these scalar -- wrapping in [] would break OrcaSlicer parsing.
    SCALAR_KEYS = {"filament_density"}
    for k, v in kv.items():
        if v is None or v == "nil" or v == "":
            continue
        if k in SKIP_KEYS:
            continue
        if k in flat:
            flat[k] = str(v) if k in SCALAR_KEYS else [str(v)]

    # Version
    if "version" not in flat:
        flat["version"] = "2.4.0.3"

    return flat


def write_info_file(preset_path, *, setting_id=""):
    """Write a .info file alongside a preset JSON.
    OrcaSlicer uses key=value format (not JSON). Per Global Constraint 8,
    sync_info and base_id are always empty; setting_id defaults to empty.
    """
    info_path = preset_path.with_suffix(".info")
    # Extract user_id from path: user/<UUID>/filament/[base/]<name>.info
    user_id = ""
    try:
        parts = preset_path.parts
        if "user" in parts:
            idx = parts.index("user")
            if idx + 1 < len(parts):
                user_id = parts[idx + 1]
    except (ValueError, IndexError):
        pass
    lines = [
        "sync_info = ",
        f"user_id = {user_id}",
        f"setting_id = {setting_id}",
        "base_id = ",
        f"updated_time = {int(time.time())}",
    ]
    info_path.write_text("\n".join(lines) + "\n")
    return info_path
