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
