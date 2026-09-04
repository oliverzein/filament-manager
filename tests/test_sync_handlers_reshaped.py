import json
from pathlib import Path
from unittest.mock import MagicMock

from sqlalchemy import select

from conftest import FakeTransport
from fm.models import SyncTarget
from fm.printer.dbcodec import validate_entry
from fm.service import sync_handlers as sh
from fm.service.sync_handlers import _cloud_preset_to_entry_values


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


# ---------------------------------------------------------------------------
# Integration: cloud-first push creates SyncTarget and verify checks it
# ---------------------------------------------------------------------------

class _VerifyTransport(FakeTransport):
    """FakeTransport that materializes a DB on scp_pull."""

    def __init__(self, db, **kw):
        super().__init__(**kw)
        self.db = db

    def scp_pull(self, local):
        Path(local).write_text(json.dumps(self.db))


def test_rebuild_from_cloud_creates_synctarget_and_verifies(
    inmemory_session, cfg, tmp_path, sample_db, monkeypatch
):
    """Cloud-first push: a preset with no existing SyncTarget gets one
    created (cloud_id, printer_db_id, expected_hash).  Verify then
    finds it via the SyncTarget query and sets status to in_sync."""
    s = inmemory_session

    # Cache file for _load_cache_db
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(sample_db))
    monkeypatch.setattr(sh, "_cache_db_path", cache)
    monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
    monkeypatch.setattr(sh, "_sleep", lambda sec: None)

    # Fake Orca Cloud client with one filament preset
    fake_client = MagicMock()
    fake_client.sync_pull.return_value = {
        "next_cursor": 0,
        "upserts": [{
            "id": "cloud-uuid-sunlu",
            "content": {
                "type": "filament",
                "name": "Sunlu PLA+ Optimized",
                "filament_type": "PLA",
                "filament_vendor": "Sunlu",
                "nozzle_temperature": "215",
                "nozzle_temperature_range_low": "205",
                "nozzle_temperature_range_high": "225",
                "filament_density": "1.24",
                "filament_flow_ratio": "0.98",
                "pressure_advance": "0.035",
                "filament_max_volumetric_speed": "18",
            },
        }],
        "deletes": [],
    }
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

    # Push — should create a SyncTarget with cloud_id + printer_db_id
    result = sh.handle_push(s, cfg, {})

    assert result["status"] == "ok"
    assert result["pushed"] == 1

    # SyncTarget created with cloud_id, printer_db_id, expected_hash
    target = s.scalars(
        select(SyncTarget).where(
            SyncTarget.cloud_id == "cloud-uuid-sunlu",
            SyncTarget.kind == "printer_db",
        )
    ).first()
    assert target is not None
    assert target.printer_db_id is not None
    assert target.printer_db_id.startswith("99")
    assert target.expected_hash is not None
    assert target.status == "pending"

    # Verify — should find the SyncTarget and set status to in_sync
    pushed_db = json.loads(cache.read_text())
    pushed_db["result"]["version"] = str(cfg.version_override)
    verify_transport = _VerifyTransport(db=pushed_db)
    monkeypatch.setattr(sh, "_transport_factory", lambda c: verify_transport)

    verify_result = sh.handle_verify(s, cfg, {})

    assert verify_result["version_ok"] is True
    assert target.printer_db_id in verify_result["present"]

    # Re-query target (verify updates in-memory, not committed)
    target = s.scalars(
        select(SyncTarget).where(
            SyncTarget.cloud_id == "cloud-uuid-sunlu",
            SyncTarget.kind == "printer_db",
        )
    ).first()
    assert target.status == "in_sync"
