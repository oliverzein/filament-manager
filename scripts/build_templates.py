#!/usr/bin/env python3
"""build_templates.py — Extract, deduplicate, and index OrcaSlicer @base filament presets.

Scans /opt/orca-slicer/resources/profiles for @base filament presets,
deduplicates by display name (preferring vendor-specific copies over
OrcaFilamentLibrary copies), copies them to fm/presets/templates/,
and generates fm/presets/template_index.json with vendor + type metadata.

Run once during development. Generated files are committed to git.
"""
import json
import shutil
import sys
from pathlib import Path

# Add project root to path for imports
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fm.presets.flattener import flatten_preset, _TYPE_TO_TEMPLATE, _BUNDLED_DIR

ORCA_PROFILES = Path("/opt/orca-slicer/resources/profiles")
TEMPLATES_DIR = _BUNDLED_DIR  # fm/presets/templates/
INDEX_PATH = TEMPLATES_DIR.parent / "template_index.json"

KNOWN_BRANDS = {
    "Snapmaker", "Anker", "Bambu", "Creality", "Polymaker", "SUNLU",
    "eSUN", "Overture", "Elegoo", "Cubicon", "Flashforge", "FusRock",
    "Numakers", "COEX", "FILL3D", "FDplast", "DREMC", "Elas", "FilAr",
    "NIT", "Valment", "GreenGate3D", "BETA", "AliZ", "OrcaArena",
}


def find_base_presets():
    """Find all @base filament presets, deduplicated by display name.
    Returns dict: display_name -> Path (preferring vendor-specific copy)."""
    presets = {}
    ofl_dir = ORCA_PROFILES / "OrcaFilamentLibrary" / "filament"

    for f in sorted(ORCA_PROFILES.rglob("*@base.json")):
        if "/filament/" not in str(f):
            continue
        display_name = f.stem.replace(" @base", "")
        # Prefer non-OFL copies (vendor-specific dirs have richer data)
        is_ofl = ofl_dir in f.parents
        if display_name not in presets:
            presets[display_name] = f
        elif is_ofl and display_name in presets:
            # Keep existing non-OFL copy
            pass
        elif not is_ofl:
            # Replace OFL copy with vendor-specific copy
            presets[display_name] = f

    return presets


def resolve_vendor(flat, display_name):
    """Extract vendor from flattened preset, or derive from name prefix."""
    fv = flat.get("filament_vendor")
    if isinstance(fv, list) and fv:
        return fv[0]
    if isinstance(fv, str) and fv:
        return fv
    # Derive from name prefix
    parts = display_name.split()
    if parts and parts[0] in KNOWN_BRANDS:
        if parts[0] == "Bambu":
            return "Bambu Lab"
        return parts[0]
    return "Generic"


def resolve_type(flat):
    """Extract filament_type from flattened preset."""
    ft = flat.get("filament_type")
    if isinstance(ft, list) and ft:
        return ft[0]
    if isinstance(ft, str) and ft:
        return ft
    return ""


def main():
    if not ORCA_PROFILES.exists():
        print(f"ERROR: OrcaSlicer profiles not found at {ORCA_PROFILES}", file=sys.stderr)
        sys.exit(1)

    presets = find_base_presets()
    print(f"Found {len(presets)} unique @base presets")

    templates = []

    # Copy @base files and build index entries
    for display_name, src_path in sorted(presets.items()):
        dest = TEMPLATES_DIR / f"{display_name} @base.json"
        shutil.copy2(src_path, dest)

        # Flatten to resolve vendor + type
        try:
            flat = flatten_preset(dest)
        except Exception as e:
            print(f"WARN: failed to flatten {display_name}: {e}", file=sys.stderr)
            continue

        vendor = resolve_vendor(flat, display_name)
        ftype = resolve_type(flat)

        if not ftype:
            print(f"WARN: no filament_type for {display_name}", file=sys.stderr)
            continue

        templates.append({
            "name": display_name,
            "vendor": vendor,
            "type": ftype,
            "file": f"{display_name} @base.json",
        })

    # Add existing fdm_filament_*.json as Generic templates
    for ftype, filename in sorted(_TYPE_TO_TEMPLATE.items()):
        # Skip if an @base-derived entry already uses this exact name
        # (e.g. "Generic PLA Silk" @base has type PLA, fdm key "PLA Silk"
        # would collide on name — keep the @base entry, drop the fdm one).
        generic_name = f"Generic {ftype}"
        if any(t["name"] == generic_name for t in templates):
            continue
        templates.append({
            "name": generic_name,
            "vendor": "Generic",
            "type": ftype,
            "file": filename,
        })

    # Sort: Generic first, then by vendor, then by name
    templates.sort(key=lambda t: (t["vendor"] != "Generic", t["vendor"], t["name"]))

    index = {"templates": templates}
    INDEX_PATH.write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"Wrote {len(templates)} templates to {INDEX_PATH}")
    print(f"Files in {TEMPLATES_DIR}: {len(list(TEMPLATES_DIR.glob('*.json')))}")


if __name__ == "__main__":
    main()
