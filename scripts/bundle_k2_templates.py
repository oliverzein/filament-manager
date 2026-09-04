#!/usr/bin/env python3
"""bundle_k2_templates.py — Copy K2 printer filament templates to fm/presets/templates/.

Copies all *@K2-all.json presets and their referenced parent presets from
/opt/orca-slicer/resources/profiles/Creality/filament/ into fm/presets/templates/
so Docker deployments have all K2 system presets available for flattening.
"""
import shutil
from pathlib import Path

SOURCE_DIR = Path("/opt/orca-slicer/resources/profiles/Creality/filament")
TARGET_DIR = Path(__file__).resolve().parent.parent / "fm" / "presets" / "templates"


def bundle_k2():
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    # Copy all @K2-all.json files
    for f in SOURCE_DIR.glob("*@K2-all.json"):
        shutil.copy2(f, TARGET_DIR / f.name)
        count += 1
    # Also copy referenced generic parents like Creality Generic PLA.json if present
    for f in SOURCE_DIR.glob("Creality Generic *.json"):
        shutil.copy2(f, TARGET_DIR / f.name)
        count += 1
    print(f"Bundled {count} K2 templates to {TARGET_DIR}")


if __name__ == "__main__":
    bundle_k2()
