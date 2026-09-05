#!/usr/bin/env python3
"""bundle_system_templates.py — Copy OrcaSlicer @System filament presets to fm/presets/templates/.

OrcaSlicer 2.x writes ``inherits: "SUNLU PETG @System"`` when a user creates
a preset based on a system filament.  The bundled templates directory previously
only shipped ``@base.json`` files (356 of them) but zero ``@System.json`` files,
so ``find_preset_by_name("SUNLU PETG @System")`` returned ``None`` and
``cache_sync`` fell back to ``vendor="Generic"`` / ``type="PLA"``.

This script copies every ``*@System.json`` file from the OrcaSlicer
OrcaFilamentLibrary into ``fm/presets/templates/`` so Docker deployments and
CI environments without a local OrcaSlicer install can still flatten presets
that inherit from ``@System`` templates.

It also copies a handful of missing parent base files that some ``@System``
presets reference but that are not part of the standard
``OrcaFilamentLibrary/base`` set.
"""
import shutil
from pathlib import Path

# Primary source: user-installed OrcaSlicer system profiles.
HOME_SRC = Path.home() / ".config" / "OrcaSlicer" / "system" / "OrcaFilamentLibrary" / "filament"

# Secondary source: system-wide OrcaSlicer profiles (Snapmaker extras, etc.).
OPT_SRC = Path("/opt/orca-slicer/resources/profiles")

TARGET_DIR = Path(__file__).resolve().parent.parent / "fm" / "presets" / "templates"

# Missing parent files that some @System presets inherit from but that are not
# in the standard OrcaFilamentLibrary/base directory.  Each tuple is
# (filename, [search_roots]).
MISSING_PARENTS = [
    ("fdm_filament_breakaway.json", [OPT_SRC / "Snapmaker" / "filament"]),
    ("fdm_filament_pla_eco.json", [OPT_SRC / "Snapmaker" / "filament"]),
    ("Snapmaker J1 PVA.json", [OPT_SRC / "Snapmaker" / "filament"]),
]


def bundle_system_templates():
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    count = 0

    # 1. Copy all *@System.json files from the OrcaFilamentLibrary.
    if HOME_SRC.exists():
        for f in HOME_SRC.rglob("*@System.json"):
            dest = TARGET_DIR / f.name
            if not dest.exists():
                shutil.copy2(f, dest)
                count += 1
        print(f"Copied {count} @System templates from {HOME_SRC}")
    else:
        print(f"WARN: source directory not found: {HOME_SRC}")

    # 2. Copy missing parent base files.
    parent_count = 0
    for filename, roots in MISSING_PARENTS:
        dest = TARGET_DIR / filename
        if dest.exists():
            continue
        for root in roots:
            candidate = root / filename
            if candidate.exists():
                shutil.copy2(candidate, dest)
                parent_count += 1
                break
        else:
            print(f"WARN: missing parent not found in any root: {filename}")
    print(f"Copied {parent_count} missing parent files")
    count += parent_count

    print(f"Total files copied: {count}")
    print(f"Templates directory now has {len(list(TARGET_DIR.glob('*.json')))} files")


if __name__ == "__main__":
    bundle_system_templates()
