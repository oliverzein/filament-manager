# tests/conftest.py
import json
from pathlib import Path

import pytest
from fm.config import Config


@pytest.fixture
def cfg():
    return Config(printer_ip="192.168.0.101", ssh_user="root",
                  ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
                  ws_port=9999, version_override=9876543210,
                  id_range_start=99001, agent_token="testtoken")


@pytest.fixture
def mock_db():
    """Minimal valid DB with Hyper PLA template + one custom entry (lifted from skill conftest)."""
    return {
        "code": 0,
        "msg": "",
        "reqId": "",
        "result": {
            "list": [
                {
                    "engineVersion": "1.0",
                    "printerIntName": "creality_k2",
                    "nozzleDiameter": "0.4",
                    "base": {
                        "id": "01001",
                        "brand": "Creality",
                        "name": "Hyper PLA",
                        "meterialType": "PLA",
                        "colors": ["#ffffff"],
                        "density": 1.24,
                        "diameter": "1.75",
                        "costPerMeter": 0,
                        "weightPerMeter": 0,
                        "rank": 10000,
                        "minTemp": 190,
                        "maxTemp": 240,
                        "isSoluble": False,
                        "isSupport": False,
                        "shrinkageRate": 0,
                        "softeningTemp": 0,
                        "dryingTemp": 0,
                        "dryingTime": 0,
                        "dryingTempLow": 0,
                        "dryingTempHigh": 0,
                    },
                    "kvParam": {
                        "nozzle_temperature": "220",
                        "nozzle_temperature_range_high": "240",
                        "nozzle_temperature_range_low": "190",
                        "filament_type": "PLA",
                        "filament_vendor": "Creality",
                        "filament_density": "1.24",
                        "filament_flow_ratio": "1.0",
                        "pressure_advance": "0.02",
                        "filament_max_volumetric_speed": "10",
                    },
                },
                {
                    "engineVersion": "1.0",
                    "printerIntName": "creality_k2",
                    "nozzleDiameter": "0.4",
                    "base": {
                        "id": "99001",
                        "brand": "Sunlu",
                        "name": "Sunlu PLA+",
                        "meterialType": "PLA",
                        "colors": ["#ffffff"],
                        "density": 1.23,
                        "diameter": "1.75",
                        "costPerMeter": 0,
                        "weightPerMeter": 0,
                        "rank": 10000,
                        "minTemp": 205,
                        "maxTemp": 215,
                        "isSoluble": False,
                        "isSupport": False,
                        "shrinkageRate": 0,
                        "softeningTemp": 0,
                        "dryingTemp": 50,
                        "dryingTime": 8,
                        "dryingTempLow": 0,
                        "dryingTempHigh": 0,
                    },
                    "kvParam": {
                        "nozzle_temperature": "215",
                        "nozzle_temperature_range_high": "215",
                        "nozzle_temperature_range_low": "205",
                        "filament_type": "PLA",
                        "filament_vendor": "Sunlu",
                        "filament_density": "1.23",
                        "filament_flow_ratio": "0.998",
                        "pressure_advance": "0.032",
                        "filament_max_volumetric_speed": "10",
                    },
                },
            ],
            "count": 2,
            "version": 1781668740,
        },
    }


@pytest.fixture
def db_file(tmp_path, mock_db):
    """Write mock_db to tmp_path/db.json, return path."""
    p = tmp_path / "db.json"
    p.write_text(json.dumps(mock_db))
    return p


@pytest.fixture
def sample_db():
    """Load the 40-stock + 1-custom fixture DB (tests/fixtures/sample_db.json)."""
    with open(Path(__file__).parent / "fixtures" / "sample_db.json") as f:
        return json.load(f)
