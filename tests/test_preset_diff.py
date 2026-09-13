# tests/test_preset_diff.py
"""Preset diff engine tests — normalization, categories, A-vs-B comparison."""
from __future__ import annotations

import pytest

from fm.orca_cloud import OrcaCloudError
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


class TestFetchCloudPresets:
    def _client(self, upserts):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.sync_pull.return_value = {
            "next_cursor": 0, "upserts": upserts, "deletes": [],
        }
        return c

    def test_returns_presets_by_id(self):
        client = self._client([{
            "id": "uuid-1",
            "content": {
                "type": "filament",
                "name": "Cloud PLA",
                "filament_type": "PLA",
                "nozzle_temperature": "220",
            },
        }])
        presets = preset_diff.fetch_cloud_presets(client)
        assert presets["uuid-1"]["name"] == "Cloud PLA"
        assert presets["uuid-1"]["nozzle_temperature"] == ["220"]

    def test_pull_error_raises(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.sync_pull.side_effect = OrcaCloudError(500, "boom")
        with pytest.raises(preset_diff.PresetDiffError, match="Cloud pull failed"):
            preset_diff.fetch_cloud_presets(client)

    def test_non_filament_presets_skipped(self):
        client = self._client([{
            "id": "uuid-print",
            "content": {"type": "print", "name": "0.2mm"},
        }])
        assert preset_diff.fetch_cloud_presets(client) == {}


class TestBuildPresetDiff:
    def _client(self, upserts):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.sync_pull.return_value = {
            "next_cursor": 0, "upserts": upserts, "deletes": [],
        }
        return c

    def test_not_connected_raises(self, monkeypatch):
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: None
        )
        with pytest.raises(preset_diff.PresetDiffError, match="not connected"):
            preset_diff.build_preset_diff("uuid-1", "uuid-2")

    def test_unknown_id_raises(self, monkeypatch):
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory",
            lambda: self._client([]),
        )
        with pytest.raises(preset_diff.PresetDiffError, match="not found"):
            preset_diff.build_preset_diff("uuid-1", "uuid-2")

    def test_end_to_end(self, monkeypatch):
        client = self._client([
            {
                "id": "uuid-a",
                "content": {
                    "type": "filament",
                    "name": "eSUN PLA",
                    "inherits": "eSUN PLA+ @System",
                    "filament_flow_ratio": "1.10",
                },
            },
            {
                "id": "uuid-b",
                "content": {
                    "type": "filament",
                    "name": "SUNLU PLA",
                    "filament_flow_ratio": "0.95",
                    "filament_max_volumetric_speed": "12",
                },
            },
        ])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        diff = preset_diff.build_preset_diff("uuid-a", "uuid-b")
        assert diff["name_a"] == "eSUN PLA"
        assert diff["name_b"] == "SUNLU PLA"
        assert _field(diff, "filament_flow_ratio")["status"] == "diff"
        # A inherits max_volumetric 16 from base; B sets 12 -> diff
        f = _field(diff, "filament_max_volumetric_speed")
        assert f["status"] == "diff"
        assert f["value_a"] == "16"
        assert f["value_b"] == "12"

    def test_base_pseudo_id(self, monkeypatch):
        client = self._client([{
            "id": "uuid-a",
            "content": {
                "type": "filament",
                "name": "eSUN PLA",
                "inherits": "eSUN PLA+ @System",
                "filament_flow_ratio": "1.10",
            },
        }])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        diff = preset_diff.build_preset_diff("uuid-a", "@base")
        assert diff["name_b"] == "eSUN PLA+ @System"
        f = _field(diff, "filament_flow_ratio")
        assert f["status"] == "diff"
        assert f["value_a"] == "1.10"
        assert f["value_b"] == "0.98"

    def test_base_on_standalone_raises(self, monkeypatch):
        client = self._client([{
            "id": "uuid-a",
            "content": {
                "type": "filament",
                "name": "Solo",
                "inherits": "",
                "filament_flow_ratio": "1.0",
            },
        }])
        monkeypatch.setattr(
            preset_diff.sync_handlers, "_orca_client_factory", lambda: client
        )
        with pytest.raises(preset_diff.PresetDiffError, match="base template"):
            preset_diff.build_preset_diff("uuid-a", "@base")
