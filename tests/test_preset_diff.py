# tests/test_preset_diff.py
"""Preset diff engine tests — normalization, categories, A-vs-B comparison."""
from __future__ import annotations

import pytest

from fm.service import preset_diff


def _field(diff: dict, key: str) -> dict:
    for cat in diff["categories"]:
        for f in cat["fields"]:
            if f["key"] == key:
                return f
    raise AssertionError(f"field {key} not in diff")


class TestValuesEqual:
    def test_numeric_coercion(self):
        assert preset_diff._values_equal(["21"], "21.0")
        assert preset_diff._values_equal("0.98", ["0.98"])

    def test_string_inequality(self):
        assert not preset_diff._values_equal(["PLA"], "PETG")
        assert not preset_diff._values_equal("", "x")


class TestDiffPresets:
    def test_diff_same_only_statuses(self):
        a = {
            "name": "A", "type": "filament",
            "filament_flow_ratio": ["1.0"],
            "filament_vendor": ["eSUN"],
            "filament_notes": ["only in a"],
        }
        b = {
            "name": "B", "type": "filament",
            "filament_flow_ratio": ["1.05"],
            "filament_vendor": ["eSUN"],
            "filament_density": ["1.24"],
        }
        diff = preset_diff.diff_presets(a, b)
        assert diff["name_a"] == "A"
        assert diff["name_b"] == "B"
        assert _field(diff, "filament_flow_ratio")["status"] == "diff"
        assert _field(diff, "filament_flow_ratio")["value_a"] == "1.0"
        assert _field(diff, "filament_flow_ratio")["value_b"] == "1.05"
        assert _field(diff, "filament_vendor")["status"] == "same"
        assert _field(diff, "filament_notes")["status"] == "only_a"
        assert _field(diff, "filament_density")["status"] == "only_b"
        assert _field(diff, "filament_density")["value_a"] == ""
        assert diff["summary"] == {"diff": 1, "same": 1, "only_a": 1, "only_b": 1}

    def test_flatten_compares_effective_values(self):
        """A inherits flow_ratio 0.98 from its base; B sets 1.05 -> diff."""
        a = {
            "name": "Inherited A", "type": "filament",
            "inherits": "eSUN PLA+ @System",
        }
        b = {
            "name": "B", "type": "filament",
            "filament_flow_ratio": ["1.05"],
        }
        diff = preset_diff.diff_presets(a, b)
        f = _field(diff, "filament_flow_ratio")
        assert f["status"] == "diff"
        assert f["value_a"] == "0.98"
        assert f["value_b"] == "1.05"
        # A's inherited vendor resolves to eSUN; B has none -> only_a
        assert _field(diff, "filament_vendor")["status"] == "only_a"
        assert _field(diff, "filament_vendor")["value_a"] == "eSUN"

    def test_no_flatten_uses_raw_fields(self):
        a = {
            "name": "A", "type": "filament",
            "inherits": "eSUN PLA+ @System",
        }
        b = {"name": "B", "type": "filament", "filament_flow_ratio": ["1.05"]}
        diff = preset_diff.diff_presets(a, b, flatten=False)
        # Raw A has no flow_ratio (it lives in the base) -> only_b
        assert _field(diff, "filament_flow_ratio")["status"] == "only_b"

    def test_meta_keys_excluded(self):
        a = {
            "name": "A", "type": "filament", "version": "2.4.0.3",
            "setting_id": "abc", "filament_settings_id": "x",
            "filament_flow_ratio": ["1.0"],
        }
        b = {"name": "B", "type": "filament", "filament_flow_ratio": ["1.0"]}
        diff = preset_diff.diff_presets(a, b)
        keys = {f["key"] for c in diff["categories"] for f in c["fields"]}
        assert "type" not in keys
        assert "version" not in keys
        assert "setting_id" not in keys
        assert "filament_settings_id" not in keys
        assert "filament_flow_ratio" in keys

    def test_categories_group_fields(self):
        a = {
            "name": "A", "type": "filament",
            "nozzle_temperature": ["220"],
            "filament_flow_ratio": ["1.0"],
            "filament_start_gcode": ["G90\nM83"],
        }
        b = {"name": "B", "type": "filament"}
        diff = preset_diff.diff_presets(a, b)
        cats = {c["name"]: [f["key"] for f in c["fields"]] for c in diff["categories"]}
        assert "nozzle_temperature" in cats["Temperature"]
        assert "filament_flow_ratio" in cats["Flow & Pressure"]
        assert "filament_start_gcode" in cats["G-Code"]

    def test_multiline_flag(self):
        a = {"name": "A", "type": "filament", "filament_start_gcode": ["G90\nM83"], "filament_flow_ratio": ["1.0"]}
        b = {"name": "B", "type": "filament", "filament_start_gcode": ["G90"], "filament_flow_ratio": ["1.0"]}
        diff = preset_diff.diff_presets(a, b)
        f = _field(diff, "filament_start_gcode")
        assert f["multiline"] is True
        assert f["status"] == "diff"
        assert _field(diff, "filament_flow_ratio")["multiline"] is False
