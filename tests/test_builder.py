# tests/test_builder.py
import hashlib
import json

import pytest

from fm.presets import builder
from fm.presets import flattener


# === Fixtures ===

@pytest.fixture
def profile_dict():
    """Sunlu PLA+ catalog profile (mirrors skill mock_db entry 99001)."""
    return {
        "id": 1,
        "brand": "Sunlu",
        "name": "Sunlu PLA+",
        "type": "PLA",
        "colors": '["#ffffff"]',
        "min_temp": 205,
        "max_temp": 215,
        "density": 1.23,
        "drying_temp": 50,
        "drying_time": 8,
        "pa": 0.032,
        "flow_ratio": 0.998,
        "max_volumetric": 10,
        "retraction_length": None,
        "retraction_speed": None,
        "nozzle_temperature": 215,
        "printer_db_id": "99001",
        "filament_id": None,
        "notes": "",
    }


@pytest.fixture
def profile_hyper():
    """Creality Hyper PLA catalog profile (mirrors skill mock_db entry 01001)."""
    return {
        "id": 2,
        "brand": "Creality",
        "name": "Hyper PLA",
        "type": "PLA",
        "colors": '["#ffffff"]',
        "min_temp": 190,
        "max_temp": 240,
        "density": 1.24,
        "drying_temp": 0,
        "drying_time": 0,
        "pa": 0.02,
        "flow_ratio": 1.0,
        "max_volumetric": 10,
        "retraction_length": None,
        "retraction_speed": None,
        "nozzle_temperature": 220,
        "printer_db_id": "01001",
        "filament_id": None,
        "notes": "",
    }


@pytest.fixture
def user_template():
    """A minimal user-from template."""
    return {
        "name": "Generic PLA @K2",
        "filament_type": "PLA",
        "from": "User",
        "fan_min_speed": ["30"],
        "nozzle_temperature": ["220"],
        "version": "2.4.0.3",
    }


# === generate_filament_id ===

def test_filament_id_uses_space_separator():
    assert builder.generate_filament_id("Sunlu PLA+", "99001") == \
           "P" + hashlib.md5("Sunlu PLA+ 99001".encode()).hexdigest()[:13]


def test_generates_id_with_p_prefix():
    fid = builder.generate_filament_id("Creality Hyper PLA Optimized")
    assert fid.startswith("P")
    assert len(fid) == 14  # P + 13 chars


def test_generates_different_ids_for_different_names():
    fid1 = builder.generate_filament_id("Creality Hyper PLA")
    fid2 = builder.generate_filament_id("eSUN PETG Basic")
    assert fid1 != fid2


def test_generates_same_id_for_same_name():
    fid1 = builder.generate_filament_id("Sunlu PLA+")
    fid2 = builder.generate_filament_id("Sunlu PLA+")
    assert fid1 == fid2


# === build_standalone_preset ===

def test_generated_preset_has_no_type_field(profile_dict, user_template):
    out = builder.build_standalone_preset(profile_dict, user_template)
    assert "type" not in out
    assert out["inherits"] == "" and out["from"] == "User"
    assert out["filament_vendor"] == ["Sunlu"]      # array
    assert out["filament_density"] == "1.23"        # scalar string


def test_builds_preset_with_correct_identity(profile_dict):
    system_preset = {
        "name": "Generic PLA @K2",
        "filament_type": "PLA",
        "fan_min_speed": ["30"],
        "nozzle_temperature": ["220"],
        "version": "2.4.0.3",
    }
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["name"] == "Sunlu PLA+"  # name already has vendor prefix (Rule 2)
    assert flat["filament_id"].startswith("P")
    assert flat["inherits"] == ""
    assert flat["from"] == "User"
    assert flat["filament_vendor"] == ["Sunlu"]  # array in OrcaSlicer presets
    assert flat["filament_type"] == ["PLA"]  # array in OrcaSlicer presets
    assert "type" not in flat  # user presets don't have type field


def test_temperatures_from_db(profile_dict):
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["nozzle_temperature"] == ["215"]
    assert flat["nozzle_temperature_range_low"] == ["205"]
    assert flat["nozzle_temperature_range_high"] == ["215"]


def test_density_from_db(profile_dict):
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["filament_density"] == "1.23"


def test_strips_system_specific_fields(profile_hyper):
    system_preset = {
        "name": "Generic PLA",
        "filament_type": "PLA",
        "setting_id": "abc123",
        "instantiation": "user",
        "compatible_printers": ["K2"],
    }
    flat = builder.build_standalone_preset(profile_hyper, system_preset)
    assert "setting_id" not in flat
    assert "instantiation" not in flat
    assert "compatible_printers" not in flat


def test_preserves_system_preset_fields(profile_hyper):
    system_preset = {
        "name": "Generic PLA",
        "filament_type": "PLA",
        "fan_min_speed": ["30"],
        "fan_max_speed": ["100"],
        "filament_flow_ratio": ["0.98"],
    }
    flat = builder.build_standalone_preset(profile_hyper, system_preset)
    assert flat["fan_min_speed"] == ["30"]
    assert flat["fan_max_speed"] == ["100"]
    # kvParam overrides template values — profile has flow_ratio=1.0
    assert flat["filament_flow_ratio"] == ["1.0"]


def test_filament_settings_id_matches_name(profile_dict):
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["filament_settings_id"] == [flat["name"]]


def test_default_filament_colour_empty_for_non_hex(profile_dict):
    profile_dict["colors"] = '["Midnight Black"]'
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["default_filament_colour"] == [""]


def test_default_filament_colour_hex_passthrough(profile_dict):
    profile_dict["colors"] = '["#ff0000"]'
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["default_filament_colour"] == ["#ff0000"]


def test_user_template_keeps_compatible_printers(profile_dict):
    system_preset = {
        "name": "Generic PLA",
        "filament_type": "PLA",
        "from": "User",
        "compatible_printers": ["Creality K2"],
    }
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    assert flat["compatible_printers"] == ["Creality K2"]


def test_filament_id_uses_db_id(profile_dict):
    system_preset = {"name": "Generic PLA", "filament_type": "PLA"}
    flat = builder.build_standalone_preset(profile_dict, system_preset)
    expected = "P" + hashlib.md5("Sunlu PLA+ 99001".encode()).hexdigest()[:13]
    assert flat["filament_id"] == expected


# === write_info_file ===

def test_info_file_empty_sync_info(tmp_path):
    p = tmp_path / "X.json"
    p.write_text("{}")
    builder.write_info_file(p)
    text = p.with_suffix(".info").read_text()
    assert "sync_info = \n" in text and "setting_id = \n" in text


def test_writes_info_file(tmp_path):
    preset_path = tmp_path / "test_preset.json"
    preset_path.write_text("{}")
    info_path = builder.write_info_file(preset_path)
    assert info_path.exists()
    # .info is key=value format (not JSON)
    content = info_path.read_text()
    lines = dict(line.split(" = ", 1) for line in content.strip().split("\n"))
    assert lines["sync_info"] == ""
    assert lines["setting_id"] == ""
    assert lines["base_id"] == ""
    assert "updated_time" in lines


def test_info_path_is_correct(tmp_path):
    preset_path = tmp_path / "My Preset.json"
    preset_path.write_text("{}")
    info_path = builder.write_info_file(preset_path)
    assert info_path == preset_path.with_suffix(".info")


def test_info_file_setting_id_override(tmp_path):
    preset_path = tmp_path / "X.json"
    preset_path.write_text("{}")
    builder.write_info_file(preset_path, setting_id="P123")
    content = preset_path.with_suffix(".info").read_text()
    assert "setting_id = P123\n" in content


# === flattener: find_preset_by_name ===

def test_finds_preset_in_hint_dir(tmp_path):
    preset = tmp_path / "MyPreset.json"
    preset.write_text("{}")
    found = flattener.find_preset_by_name("MyPreset", hint_dir=tmp_path)
    assert found == preset


def test_returns_none_when_not_found(tmp_path):
    assert flattener.find_preset_by_name("NoSuch", hint_dir=tmp_path) is None


# === flattener: flatten_preset ===

def test_standalone_preset_strips_inheritance_keys(tmp_path):
    path = tmp_path / "standalone.json"
    path.write_text(json.dumps({
        "name": "Generic PLA",
        "nozzle_temperature": ["220"],
        "inherits": "",
        "setting_id": "x",
        "instantiation": "user",
    }))
    flat = flattener.flatten_preset(path)
    assert flat["name"] == "Generic PLA"
    assert "inherits" not in flat
    assert "setting_id" not in flat
    assert "instantiation" not in flat


def test_inherits_merges_parent_with_child_override(tmp_path):
    parent = tmp_path / "Parent.json"
    parent.write_text(json.dumps({
        "name": "Parent",
        "nozzle_temperature": ["200"],
        "filament_type": ["PLA"],
        "inherits": "",
    }))
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({
        "name": "Child",
        "inherits": "Parent",
        "nozzle_temperature": ["220"],
    }))
    flat = flattener.flatten_preset(child)
    assert flat["name"] == "Child"
    assert flat["nozzle_temperature"] == ["220"]
    assert flat["filament_type"] == ["PLA"]
    assert "inherits" not in flat


def test_missing_parent_prints_warning(tmp_path, capsys):
    child = tmp_path / "Child.json"
    child.write_text(json.dumps({
        "name": "Child",
        "inherits": "Missing",
    }))
    flat = flattener.flatten_preset(child)
    assert flat["name"] == "Child"
    captured = capsys.readouterr()
    assert "WARN: parent 'Missing' not found" in captured.err
