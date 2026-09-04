import pytest
from unittest.mock import MagicMock
from fm.service.sync_handlers import _cloud_preset_to_entry_values
from fm.printer.dbcodec import validate_entry


def test_cloud_preset_to_entry_values_unboxing_and_types():
    raw_preset = {
        "id": "uuid-sunlu",
        "name": "Sunlu PLA+ Optimized",
        "filament_vendor": ["Sunlu"],
        "filament_type": ["PLA"],
        "nozzle_temperature": ["215"],
        "nozzle_temperature_range_low": ["205"],
        "nozzle_temperature_range_high": ["225"],
        "filament_density": ["1.24"],
        "filament_flow_ratio": ["0.98"],
        "pressure_advance": ["0.035"],
        "filament_max_volumetric_speed": ["18"],
    }
    values = _cloud_preset_to_entry_values(raw_preset, entry_id="99001")

    assert values["id"] == "99001"
    assert values["brand"] == "Sunlu"
    assert values["name"] == "Sunlu PLA+ Optimized"
    assert values["type"] == "PLA"
    assert values["minTemp"] == 205
    assert values["maxTemp"] == 225
    assert values["nozzle_temperature"] == 215
    assert values["density"] == 1.24
    assert values["dryingTemp"] == 0
    assert values["dryingTime"] == 0
    assert values["flowRatio"] == 0.98
    assert values["pa"] == 0.035
    assert values["maxVolumetric"] == 18.0

    errors, warnings = validate_entry(values)
    assert not errors
