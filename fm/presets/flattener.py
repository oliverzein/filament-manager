# fm/presets/flattener.py
"""flattener.py — Shared helpers for flattening OrcaSlicer filament presets.

Lifted from skill/preset_utils.py (SYS_PROFILES, find_preset_by_name,
flatten_preset) and skill/orca.py (find_system_preset). Verbatim per the
lift rule; no behaviour changes.
"""
import copy
import json
import sys
from pathlib import Path


# Bundled base templates (shipped with the app).
_BUNDLED_DIR = Path(__file__).parent / "templates"

# Deprecated: OrcaSlicer system profiles path. No longer used for template
# selection (bundled templates are used instead). Kept for backward compat
# with matching.py which imports it for load_presets().
SYS_PROFILES = Path("/opt/orca-slicer/resources/profiles")


def find_preset_by_name(name, hint_dir=None):
    """Find a preset JSON by name. Prefer hint_dir, then bundled templates."""
    if not name:
        return None
    if hint_dir:
        p = hint_dir / f"{name}.json"
        if p.exists():
            return p
    p = _BUNDLED_DIR / f"{name}.json"
    if p.exists():
        return p
    return None


def flatten_preset(path, _seen=None):
    """Recursively flatten a preset: parent fields as base, child overrides on top."""
    if _seen is None:
        _seen = set()
    data = json.loads(Path(path).read_text())
    parent_name = data.get("inherits", "").strip()
    hint = Path(path).parent
    if parent_name and parent_name not in _seen:
        _seen.add(parent_name)
        parent_path = find_preset_by_name(parent_name, hint_dir=hint)
        if parent_path:
            parent_flat = flatten_preset(parent_path, _seen)
            merged = copy.deepcopy(parent_flat)
            for k, v in data.items():
                if k in ("inherits", "setting_id", "instantiation"):
                    continue
                merged[k] = copy.deepcopy(v)
            return merged
        else:
            print(f"WARN: parent '{parent_name}' not found", file=sys.stderr)
    data.pop("inherits", None)
    data.pop("setting_id", None)
    data.pop("instantiation", None)
    return data


# Map filament type -> bundled template filename.
_TYPE_TO_TEMPLATE = {
    "PLA": "fdm_filament_pla.json",
    "PLA-Silk": "fdm_filament_pla_silk.json",
    "PLA Silk": "fdm_filament_pla_silk.json",
    "PETG": "fdm_filament_pet.json",
    "ABS": "fdm_filament_abs.json",
    "ASA": "fdm_filament_asa.json",
    "PA": "fdm_filament_pa.json",
    "PAHT": "fdm_filament_paht.json",
    "PC": "fdm_filament_pc.json",
    "PCTG": "fdm_filament_pctg.json",
    "TPU": "fdm_filament_tpu.json",
    "PVA": "fdm_filament_pva.json",
    "HIPS": "fdm_filament_hips.json",
    "PE": "fdm_filament_pe.json",
    "PP": "fdm_filament_pp.json",
    "PPA": "fdm_filament_ppa.json",
    "PPS": "fdm_filament_pps.json",
    "PHA": "fdm_filament_pha.json",
    "EVA": "fdm_filament_eva.json",
    "BVOH": "fdm_filament_bvoh.json",
    "COPE": "fdm_filament_cope.json",
    "SBS": "fdm_filament_sbs.json",
}


def template_name_for_type(filament_type: str) -> str | None:
    """Return the template filename for the given filament type, or None."""
    return _TYPE_TO_TEMPLATE.get(filament_type)


def find_system_preset(filament_type, hint_name=None):
    """Find a base template for the given filament type.

    Uses bundled templates first (shipped with the app), then falls back
    to OrcaSlicer system profiles if available.  Bundled templates are
    the OrcaFilamentLibrary base presets (fdm_filament_*.json) which
    provide sane defaults for each filament type.
    """
    # 1. Try bundled template (deterministic, no filesystem scan).
    filename = _TYPE_TO_TEMPLATE.get(filament_type)
    if filename:
        p = _BUNDLED_DIR / filename
        if p.exists():
            return flatten_preset(p)

    return None
