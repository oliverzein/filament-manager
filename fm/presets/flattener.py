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


SYS_PROFILES = Path("/opt/orca-slicer/resources/profiles")


def find_preset_by_name(name, hint_dir=None):
    """Find a preset JSON by name. Prefer hint_dir, then system profiles."""
    if not name:
        return None
    candidates = []
    if hint_dir:
        p = hint_dir / f"{name}.json"
        if p.exists():
            candidates.append(p)
    base_dir = SYS_PROFILES / "OrcaFilamentLibrary" / "filament" / "base"
    if base_dir.exists():
        p = base_dir / f"{name}.json"
        if p.exists() and p not in candidates:
            candidates.append(p)
    for p in SYS_PROFILES.rglob(f"{name}.json"):
        if p not in candidates:
            candidates.append(p)
    return candidates[0] if candidates else None


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


def find_system_preset(filament_type, hint_name=None):
    """Find a system preset to use as template for the given filament type.

    Searches /opt/orca-slicer/resources/profiles for a filament preset matching
    the given type. Prefers generic/base presets.
    """
    if not SYS_PROFILES.exists():
        return None
    candidates = []
    for p in SYS_PROFILES.rglob("filament/*.json"):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("type") != "filament":
            continue
        ft = d.get("filament_type")
        if isinstance(ft, list) and ft:
            ft = ft[0]
        if ft != filament_type:
            continue
        d["_path"] = str(p)
        candidates.append(d)
    if not candidates:
        return None
    # Prefer presets with "Generic" in name, then "base"
    generic = [c for c in candidates if "generic" in c.get("name", "").lower()]
    if generic:
        return generic[0]
    base = [c for c in candidates if "base" in c.get("name", "").lower()]
    if base:
        return base[0]
    return candidates[0]
