# tests/test_dbcodec.py
# Ported from skill-repo tests/test_db.py, tests/test_validate.py, tests/test_build_entry.py.
# Adaptations: import cfs -> from fm.printer import dbcodec as dc;
#               SystemExit -> FmError; cfs.EXIT_DB -> dc.EXIT_DB.
import json

import pytest

from fm.errors import FmError
from fm.printer import dbcodec as dc


# --- load_db / save_db ---

def test_load_db_valid(db_file):
    db = dc.load_db(str(db_file))
    assert db["result"]["count"] == 2


def test_load_db_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(FmError) as exc:
        dc.load_db(str(p))
    assert exc.value.code == dc.EXIT_DB


def test_load_db_missing(tmp_path):
    p = tmp_path / "nonexistent.json"
    with pytest.raises(FmError) as exc:
        dc.load_db(str(p))
    assert exc.value.code == dc.EXIT_DB


def test_save_db(tmp_path, mock_db):
    p = tmp_path / "out.json"
    dc.save_db(str(p), mock_db)
    with open(p) as f:
        loaded = json.load(f)
    assert loaded == mock_db


# --- find_entry / find_custom_entries ---

def test_find_entry_by_id(mock_db):
    e = dc.find_entry(mock_db, "01001")
    assert e["base"]["name"] == "Hyper PLA"


def test_find_entry_not_found(mock_db):
    assert dc.find_entry(mock_db, "99999") is None


def test_find_custom_entries(mock_db):
    custom = dc.find_custom_entries(mock_db)
    assert len(custom) == 1
    assert custom[0]["base"]["id"] == "99001"


def test_find_custom_entries_empty():
    db = {"result": {"list": [], "count": 0, "version": 1}}
    assert dc.find_custom_entries(db) == []


# --- next_free_id ---

def test_next_free_id_empty_db():
    db = {"result": {"list": [], "count": 0, "version": 1}}
    assert dc.next_free_id(db, 99001) == 99001


def test_next_free_id_with_entries(mock_db):
    assert dc.next_free_id(mock_db, 99001) == 99002


def test_next_free_id_gap(mock_db):
    # add 99003, expect 99002 still free
    mock_db["result"]["list"].append({"base": {"id": "99003"}})
    assert dc.next_free_id(mock_db, 99001) == 99002


# --- insert_entry ---

def test_insert_entry(mock_db):
    entry = {"base": {"id": "99002", "brand": "eSun", "name": "eSun PLA+"}}
    dc.insert_entry(mock_db, entry)
    assert mock_db["result"]["count"] == 3
    assert dc.find_entry(mock_db, "99002") is not None


def test_insert_entry_duplicate_id(mock_db):
    entry = {"base": {"id": "99001"}}
    with pytest.raises(FmError) as exc:
        dc.insert_entry(mock_db, entry)
    assert exc.value.code == dc.EXIT_DB


# --- patch_entry ---

def test_patch_entry_existing(mock_db):
    dc.patch_entry(mock_db, "99001", {"base": {"maxTemp": 225}})
    e = dc.find_entry(mock_db, "99001")
    assert e["base"]["maxTemp"] == 225
    assert e["base"]["brand"] == "Sunlu"


def test_patch_entry_nonexistent(mock_db):
    with pytest.raises(FmError) as exc:
        dc.patch_entry(mock_db, "99999", {"base": {"maxTemp": 225}})
    assert exc.value.code == dc.EXIT_DB


def test_patch_entry_stock_id_refused(mock_db):
    with pytest.raises(FmError) as exc:
        dc.patch_entry(mock_db, "01001", {"base": {"maxTemp": 225}})
    assert exc.value.code == dc.EXIT_DB


# --- remove_entry ---

def test_remove_entry(mock_db):
    dc.remove_entry(mock_db, "99001")
    assert mock_db["result"]["count"] == 1
    assert dc.find_entry(mock_db, "99001") is None


def test_remove_entry_stock_id_refused(mock_db):
    with pytest.raises(FmError) as exc:
        dc.remove_entry(mock_db, "01001")
    assert exc.value.code == dc.EXIT_DB


def test_remove_entry_nonexistent(mock_db):
    with pytest.raises(FmError) as exc:
        dc.remove_entry(mock_db, "99999")
    assert exc.value.code == dc.EXIT_DB


# --- bump_version / count_autofix ---

def test_bump_version(mock_db):
    dc.bump_version(mock_db, 9876543210)
    assert mock_db["result"]["version"] == "9876543210"


def test_bump_version_increments_above_floor():
    db = {"result": {"version": "9876543210"}}
    assert dc.bump_version(db) == 9876543211


def test_bump_version_sets_floor_from_below():
    db = {"result": {"version": "1781668740"}}
    assert dc.bump_version(db) == 9876543210


def test_count_autofix(mock_db):
    mock_db["result"]["count"] = 99  # wrong
    dc.count_autofix(mock_db)
    assert mock_db["result"]["count"] == 2


# --- build_entry ---

def test_build_entry_copies_template(mock_db):
    values = {
        "id": "99002", "brand": "eSun", "name": "eSun PLA+", "type": "PLA",
        "minTemp": 205, "maxTemp": 215, "density": 1.24,
        "color": "#ff0000", "pa": 0.03, "flowRatio": 0.995, "maxVolumetric": 12,
        "dryingTemp": 50, "dryingTime": 8,
    }
    entry = dc.build_entry(mock_db, values)
    assert entry["base"]["id"] == "99002"
    assert entry["base"]["brand"] == "eSun"
    assert entry["base"]["name"] == "eSun PLA+"
    assert entry["base"]["meterialType"] == "PLA"
    assert entry["base"]["density"] == 1.24
    assert entry["base"]["minTemp"] == 205
    assert entry["base"]["maxTemp"] == 215
    assert entry["base"]["dryingTemp"] == 50
    assert entry["base"]["dryingTime"] == 8
    assert entry["base"]["colors"] == ["#ff0000"]


def test_build_entry_overrides_kvparam(mock_db):
    values = {
        "id": "99002", "brand": "eSun", "name": "eSun PLA+", "type": "PLA",
        "minTemp": 205, "maxTemp": 215, "density": 1.24,
        "pa": 0.03, "flowRatio": 0.995, "maxVolumetric": 12,
    }
    entry = dc.build_entry(mock_db, values)
    assert entry["kvParam"]["nozzle_temperature"] == "215"
    assert entry["kvParam"]["nozzle_temperature_range_high"] == "215"
    assert entry["kvParam"]["nozzle_temperature_range_low"] == "205"
    assert entry["kvParam"]["filament_type"] == "PLA"
    assert entry["kvParam"]["filament_vendor"] == "eSun"
    assert entry["kvParam"]["filament_density"] == "1.24"
    assert entry["kvParam"]["filament_flow_ratio"] == "0.995"
    assert entry["kvParam"]["pressure_advance"] == "0.03"
    assert entry["kvParam"]["filament_max_volumetric_speed"] == "12"


def test_build_entry_preserves_unused_kvparam(mock_db):
    values = {
        "id": "99002", "brand": "eSun", "name": "eSun PLA+", "type": "PLA",
        "minTemp": 205, "maxTemp": 215,
    }
    entry = dc.build_entry(mock_db, values)
    # template had filament_flow_ratio="1.0" — should remain since we didn't override
    assert entry["kvParam"]["filament_flow_ratio"] == "1.0"


def test_build_entry_template_missing():
    db = {"result": {"list": [], "count": 0, "version": 1}}
    values = {"id": "99001", "brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 200, "maxTemp": 220}
    with pytest.raises(FmError) as exc:
        dc.build_entry(db, values)
    assert exc.value.code == dc.EXIT_DB


def test_build_entry_does_not_mutate_template(mock_db):
    import copy
    original = copy.deepcopy(mock_db)
    values = {
        "id": "99002", "brand": "eSun", "name": "eSun PLA+", "type": "PLA",
        "minTemp": 205, "maxTemp": 215,
    }
    dc.build_entry(mock_db, values)
    assert mock_db == original


# --- validate_entry ---

def test_validate_valid_entry():
    values = {
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "minTemp": 205, "maxTemp": 215, "density": 1.23,
    }
    errors, warnings = dc.validate_entry(values)
    assert errors == []
    assert warnings == []


def test_validate_min_gt_max():
    values = {"brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 220, "maxTemp": 200}
    errors, warnings = dc.validate_entry(values)
    assert any("minTemp" in e for e in errors)


def test_validate_temp_out_of_range():
    values = {"brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 50, "maxTemp": 200}
    errors, warnings = dc.validate_entry(values)
    assert any("100" in e or "400" in e for e in errors)


def test_validate_density_out_of_range():
    values = {"brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 200, "maxTemp": 220, "density": 5.0}
    errors, warnings = dc.validate_entry(values)
    assert any("density" in e.lower() for e in errors)


def test_validate_missing_required():
    values = {"name": "X PLA", "type": "PLA", "minTemp": 200, "maxTemp": 220}
    errors, warnings = dc.validate_entry(values)
    assert any("brand" in e for e in errors)


def test_validate_warns_missing_vendor_in_name():
    errors, warns = dc.validate_entry({"brand": "Sunlu", "name": "PLA+", "type": "PLA",
                                       "minTemp": 200, "maxTemp": 230})
    assert errors == []
    assert any("vendor" in w.lower() for w in warns)


def test_validate_name_without_vendor_warning():
    values = {"brand": "Sunlu", "name": "PLA+", "type": "PLA", "minTemp": 205, "maxTemp": 215}
    errors, warnings = dc.validate_entry(values)
    assert errors == []
    assert any("vendor" in w.lower() or "tie" in w.lower() for w in warnings)


def test_validate_unknown_type_warning():
    values = {"brand": "X", "name": "X Foo", "type": "FOO", "minTemp": 200, "maxTemp": 220}
    errors, warnings = dc.validate_entry(values)
    assert errors == []
    assert any("type" in w.lower() for w in warnings)


def test_validate_drying_temp_out_of_range():
    values = {"brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 200, "maxTemp": 220, "dryingTemp": 200}
    errors, warnings = dc.validate_entry(values)
    assert any("dryingTemp" in e for e in errors)


def test_validate_drying_time_out_of_range():
    values = {"brand": "X", "name": "X PLA", "type": "PLA", "minTemp": 200, "maxTemp": 220, "dryingTime": 50}
    errors, warnings = dc.validate_entry(values)
    assert any("dryingTime" in e for e in errors)


# --- brief representative cases against the 41-entry fixture ---

def test_patch_stock_entry_refused(sample_db):
    with pytest.raises(FmError):
        dc.patch_entry(sample_db, "01001", {})


def test_remove_then_count_autofix(sample_db):
    before = len(sample_db["result"]["list"])
    dc.remove_entry(sample_db, "99001")
    dc.count_autofix(sample_db)
    assert sample_db["result"]["count"] == before - 1


def test_sample_db_has_min_entries(sample_db):
    # MIN_DB_ENTRIES corruption guard (Rule 6)
    assert len(sample_db["result"]["list"]) >= dc.MIN_DB_ENTRIES


def test_sample_db_find_custom(sample_db):
    custom = dc.find_custom_entries(sample_db)
    assert len(custom) == 1
    assert custom[0]["base"]["id"] == "99001"
    assert custom[0]["base"]["name"] == "Sunlu PLA+"
