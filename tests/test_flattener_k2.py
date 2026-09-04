import pytest
from pathlib import Path
from fm.presets.flattener import flatten_preset, find_preset_by_name, _BUNDLED_DIR


def test_k2_system_templates_exist_in_bundled_dir():
    assert (_BUNDLED_DIR / "Hyper Stardust @K2-all.json").exists()
    assert (_BUNDLED_DIR / "EN-PLA+ @K2-all.json").exists()
    assert (_BUNDLED_DIR / "Creality Generic PLA @K2-all.json").exists()


def test_hyper_stardust_multi_level_flattening():
    template_path = find_preset_by_name("Hyper Stardust @K2-all")
    assert template_path is not None

    flat = flatten_preset(template_path)
    # Check fields inherited through Hyper Stardust @K2-all -> fdm_filament_pla -> fdm_filament_common
    assert flat.get("filament_vendor") in (["Creality"], "Creality")
    assert flat.get("filament_type") in (["PLA"], "PLA")
    assert flat.get("nozzle_temperature") in (["220"], "220", 220)
    assert flat.get("filament_density") in (["1.24"], "1.24", 1.24)
    assert flat.get("nozzle_temperature_range_high") in (["240"], "240", 240)


def test_en_pla_plus_multi_level_flattening():
    template_path = find_preset_by_name("EN-PLA+ @K2-all")
    assert template_path is not None

    flat = flatten_preset(template_path)
    assert flat.get("filament_vendor") in (["Creality"], "Creality")
    assert flat.get("filament_type") in (["PLA"], "PLA")
    assert flat.get("nozzle_temperature") in (["220"], "220", 220)
    assert flat.get("filament_density") in (["1.24"], "1.24", 1.24)
    assert flat.get("filament_flow_ratio") in (["0.98"], "0.98", 0.98)
