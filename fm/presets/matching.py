# fm/presets/matching.py
"""matching.py — OrcaSlicer preset matching.

Lifted verbatim from skill/orca.py (load_presets, find_orca_user_dir,
get_filament_type, get_filament_id, match_presets, get_preset_temp,
is_orcaslicer_running) per the plan's lift rule.

Scoring (+20 brand, +10 vendor, type hard filter) and the score<30 threshold
are copied unchanged from the skill.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from fm.presets.flattener import SYS_PROFILES

ORCA_CONFIG_DIR = Path(os.path.expanduser("~/.config/OrcaSlicer"))


# === Preset discovery ===

def find_orca_user_dir(config_dir=None):
    """Find the user filament directory under OrcaSlicer config.

    OrcaSlicer stores user presets in ~/.config/OrcaSlicer/user/<UUID>/filament/.
    There can be multiple profile UUIDs — pick the one that has a filament/ subdir.
    """
    base = Path(os.path.expanduser(str(config_dir))) if config_dir else ORCA_CONFIG_DIR
    user_root = base / "user"
    if not user_root.exists():
        return None
    # Find first UUID dir with a filament/ subdir
    for d in sorted(user_root.iterdir()):
        filament_dir = d / "filament"
        if filament_dir.is_dir():
            return filament_dir
    return None


def load_presets(config_dir=None):
    """Load all system + user filament presets."""
    base = Path(os.path.expanduser(str(config_dir))) if config_dir else ORCA_CONFIG_DIR
    presets = []
    # System presets
    if SYS_PROFILES.exists():
        for p in SYS_PROFILES.rglob("filament/*.json"):
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if d.get("type") != "filament":
                continue
            d["_system"] = True
            d["_path"] = str(p)
            presets.append(d)
    # User presets — use the first user profile with a filament/ subdir (mirrors
    # find_orca_user_dir) and only direct .json files inside it. This avoids
    # duplicates from multiple profiles and manual backup subdirs.
    filament_dir = find_orca_user_dir(str(base))
    if filament_dir and filament_dir.is_dir():
        for p in filament_dir.glob("*.json"):
            try:
                d = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            d.setdefault("type", "filament")
            if d.get("type") != "filament":
                continue
            d["_system"] = False
            d["_path"] = str(p)
            presets.append(d)
    return presets


# === Field accessors ===

def get_filament_type(d):
    ft = d.get("filament_type")
    if isinstance(ft, list) and ft:
        return ft[0]
    if isinstance(ft, str):
        return ft
    return ""


def get_filament_id(d):
    fid = d.get("filament_id")
    if isinstance(fid, list) and fid:
        return fid[0]
    if isinstance(fid, str):
        return fid
    return "?(inherited)"


# === Matching ===

def match_presets(presets, vendor, brand_name, base_type):
    """Simulate CrealityPrintAgent::match_filament_preset scoring."""
    v_low = vendor.lower()
    b_low = brand_name.lower()
    t_low = base_type.lower()
    matches = []
    considered = 0
    for p in presets:
        considered += 1
        pt = get_filament_type(p).lower()
        if pt != t_low:
            continue
        name_low = p.get("name", "").lower()
        score = 0
        if b_low and b_low in name_low:
            score += 20
        if v_low and v_low in name_low:
            score += 10
        if score > 0:
            matches.append({
                "name": p["name"],
                "score": score,
                "system": p["_system"],
                "fid": get_filament_id(p),
                "path": p["_path"],
            })
    matches.sort(key=lambda x: (-x["score"], x["system"] is False))
    return matches, considered


# === Field helpers ===

def get_preset_temp(preset, key, default=None):
    """Return preset[key] as int, or default if missing/non-numeric.

    Preset fields are arrays of strings; return first element.
    """
    value = preset.get(key, default)
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

# === OrcaSlicer process guard ===

def is_orcaslicer_running():
    """Check if an OrcaSlicer process is currently running."""
    names = ("OrcaSlicer", "orca-slicer", "orca-slicer.AppImage")
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] in names:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except ImportError:
        pass
    for name in names:
        try:
            result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
            if result.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False
