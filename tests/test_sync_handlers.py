# tests/test_sync_handlers.py
"""Sync orchestration handler tests — push, verify.

Tests the handlers directly (not through the API) using FakeTransport from
conftest and in-memory sessions.  The busy-flow / resolve path is tested
through the API in test_api_sync.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone


import pytest
from sqlalchemy import select

from conftest import FakeTransport
from fm.printer.dbcodec import find_entry
from fm.models import ActivityLog, Job, Profile, SyncTarget
from fm.service import sync_handlers as sh


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Avoid real time.sleep in run_push reboot wait during tests."""
    monkeypatch.setattr(sh, "_sleep", lambda s: None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(session, **overrides) -> Profile:
    now = datetime.now(timezone.utc)
    defaults = dict(
        brand="Sunlu", name="Sunlu PLA+", type="PLA",
        colors='["#ffffff"]', min_temp=205, max_temp=215,
        density=1.23, drying_temp=50, drying_time=8,
        notes="", created_at=now, updated_at=now,
    )
    defaults.update(overrides)
    p = Profile(**defaults)
    session.add(p)
    session.flush()
    return p


def _cache_file(tmp_path, db) -> Path:
    p = tmp_path / "cache.json"
    p.write_text(json.dumps(db))
    return p




# ---------------------------------------------------------------------------
# push handler
# ---------------------------------------------------------------------------

class TestPushHandler:
    def test_rebuilds_and_pushes(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        transport = FakeTransport()
        monkeypatch.setattr(sh, "_transport_factory", lambda c: transport)

        result = sh.handle_push(s, cfg, {})

        assert result["status"] == "ok"
        assert result["pushed"] == 1
        # printer_db_id assigned
        assert profile.printer_db_id is not None
        assert profile.printer_db_id.startswith("99")
        # SyncTarget created with expected_hash
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        assert target is not None
        assert target.expected_hash is not None
        assert target.status == "pending"
        # verify job enqueued
        verify_job = s.scalars(
            select(Job).where(Job.kind == "verify")
        ).first()
        assert verify_job is not None
        assert verify_job.status == "queued"
        # transport was used for push
        assert any(c.startswith("scp-push") for c in transport.commands)

    def test_busy_returns_awaiting_input_sentinel(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)

        result = sh.handle_push(s, cfg, {})

        assert result["_awaiting_input"] is True
        assert "force_reboot" in result["options"]
        assert "wait" in result["options"]
        assert "no_reboot" in result["options"]
        assert result["busy"]["filename"] == "x.gcode"
        # no verify job enqueued when busy
        verify_job = s.scalars(
            select(Job).where(Job.kind == "verify")
        ).first()
        assert verify_job is None

    def test_force_reboot_pushes_despite_busy(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)

        result = sh.handle_push(s, cfg, {"force_reboot": True})

        assert result["status"] == "ok"
        assert any(c.startswith("scp-push") for c in busy_transport.commands)

    def test_no_reboot_skips_reboot(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        transport = FakeTransport()
        monkeypatch.setattr(sh, "_transport_factory", lambda c: transport)

        result = sh.handle_push(s, cfg, {"no_reboot": True})

        assert result["status"] == "no_reboot"
        assert not any("sync; sync;" in c for c in transport.commands)

    def test_profile_ids_filters_targets(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        p1 = _make_profile(s, name="Sunlu PLA+")
        p2 = _make_profile(s, brand="eSUN", name="eSUN PETG Basic", type="PETG",
                           min_temp=230, max_temp=250)
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        result = sh.handle_push(s, cfg, {"profile_ids": [p1.id]})

        assert result["pushed"] == 1
        assert p1.printer_db_id is not None
        assert p2.printer_db_id is None  # not pushed

    def test_existing_printer_db_id_preserved(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s, printer_db_id="99001")
        monkeypatch.setattr(sh, "_cache_db_path", _cache_file(tmp_path, sample_db))
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        sh.handle_push(s, cfg, {})

        assert profile.printer_db_id == "99001"  # unchanged

    def test_creality_stock_profiles_are_pushed(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        """Creality profiles are now pushed normally (stock RFID block removed)."""
        s = inmemory_session
        sunlu = _make_profile(s)
        creality = _make_profile(s, brand="Creality", name="Creality Hyper PLA Optimized")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        result = sh.handle_push(s, cfg, {})

        assert result["status"] == "ok"
        assert result["pushed"] == 2
        assert result["skipped"] == []
        assert creality.printer_db_id is not None
        assert creality.printer_db_id.startswith("99")
        # pushed DB contains a custom entry for the Creality profile
        db = json.loads(cache.read_text())
        assert any(
            e.get("base", {}).get("name", "") == "Creality Hyper PLA Optimized"
            for e in db["result"]["list"]
        )

    def test_explicit_creality_profile_id_is_pushed(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        """Explicitly requesting a Creality profile pushes it normally."""
        s = inmemory_session
        creality = _make_profile(s, brand="Creality", name="Creality Hyper PLA Optimized")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        result = sh.handle_push(s, cfg, {"profile_ids": [creality.id]})

        assert result["status"] == "ok"
        assert result["pushed"] == 1
        assert result["skipped"] == []
        assert creality.printer_db_id is not None
        assert creality.printer_db_id.startswith("99")


# ---------------------------------------------------------------------------
# verify handler
# ---------------------------------------------------------------------------

class VerifyTransport(FakeTransport):
    """FakeTransport that materializes a DB on scp_pull."""

    def __init__(self, db, **kw):
        super().__init__(**kw)
        self.db = db

    def scp_pull(self, local):
        Path(local).write_text(json.dumps(self.db))


class TestVerifyHandler:
    def test_in_sync_when_hash_matches(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s)
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        # Push first to set expected_hash
        sh.handle_push(s, cfg, {})

        # Verify with matching DB (the cache file was updated by push)
        pushed_db = json.loads(cache.read_text())
        verify_transport = VerifyTransport(db=pushed_db)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: verify_transport)

        result = sh.handle_verify(s, cfg, {})

        assert result["version_ok"] is True
        assert result["cloud_rollback"] is False
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        assert target.status == "in_sync"

    def test_cloud_rollback_sets_error_and_warns(self, inmemory_session, cfg, sample_db, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s, printer_db_id="99001")
        target = sh._get_or_create_target(s, profile.id, "printer_db")
        target.printer_db_id = "99001"
        target.expected_hash = "somehash"

        # DB with low version (cloud-sync rollback)
        rolled_back_db = json.loads(json.dumps(sample_db))
        rolled_back_db["result"]["version"] = 1000  # < override 9876543210
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: VerifyTransport(db=rolled_back_db))

        result = sh.handle_verify(s, cfg, {})

        assert result["version_ok"] is False
        assert result["cloud_rollback"] is True
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        assert target.status == "error"
        # Activity log warn
        log = s.scalars(
            select(ActivityLog).where(ActivityLog.action == "verify")
            .order_by(ActivityLog.id.desc())
        ).first()
        assert log.outcome == "warn"

    def test_missing_entry_sets_error(self, inmemory_session, cfg, sample_db, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s, printer_db_id="99001")
        target = sh._get_or_create_target(s, profile.id, "printer_db")
        target.printer_db_id = "99001"
        target.expected_hash = "somehash"

        # DB without the 99001 entry (remove it)
        db_no_entry = json.loads(json.dumps(sample_db))
        db_no_entry["result"]["list"] = [
            e for e in db_no_entry["result"]["list"]
            if e["base"]["id"] != "99001"
        ]
        db_no_entry["result"]["version"] = str(cfg.version_override)
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: VerifyTransport(db=db_no_entry))

        result = sh.handle_verify(s, cfg, {})

        assert "99001" in result["missing"]
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "printer_db",
            )
        ).first()
        assert target.status == "error"




# ---------------------------------------------------------------------------
# _cloud_presets_to_local helper
# ---------------------------------------------------------------------------

class TestCloudPresetsToLocal:
    def test_wraps_array_fields(self):
        """Cloud string values for known array fields are wrapped in lists."""
        upserts = [{
            "id": "uuid1",
            "name": "Test",
            "content": {
                "type": "filament",
                "name": "Test",
                "filament_type": "PLA",
                "filament_vendor": "Sunlu",
                "nozzle_temperature": "220",
                "filament_id": "P1234567890123",
            },
        }]
        presets = sh._cloud_presets_to_local(upserts)
        assert len(presets) == 1
        p = presets[0]
        assert p["filament_type"] == ["PLA"]
        assert p["filament_vendor"] == ["Sunlu"]
        assert p["nozzle_temperature"] == ["220"]
        # Non-array fields stay as strings
        assert p["name"] == "Test"
        assert p["filament_id"] == "P1234567890123"

    def test_adds_internal_keys(self):
        """_path and _system keys are added for match_presets compatibility."""
        upserts = [{
            "id": "uuid-abc",
            "content": {"type": "filament", "name": "Test"},
        }]
        presets = sh._cloud_presets_to_local(upserts)
        assert presets[0]["_path"] == "uuid-abc"
        assert presets[0]["_system"] is False

    def test_empty_upserts(self):
        """Empty upserts list → empty presets list."""
        assert sh._cloud_presets_to_local([]) == []


# ---------------------------------------------------------------------------
# Final-review fix wave regression tests
# ---------------------------------------------------------------------------

class TestProductionCachePath:
    """[P1] Finding 1: handle_push must succeed without _cache_db_path
    monkeypatched — production derives the cache path from FM_DATABASE_URL
    and pulls from the printer on first run."""

    def test_push_succeeds_without_cache_db_path_monkeypatch(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        _make_profile(s)
        # Production-like: FM_DATABASE_URL set, _cache_db_path left as None
        monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/filaments.db")
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: VerifyTransport(db=sample_db))

        result = sh.handle_push(s, cfg, {})

        assert result["status"] == "ok"
        assert result["pushed"] == 1
        # Cache file created in the data dir alongside the SQLite DB
        assert (tmp_path / "printer-db-cache.json").exists()


class TestNozzleTemperaturePropagation:
    """[P1] Finding 2: profile.nozzle_temperature must reach the printer DB
    kvParam.nozzle_temperature instead of being overwritten by maxTemp."""

    def test_nozzle_temperature_reaches_printer(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, max_temp=215, nozzle_temperature=205)
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        sh.handle_push(s, cfg, {})

        pushed_db = json.loads(cache.read_text())
        entry = find_entry(pushed_db, profile.printer_db_id)
        assert entry["kvParam"]["nozzle_temperature"] == "205"
        # range_high still tracks maxTemp (the allowed range, not default temp)
        assert entry["kvParam"]["nozzle_temperature_range_high"] == "215"

    def test_nozzle_temperature_falls_back_to_max_temp(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, max_temp=215, nozzle_temperature=None)
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        sh.handle_push(s, cfg, {})

        pushed_db = json.loads(cache.read_text())
        entry = find_entry(pushed_db, profile.printer_db_id)
        assert entry["kvParam"]["nozzle_temperature"] == "215"


class TestEntryHashCoverage:
    """[P2] Finding 3: _entry_hash must cover density, drying_temp, drying_time,
    pa, flow_ratio, max_volumetric so drift in those fields is detected."""

    def test_density_drift_detected(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, density=1.23, printer_db_id="99001")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
        # Push to set expected_hash (includes density=1.23)
        sh.handle_push(s, cfg, {})

        # Verify with density mutated on the printer
        drifted_db = json.loads(json.dumps(sample_db))
        entry = find_entry(drifted_db, "99001")
        entry["base"]["density"] = 1.99
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: VerifyTransport(db=drifted_db))

        sh.handle_verify(s, cfg, {})

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "printer_db",
        )).first()
        assert target.status == "error"  # drift detected

    def test_flow_ratio_drift_detected(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, flow_ratio=0.998, printer_db_id="99001")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
        sh.handle_push(s, cfg, {})

        drifted_db = json.loads(json.dumps(sample_db))
        entry = find_entry(drifted_db, "99001")
        entry["kvParam"]["filament_flow_ratio"] = "0.95"
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: VerifyTransport(db=drifted_db))

        sh.handle_verify(s, cfg, {})

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "printer_db",
        )).first()
        assert target.status == "error"



class TestOrphanPreservation:
    """[F3/B] Orphan Rule: custom 99xxx entries must NOT be removed when
    their catalog profile is deleted — they persist on the printer until
    explicitly removed via a separate action."""

    def test_deleted_profile_orphan_preserved(
        self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, printer_db_id="99001")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())
        # Push to seed the cache with the profile's entry
        sh.handle_push(s, cfg, {})
        pushed_db = json.loads(cache.read_text())
        assert find_entry(pushed_db, "99001") is not None

        # Delete the profile from the catalog
        s.delete(profile)
        s.flush()

        # Push again — orphan 99001 must STILL be present (Orphan Rule F3/B)
        sh.handle_push(s, cfg, {})

        pushed_db = json.loads(cache.read_text())
        assert find_entry(pushed_db, "99001") is not None

