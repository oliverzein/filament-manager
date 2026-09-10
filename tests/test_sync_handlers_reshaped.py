import json
from datetime import datetime, timezone
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


def test_inheritance_unresolvable_skips_preset(
    inmemory_session, cfg, tmp_path, sample_db, monkeypatch
):
    """A preset whose inherits chain cannot be resolved is skipped, not pushed."""
    s = inmemory_session
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(sample_db))
    monkeypatch.setattr(sh, "_cache_db_path", cache)
    monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
    monkeypatch.setattr(sh, "_sleep", lambda sec: None)

    fake_client = MagicMock()
    fake_client.sync_pull.return_value = {
        "next_cursor": 0,
        "upserts": [{
            "id": "cloud-uuid-broken",
            "content": {
                "type": "filament",
                "name": "Broken Inherit",
                "inherits": "Nonexistent Template",
                "filament_type": "PLA",
                "filament_vendor": "Sunlu",
            },
        }],
        "deletes": [],
    }
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

    result = sh.handle_push(s, cfg, {})
    assert result["status"] == "ok"
    assert result["pushed"] == 0

    # No SyncTarget should have been created for the broken preset
    target = s.scalars(
        select(SyncTarget).where(
            SyncTarget.cloud_id == "cloud-uuid-broken",
        )
    ).first()
    assert target is None


def test_cloud_resource_ids_no_match_no_catalog_fallback(
    inmemory_session, cfg, tmp_path, sample_db, monkeypatch
):
    """When cloud_resource_ids are requested but none match cloud presets,
    the rebuild must NOT fall back to catalog (dumping all profiles)."""
    s = inmemory_session
    from fm.models import Profile
    now = datetime.now(timezone.utc)
    prof = Profile(
        brand="Sunlu", name="Sunlu PLA+", type="PLA",
        min_temp=200, max_temp=230, colors="[]",
        created_at=now, updated_at=now,
    )
    s.add(prof)
    s.flush()

    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(sample_db))
    monkeypatch.setattr(sh, "_cache_db_path", cache)
    monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
    monkeypatch.setattr(sh, "_sleep", lambda sec: None)

    fake_client = MagicMock()
    fake_client.sync_pull.return_value = {
        "next_cursor": 0,
        "upserts": [{
            "id": "some-other-uuid",
            "content": {
                "type": "filament",
                "name": "Other Preset",
                "filament_type": "PLA",
                "filament_vendor": "Generic",
            },
        }],
        "deletes": [],
    }
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

    result = sh.handle_push(s, cfg, {"cloud_resource_ids": ["nonexistent-uuid"]})
    assert result["status"] == "ok"
    assert result["pushed"] == 0
    # Catalog profile must NOT have been pushed
    assert prof.printer_db_id is None


def test_rebuild_from_cloud_pushes_inherited_creality_presets(
    inmemory_session, cfg, tmp_path, sample_db, monkeypatch
):
    """Inherited presets with Creality vendor (e.g. Hyper Stardust @K2-all) are
    now pushed to the printer DB with a 99xxx ID, just like any other brand
    (stock RFID block removed)."""
    cache_file = tmp_path / "printer-db-cache.json"
    cache_file.write_text(json.dumps(sample_db))
    monkeypatch.setattr(sh, "_resolve_cache_path", lambda: cache_file)

    fake_client = MagicMock()
    fake_client.sync_pull.return_value = {
        "next_cursor": 0,
        "upserts": [
            {
                "id": "cloud-uuid-hyper-stardust",
                "name": "Hyper Stardust Optimized",
                "content": {
                    "type": "filament",
                    "inherits": "Hyper Stardust @K2-all",
                },
            },
            {
                "id": "cloud-uuid-creality-hyper",
                "name": "Creality Hyper PLA Optimized",
                "content": {
                    "type": "filament",
                    "filament_vendor": ["Creality"],
                    "filament_type": ["PLA"],
                },
            },
        ],
        "deletes": [],
    }
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
    transport = _VerifyTransport(sample_db)
    monkeypatch.setattr(sh, "_get_transport", lambda c: transport)

    payload = {}
    result = sh.handle_push(inmemory_session, cfg, payload)
    assert result["pushed"] == 2

    # Both Creality presets should have 99xxx entries in the pushed DB
    pushed_db = json.loads(cache_file.read_text())
    new_ids = [
        e.get("base", {}).get("id", "")
        for e in pushed_db["result"]["list"]
        if e.get("base", {}).get("id", "").startswith("99")
    ]
    # Two new 99xxx entries should exist (the sample_db has one pre-existing 99001)
    assert len(new_ids) >= 2

    # SyncTargets should have been created with 99xxx printer_db_ids
    targets = inmemory_session.scalars(
        select(SyncTarget).where(SyncTarget.kind == "printer_db")
    ).all()
    assert len(targets) == 2
    for t in targets:
        assert t.printer_db_id is not None
        assert t.printer_db_id.startswith("99")
