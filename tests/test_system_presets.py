"""Tests for @System preset bundling and flattener fallback logic.

Verifies that:
  * @System templates are bundled and resolvable.
  * find_preset_by_name falls back from @System to @base.
  * flatten_preset resolves multi-level inheritance (User -> @System -> @base -> fdm_filament_*).
  * Slash normalisation handles upstream naming inconsistencies.
"""
import json
from pathlib import Path

import pytest

from fm.presets.flattener import (
    find_preset_by_name,
    flatten_preset,
    _BUNDLED_DIR,
)


# ---------------------------------------------------------------------------
# @System templates are bundled
# ---------------------------------------------------------------------------

def test_system_templates_exist_in_bundled_dir():
    """The @System templates must be shipped alongside the @base templates."""
    assert (_BUNDLED_DIR / "SUNLU PETG @System.json").exists()
    assert (_BUNDLED_DIR / "Bambu ABS @System.json").exists()


def test_missing_parent_files_are_bundled():
    """Parent files referenced by some @System presets must be present."""
    assert (_BUNDLED_DIR / "fdm_filament_breakaway.json").exists()
    assert (_BUNDLED_DIR / "fdm_filament_pla_eco.json").exists()
    assert (_BUNDLED_DIR / "Snapmaker J1 PVA.json").exists()


# ---------------------------------------------------------------------------
# find_preset_by_name — @System resolution
# ---------------------------------------------------------------------------

def test_find_preset_by_name_resolves_system_preset():
    """SUNLU PETG @System must resolve to a real file."""
    path = find_preset_by_name("SUNLU PETG @System")
    assert path is not None
    assert path.exists()
    assert path.name == "SUNLU PETG @System.json"


def test_find_preset_by_name_system_flattens_vendor_and_type():
    """Flattening SUNLU PETG @System yields vendor=SUNLU, type=PETG."""
    path = find_preset_by_name("SUNLU PETG @System")
    assert path is not None

    flat = flatten_preset(path)
    assert flat.get("filament_vendor") in (["SUNLU"], "SUNLU")
    assert flat.get("filament_type") in (["PETG"], "PETG")


# ---------------------------------------------------------------------------
# find_preset_by_name — @System -> @base fallback
# ---------------------------------------------------------------------------

def test_find_preset_by_name_system_falls_back_to_base():
    """When an @System file is missing, find_preset_by_name falls back to @base.

    We create a temporary @base file in a tmp hint_dir and verify that looking
    up the corresponding @System name finds the @base file.
    """
    tmp = Path(__file__).parent / "fixtures" / "tmp_system_fallback"
    tmp.mkdir(parents=True, exist_ok=True)
    base_file = tmp / "NonExistent @base.json"
    base_file.write_text(json.dumps({
        "name": "NonExistent @base",
        "inherits": "fdm_filament_pet",
        "filament_vendor": ["TestVendor"],
    }))

    try:
        path = find_preset_by_name("NonExistent @System", hint_dir=tmp)
        assert path is not None
        assert path.name == "NonExistent @base.json"
    finally:
        base_file.unlink(missing_ok=True)
        tmp.rmdir()


def test_find_preset_by_name_nonexistent_system_returns_none():
    """A truly non-existent @System name (no @base either) returns None."""
    path = find_preset_by_name("ZZZ NonExistent @System")
    assert path is None


# ---------------------------------------------------------------------------
# find_preset_by_name — slash normalisation
# ---------------------------------------------------------------------------

def test_find_preset_by_name_slash_to_space():
    """Bambu Support For PA/PET @base resolves via space-normalised filename."""
    path = find_preset_by_name("Bambu Support For PA/PET @base")
    assert path is not None
    assert path.name == "Bambu Support For PA PET @base.json"


def test_find_preset_by_name_slash_to_hyphen():
    """Bambu Support For PLA/PETG @base resolves via hyphen-normalised filename."""
    path = find_preset_by_name("Bambu Support For PLA/PETG @base")
    assert path is not None
    assert path.name == "Bambu Support For PLA-PETG @base.json"


# ---------------------------------------------------------------------------
# flatten_preset — multi-level inheritance
# ---------------------------------------------------------------------------

def test_flatten_preset_multi_level_system_inheritance():
    """Flatten a preset that inherits @System which inherits @base which
    inherits fdm_filament_*.

    Chain: SUNLU PETG @System -> SUNLU PETG @base -> fdm_filament_pet
    -> fdm_filament_common
    """
    path = find_preset_by_name("SUNLU PETG @System")
    assert path is not None

    flat = flatten_preset(path)
    # vendor comes from @base, type comes from fdm_filament_pet
    assert flat.get("filament_vendor") in (["SUNLU"], "SUNLU")
    assert flat.get("filament_type") in (["PETG"], "PETG")
    # fdm_filament_common provides filament_density etc.
    assert "filament_density" in flat


def test_flatten_preset_user_inherits_system(tmp_path):
    """Simulate a user preset that inherits an @System preset.

    User -> SUNLU PETG @System -> SUNLU PETG @base -> fdm_filament_pet
    """
    user_preset = tmp_path / "My SUNLU PETG Optimized.json"
    user_preset.write_text(json.dumps({
        "name": "My SUNLU PETG Optimized",
        "inherits": "SUNLU PETG @System",
        "nozzle_temperature": ["245"],
    }))

    flat = flatten_preset(user_preset)
    # vendor and type resolved through the inheritance chain
    assert flat.get("filament_vendor") in (["SUNLU"], "SUNLU")
    assert flat.get("filament_type") in (["PETG"], "PETG")
    # user override applied
    assert flat.get("nozzle_temperature") in (["245"], "245", 245)
