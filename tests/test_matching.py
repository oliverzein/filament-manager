# tests/test_matching.py
"""Tests for fm.presets.matching — preset scoring + temp-sync target selection.

Skip-rule cases ported from skill tests/test_sync.py (TestSyncSkips), adapted
to the catalog profile-state dict input shape consumed by temp_sync_targets.
"""
import json
from pathlib import Path

import pytest

from fm.presets import matching


# === Fixtures ===

def _preset(name, *, system=False, ftype="PLA", fid="Pabc", path=None,
            nozzle_temperature=None, nozzle_temperature_initial_layer=None):
    """Build a preset dict shaped like load_presets output."""
    p = {
        "name": name,
        "filament_type": [ftype],
        "filament_id": [fid],
        "_system": system,
        "_path": path or f"/presets/{name}.json",
    }
    if nozzle_temperature is not None:
        p["nozzle_temperature"] = [str(nozzle_temperature)]
    if nozzle_temperature_initial_layer is not None:
        p["nozzle_temperature_initial_layer"] = [str(nozzle_temperature_initial_layer)]
    return p


def _profile(*, id=1, brand="Sunlu", name="Sunlu PLA+", ftype="PLA",
             nozzle_temperature=215, printer_db_id="99001"):
    """Catalog profile-state dict (mirrors Profile row -> dict)."""
    return {
        "id": id,
        "brand": brand,
        "name": name,
        "type": ftype,
        "nozzle_temperature": nozzle_temperature,
        "printer_db_id": printer_db_id,
    }


@pytest.fixture
def user_preset():
    return _preset("Sunlu PLA+", system=False, nozzle_temperature=220)


# === get_filament_type / get_filament_id ===

def test_filament_type_from_list():
    assert matching.get_filament_type({"filament_type": ["PLA"]}) == "PLA"


def test_filament_type_from_str():
    assert matching.get_filament_type({"filament_type": "PETG"}) == "PETG"


def test_filament_type_missing_returns_empty():
    assert matching.get_filament_type({}) == ""


def test_filament_id_from_list():
    assert matching.get_filament_id({"filament_id": ["P123"]}) == "P123"


def test_filament_id_inherited_default():
    assert matching.get_filament_id({}) == "?(inherited)"


# === match_presets scoring ===

def test_brand_match_scores_twenty():
    presets = [_preset("Sunlu PLA+")]
    matches, considered = matching.match_presets(presets, "Sunlu", "Sunlu PLA+", "PLA")
    assert considered == 1
    assert len(matches) == 1
    assert matches[0]["score"] == 30  # brand(20) + vendor(10)


def test_vendor_only_scores_ten():
    presets = [_preset("Sunlu Generic")]
    matches, _ = matching.match_presets(presets, "Sunlu", "OtherBrand", "PLA")
    assert matches[0]["score"] == 10


def test_type_mismatch_filtered_out():
    presets = [_preset("Sunlu PLA+", ftype="PETG")]
    matches, considered = matching.match_presets(presets, "Sunlu", "Sunlu PLA+", "PLA")
    assert considered == 1
    assert matches == []


def test_zero_score_not_listed():
    # name contains neither brand nor vendor
    presets = [_preset("Generic PLA")]
    matches, _ = matching.match_presets(presets, "Sunlu", "Sunlu PLA+", "PLA")
    assert matches == []


def test_system_presort_wins_tie_break():
    # verbatim sort key (x["system"] is False) puts system (0) before user (1)
    sys_p = _preset("Sunlu PLA+ @System", system=True)
    usr_p = _preset("Sunlu PLA+", system=False)
    matches, _ = matching.match_presets([sys_p, usr_p], "Sunlu", "Sunlu PLA+", "PLA")
    assert matches[0]["system"] is True
    assert matches[1]["system"] is False
    assert matches[0]["score"] == matches[1]["score"] == 30


def test_higher_score_user_beats_lower_score_system():
    sys_p = _preset("Sunlu Generic", system=True)   # vendor only -> 10
    usr_p = _preset("Sunlu PLA+", system=False)     # brand+vendor -> 30
    matches, _ = matching.match_presets([sys_p, usr_p], "Sunlu", "Sunlu PLA+", "PLA")
    assert matches[0]["system"] is False
    assert matches[0]["score"] == 30


# === get_preset_temp ===

def test_preset_temp_from_list():
    assert matching.get_preset_temp({"nozzle_temperature": ["220"]}, "nozzle_temperature") == 220


def test_preset_temp_missing_returns_default():
    assert matching.get_preset_temp({}, "nozzle_temperature", default=200) == 200


def test_preset_temp_non_numeric_returns_default():
    assert matching.get_preset_temp({"nozzle_temperature": ["abc"]}, "nozzle_temperature") is None


def test_preset_temp_scalar_string():
    assert matching.get_preset_temp({"nozzle_temperature": "215"}, "nozzle_temperature") == 215


# === temp_sync_targets: skip rules (ported from skill TestSyncSkips) ===

def test_score30_user_preset_wins(user_preset):
    # brand(20)+vendor(10)=30, user preset, db_temp 215 != orca_temp 220 -> update
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, [user_preset])
    assert skipped == []
    assert len(updates) == 1
    u = updates[0]
    assert u["profile_id"] == 1
    assert u["db_temp"] == 215
    assert u["orca_temp"] == 220
    assert u["initial_layer_warning"] is False


def test_initial_layer_warning_flagged():
    preset = _preset("Sunlu PLA+", nozzle_temperature=220,
                     nozzle_temperature_initial_layer=225)
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, [preset])
    assert skipped == []
    assert updates[0]["initial_layer_warning"] is True


def test_tie_skipped():
    # two user presets with the same score -> ambiguous
    presets = [
        _preset("Sunlu PLA+ A", nozzle_temperature=220),
        _preset("Sunlu PLA+ B", nozzle_temperature=220),
    ]
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, presets)
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "ambiguous"
    assert skipped[0]["profile_id"] == 1


def test_system_winner_skipped():
    # only a system preset matches -> system
    presets = [_preset("Sunlu PLA+ @System", system=True, nozzle_temperature=220)]
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, presets)
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "system"


def test_score_below_thirty_skipped():
    # vendor-only match (score 10) < 30 -> no_match
    presets = [_preset("Sunlu Generic", nozzle_temperature=220)]  # name has "Sunlu" not "Sunlu PLA+"
    profiles = [_profile(brand="Sunlu", name="Sunlu PLA+", nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, presets)
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no_match"


def test_no_match_skipped():
    # no preset matches at all
    presets = [_preset("Other PLA", nozzle_temperature=220)]
    profiles = [_profile(brand="Sunlu", name="Sunlu PLA+", nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, presets)
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "no_match"


def test_missing_temp_skipped_db():
    # profile has no nozzle_temperature -> missing_temp
    preset = _preset("Sunlu PLA+", nozzle_temperature=220)
    profiles = [_profile(nozzle_temperature=None)]
    updates, skipped = matching.temp_sync_targets(profiles, [preset])
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "missing_temp"


def test_missing_temp_skipped_preset():
    # matched preset lacks nozzle_temperature -> missing_temp
    preset = _preset("Sunlu PLA+", nozzle_temperature=None)
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, [preset])
    assert updates == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "missing_temp"


def test_non_custom_printer_db_id_ignored():
    # printer_db_id not starting with 99 is skipped entirely (not even in skipped)
    preset = _preset("Sunlu PLA+", nozzle_temperature=220)
    profiles = [_profile(printer_db_id="01001")]
    updates, skipped = matching.temp_sync_targets(profiles, [preset])
    assert updates == []
    assert skipped == []


def test_matched_equal_temps_still_an_update():
    # db_temp == orca_temp: still returned as an update (caller decides mismatch)
    preset = _preset("Sunlu PLA+", nozzle_temperature=215)
    profiles = [_profile(nozzle_temperature=215)]
    updates, skipped = matching.temp_sync_targets(profiles, [preset])
    assert skipped == []
    assert len(updates) == 1
    assert updates[0]["db_temp"] == updates[0]["orca_temp"] == 215


def test_multiple_profiles_split():
    # one update, one skipped (no_match), one ignored (non-custom)
    good = _preset("Sunlu PLA+", nozzle_temperature=220)
    profiles = [
        _profile(id=1, nozzle_temperature=215, printer_db_id="99001"),   # update
        _profile(id=2, brand="eSUN", name="eSUN PETG", ftype="PETG",
                 nozzle_temperature=240, printer_db_id="99002"),          # no_match
        _profile(id=3, nozzle_temperature=215, printer_db_id="01001"),    # ignored
    ]
    updates, skipped = matching.temp_sync_targets(profiles, [good])
    assert [u["profile_id"] for u in updates] == [1]
    assert [s["profile_id"] for s in skipped] == [2]
    assert skipped[0]["reason"] == "no_match"


# === load_presets (filesystem) ===

def test_load_presets_user_and_system(tmp_path, monkeypatch):
    # system presets
    sys_dir = tmp_path / "sys" / "filament"
    sys_dir.mkdir(parents=True)
    (sys_dir / "Generic PLA.json").write_text(json.dumps({
        "name": "Generic PLA", "type": "filament", "filament_type": ["PLA"],
    }))
    # user presets
    user_dir = tmp_path / "OrcaSlicer" / "user" / "uuid-1" / "filament"
    user_dir.mkdir(parents=True)
    (user_dir / "Sunlu PLA+.json").write_text(json.dumps({
        "name": "Sunlu PLA+", "filament_type": ["PLA"], "from": "User", "inherits": "",
    }))
    monkeypatch.setattr(matching, "SYS_PROFILES", tmp_path / "sys")
    presets = matching.load_presets(config_dir=tmp_path / "OrcaSlicer")
    names = {p["name"] for p in presets}
    assert "Generic PLA" in names
    assert "Sunlu PLA+" in names
    sys_p = next(p for p in presets if p["name"] == "Generic PLA")
    usr_p = next(p for p in presets if p["name"] == "Sunlu PLA+")
    assert sys_p["_system"] is True
    assert usr_p["_system"] is False
    assert "_path" in sys_p and "_path" in usr_p


def test_load_presets_skips_non_filament(tmp_path, monkeypatch):
    sys_dir = tmp_path / "sys" / "filament"
    sys_dir.mkdir(parents=True)
    (sys_dir / "Bad.json").write_text(json.dumps({"name": "Bad", "type": "printer"}))
    (sys_dir / "Good.json").write_text(json.dumps({"name": "Good", "type": "filament"}))
    monkeypatch.setattr(matching, "SYS_PROFILES", tmp_path / "sys")
    presets = matching.load_presets(config_dir=tmp_path / "OrcaSlicer")
    assert [p["name"] for p in presets] == ["Good"]


def test_load_presets_uses_first_user_uuid_only(tmp_path, monkeypatch):
    monkeypatch.setattr(matching, "SYS_PROFILES", tmp_path / "nosuch")
    base = tmp_path / "OrcaSlicer"
    for uuid, has_fil in (("aaa", True), ("bbb", True)):
        d = base / "user" / uuid / "filament"
        d.mkdir(parents=True)
        (d / f"{uuid}.json").write_text(json.dumps({"name": uuid, "type": "filament"}))
    presets = matching.load_presets(config_dir=base)
    # only the first UUID dir (sorted) is scanned
    assert [p["name"] for p in presets] == ["aaa"]


# === is_orcaslicer_running ===

def test_is_orcaslicer_running_false_when_no_process(monkeypatch):
    def fake_run(cmd, **kw):
        import subprocess
        return subprocess.CompletedProcess(cmd, 1, "", "")
    monkeypatch.setattr(matching.subprocess, "run", fake_run)
    monkeypatch.setattr(matching, "psutil", None, raising=False)
    # force ImportError branch by hiding psutil
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "psutil":
            raise ImportError
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert matching.is_orcaslicer_running() is False


def test_is_orcaslicer_running_true_when_pgrep_matches(monkeypatch):
    def fake_run(cmd, **kw):
        import subprocess
        rc = 0 if cmd == ["pgrep", "-x", "OrcaSlicer"] else 1
        return subprocess.CompletedProcess(cmd, rc, "", "")
    monkeypatch.setattr(matching.subprocess, "run", fake_run)
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "psutil":
            raise ImportError
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert matching.is_orcaslicer_running() is True
