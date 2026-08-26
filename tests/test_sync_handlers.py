# tests/test_sync_handlers.py
"""Sync orchestration handler tests — push, verify, generate_preset, temp_sync.

Tests the handlers directly (not through the API) using FakeTransport from
conftest and in-memory sessions.  The busy-flow / resolve path is tested
through the API in test_api_sync.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from conftest import FakeTransport
from fm.printer.dbcodec import find_entry
from fm.config import Config
from fm.models import ActivityLog, Job, Profile, SyncTarget
from fm.service import jobs
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


def _preset_template():
    """Minimal system preset template for build_standalone_preset."""
    return {
        "type": "filament",
        "filament_type": ["PLA"],
        "name": "Generic PLA",
        "from": "System",
        "inherits": "",
        "filament_vendor": ["Generic"],
        "filament_density": "1.24",
        "nozzle_temperature": ["210"],
        "nozzle_temperature_range_low": ["190"],
        "nozzle_temperature_range_high": ["240"],
        "version": "2.4.0.3",
    }


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
# generate_preset handler
# ---------------------------------------------------------------------------

class TestGeneratePresetHandler:
    def test_builds_preset_and_enqueues_apply(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["filament_id"].startswith("P")
        assert result["expected_hash"] is not None
        # profile filament_id updated
        assert profile.filament_id == result["filament_id"]
        # SyncTarget set
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "orca_preset",
            )
        ).first()
        assert target is not None
        assert target.expected_hash == result["expected_hash"]
        assert target.status == "pending"
        # apply_preset job enqueued
        apply_job = s.scalars(
            select(Job).where(Job.kind == "apply_preset")
        ).first()
        assert apply_job is not None
        payload = json.loads(apply_job.payload)
        assert payload["profile_id"] == profile.id
        assert "preset" in payload
        assert "info" in payload
        assert payload["name"] == profile.name

    def test_info_content_has_empty_sync_info(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())

        sh.handle_generate_preset(s, cfg, {"profile_id": 1})

        apply_job = s.scalars(
            select(Job).where(Job.kind == "apply_preset")
        ).first()
        payload = json.loads(apply_job.payload)
        info = payload["info"]
        assert "sync_info = " in info
        # sync_info must be empty (Global Constraint 8)
        assert "sync_info = \n" in info or info.startswith("sync_info = \n")

    def test_404_profile_raises(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        with pytest.raises(ValueError, match="not found"):
            sh.handle_generate_preset(s, cfg, {"profile_id": 9999})

    def test_no_template_raises(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory", lambda p: None)
        with pytest.raises(ValueError, match="no system preset template"):
            sh.handle_generate_preset(s, cfg, {"profile_id": 1})


# ---------------------------------------------------------------------------
# temp_sync handler
# ---------------------------------------------------------------------------

class TestTempSyncHandler:
    def _seed_report(self, session, presets):
        """Create a completed report_presets job with the given presets."""
        result = json.dumps({"presets": presets})
        job = Job(kind="report_presets", payload="{}", status="done", result=result)
        session.add(job)
        session.flush()
        return job

    def test_applies_temp_changes_and_enqueues_push(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, printer_db_id="99001", nozzle_temperature=215)
        # Preset with different nozzle_temperature
        presets = [{
            "_system": False,
            "_path": "/fake/user/filament/Sunlu PLA+.json",
            "name": "Sunlu PLA+",
            "filament_type": ["PLA"],
            "filament_vendor": ["Sunlu"],
            "filament_id": "P1234567890123",
            "nozzle_temperature": ["220"],
            "nozzle_temperature_initial_layer": ["220"],
        }]
        self._seed_report(s, presets)

        result = sh.handle_temp_sync(s, cfg, {})

        assert len(result["updates"]) == 1
        assert result["updates"][0]["profile_id"] == profile.id
        assert result["updates"][0]["orca_temp"] == 220
        # nozzle_temperature updated on profile
        assert profile.nozzle_temperature == 220
        # push job enqueued
        push_job = s.scalars(
            select(Job).where(Job.kind == "push")
        ).first()
        assert push_job is not None

    def test_no_report_raises(self, inmemory_session, cfg):
        s = inmemory_session
        _make_profile(s, printer_db_id="99001")
        with pytest.raises(ValueError, match="no report_presets"):
            sh.handle_temp_sync(s, cfg, {})

    def test_skips_non_pushed_profiles(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        # Profile without printer_db_id — should be filtered out
        _make_profile(s, printer_db_id=None, nozzle_temperature=200)
        presets = [{
            "_system": False, "_path": "/fake.json", "name": "Sunlu PLA+",
            "filament_type": ["PLA"], "filament_vendor": ["Sunlu"],
            "filament_id": "P123", "nozzle_temperature": ["220"],
            "nozzle_temperature_initial_layer": ["220"],
        }]
        self._seed_report(s, presets)

        result = sh.handle_temp_sync(s, cfg, {})

        assert result["updates"] == []


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


class TestPresetDriftComputation:
    """[P2] Finding 4: orca_preset expected_hash must be compared against
    reported presets, updating status to in_sync / drift."""

    def _seed_report(self, session, presets):
        result = json.dumps({"presets": presets})
        job = Job(kind="report_presets", payload="{}", status="done", result=result)
        session.add(job)
        session.flush()
        return job

    def test_drift_detected_when_preset_temp_differs(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        presets = [{
            "name": "Sunlu PLA+",
            "filament_type": ["PLA"],
            "filament_vendor": ["Sunlu"],
            "filament_id": profile.filament_id,
            "nozzle_temperature": ["999"],  # differs from generated preset
        }]
        self._seed_report(s, presets)

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        assert target.status == "drift"

    def test_in_sync_when_reported_preset_matches(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, max_temp=215)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        presets = [{
            "name": "Sunlu PLA+",
            "filament_type": ["PLA"],
            "filament_vendor": ["Sunlu"],
            "filament_id": profile.filament_id,
            "nozzle_temperature": ["215"],  # matches generated preset
        }]
        self._seed_report(s, presets)

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        assert target.status == "in_sync"

    def test_no_report_leaves_status_pending(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        assert target.status == "pending"


class TestOrphanRemoval:
    """[P2] Finding 5: _rebuild_printer_db must remove 99xxx entries with no
    catalog profile so deleted profiles don't linger on the printer."""

    def test_deleted_profile_orphan_removed(
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

        # Push again — orphan 99001 should be removed
        sh.handle_push(s, cfg, {})

        pushed_db = json.loads(cache.read_text())
        assert find_entry(pushed_db, "99001") is None


class TestImportScanTempFileCleanup:
    """[P3] Finding 6: handle_import_scan must not leak the temp file when
    pulling via scp_pull (no _import_db_path override)."""

    def test_temp_file_unlinked_after_import_scan(
        self, inmemory_session, cfg, sample_db, monkeypatch
    ):
        s = inmemory_session
        monkeypatch.setattr(sh, "_import_db_path", None)

        pulled_path: list[Path] = []

        class CapturingTransport(FakeTransport):
            def scp_pull(self, local):
                p = Path(local)
                pulled_path.append(p)
                p.write_text(json.dumps(sample_db))

        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: CapturingTransport())

        sh.handle_import_scan(s, cfg, {})

        assert len(pulled_path) == 1
        assert not pulled_path[0].exists()  # temp file cleaned up
