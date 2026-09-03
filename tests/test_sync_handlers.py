# tests/test_sync_handlers.py
"""Sync orchestration handler tests — push, verify, generate_preset.

Tests the handlers directly (not through the API) using FakeTransport from
conftest and in-memory sessions.  The busy-flow / resolve path is tested
through the API in test_api_sync.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fm.orca_cloud import OrcaCloudConflict

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

    def test_skips_creality_stock_profiles(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        sunlu = _make_profile(s)
        creality = _make_profile(s, brand="Creality", name="Creality Hyper PLA Optimized")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        result = sh.handle_push(s, cfg, {})

        assert result["status"] == "ok"
        assert result["pushed"] == 1
        assert result["skipped"] == [creality.id]
        assert creality.printer_db_id is None
        # pushed DB contains no custom entry for the Creality profile
        db = json.loads(cache.read_text())
        assert not any(
            e.get("base", {}).get("name", "") == "Creality Hyper PLA Optimized"
            for e in db["result"]["list"]
        )

    def test_explicit_creality_profile_id_skipped(self, inmemory_session, cfg, tmp_path, sample_db, monkeypatch):
        s = inmemory_session
        creality = _make_profile(s, brand="Creality", name="Creality Hyper PLA Optimized")
        cache = _cache_file(tmp_path, sample_db)
        monkeypatch.setattr(sh, "_cache_db_path", cache)
        monkeypatch.setattr(sh, "_transport_factory", lambda c: FakeTransport())

        result = sh.handle_push(s, cfg, {"profile_ids": [creality.id]})

        assert result["status"] == "ok"
        assert result["pushed"] == 0
        assert result["skipped"] == [creality.id]
        assert creality.printer_db_id is None


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
    def test_builds_preset_and_pushes_to_cloud(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["filament_id"].startswith("P")
        assert result["expected_hash"] is not None
        assert result["status"] == "in_sync"
        # GitHub not configured in test settings → no backup scheduled
        assert result["github_backup_scheduled"] is False
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
        assert target.status == "in_sync"
        # sync_push was called with correct args
        fake_client.sync_push.assert_called_once()
        call_kwargs = fake_client.sync_push.call_args[1]
        assert call_kwargs["profile_id"] == result["filament_id"]
        assert call_kwargs["name"] == profile.name
        assert "content" in call_kwargs
        # No apply_preset job enqueued
        apply_job = s.scalars(
            select(Job).where(Job.kind == "apply_preset")
        ).first()
        assert apply_job is None

    def test_no_orca_client_raises(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        with pytest.raises(ValueError, match="Orca Cloud not configured"):
            sh.handle_generate_preset(s, cfg, {"profile_id": 1})

    def test_404_profile_raises(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        with pytest.raises(ValueError, match="not found"):
            sh.handle_generate_preset(s, cfg, {"profile_id": 9999})

    def test_no_template_raises(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory", lambda p: None)
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        with pytest.raises(ValueError, match="no system preset template"):
            sh.handle_generate_preset(s, cfg, {"profile_id": 1})

    def test_conflict_sets_drift_status(self, inmemory_session, cfg, monkeypatch):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.side_effect = OrcaCloudConflict("body", None)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["status"] == "drift"
        target = s.scalars(
            select(SyncTarget).where(
                SyncTarget.profile_id == profile.id,
                SyncTarget.kind == "orca_preset",
            )
        ).first()
        assert target.status == "drift"

    def test_template_override_uses_brand_template(self, inmemory_session, cfg, monkeypatch):
        """When template_override is set, the builder uses the brand-specific template."""
        s = inmemory_session
        profile = _make_profile(s, template_override="Bambu PETG Translucent", type="PETG")
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        # Use the real _preset_template_factory (not monkeypatched)
        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["filament_id"].startswith("P")
        # The generated preset should have Bambu-specific flow ratio
        fake_client.sync_push.assert_called_once()
        content = fake_client.sync_push.call_args[1]["content"]
        # sync_push receives the preset as a dict (content=), not a JSON string.
        preset = content
        assert preset["filament_flow_ratio"] == ["0.95"]
        assert preset["fan_max_speed"] == ["30"]

    def test_template_override_missing_logs_warning_and_falls_back(self, inmemory_session, cfg, monkeypatch, caplog):
        """When template_override references a missing file, log a warning and
        fall back to the type-based default (spec Edge Case 1)."""
        import logging
        s = inmemory_session
        profile = _make_profile(s, template_override="Nonexistent Template", type="PETG")
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        with caplog.at_level(logging.WARNING, logger=sh.log.name):
            result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # Still succeeds with the type-based default
        assert result["filament_id"].startswith("P")
        assert result["status"] == "in_sync"
        # A warning was logged about the missing override
        assert any(
            "Nonexistent Template" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        )

    # -----------------------------------------------------------------
    # Regression: duplicate cloud preset on second push
    # -----------------------------------------------------------------

    def test_push_with_existing_cloud_id_updates_same_resource(
        self, inmemory_session, cfg, monkeypatch,
    ):
        """Pushing an already-linked profile must update the SAME cloud
        resource — never create a same-name duplicate.

        Simulates a profile imported from Orca Cloud (W6) where the cloud
        resource UUID differs from UUIDv5(user_id, filament_id).
        """
        s = inmemory_session
        profile = _make_profile(s, name="Sunlu PLA+ Optimized",
                                filament_id="Pexisting123")
        target = SyncTarget(
            profile_id=profile.id, kind="orca_preset",
            cloud_id="existing-cloud-uuid",
            expected_hash="old-hash", status="in_sync",
        )
        s.add(target)
        s.flush()

        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        # cloud_id() returns a DIFFERENT uuid than the stored one
        fake_client.cloud_id.return_value = "uuidv5-computed-different"
        fake_client.sync_push.return_value = {
            "updated_time": 99999, "created_time": 12345,
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["status"] == "in_sync"
        # sync_push must target the existing resource, not a new UUIDv5
        fake_client.sync_push.assert_called_once()
        call_kwargs = fake_client.sync_push.call_args[1]
        assert call_kwargs.get("cloud_id") == "existing-cloud-uuid"
        # target.cloud_id must remain the existing resource UUID
        assert target.cloud_id == "existing-cloud-uuid"

    def test_push_discovers_existing_resource_by_filament_id(
        self, inmemory_session, cfg, monkeypatch,
    ):
        """When cloud_id is None (never pushed by this app), discover the
        existing cloud resource by filament_id before pushing — do not
        create a duplicate.
        """
        s = inmemory_session
        profile = _make_profile(s, name="Sunlu PLA+ Optimized",
                                filament_id="Pexisting123")
        target = SyncTarget(
            profile_id=profile.id, kind="orca_preset",
            cloud_id=None, expected_hash="old-hash", status="pending",
        )
        s.add(target)
        s.flush()

        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "uuidv5-computed-different"
        fake_client.sync_push.return_value = {
            "updated_time": 99999, "created_time": 12345,
        }
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "discovered-cloud-uuid",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+ Optimized",
                    "filament_id": "Pexisting123",
                    "filament_vendor": "Sunlu",
                    "filament_type": "PLA",
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["status"] == "in_sync"
        fake_client.sync_push.assert_called_once()
        call_kwargs = fake_client.sync_push.call_args[1]
        assert call_kwargs.get("cloud_id") == "discovered-cloud-uuid"
        assert target.cloud_id == "discovered-cloud-uuid"

    def test_push_ambiguous_name_match_sets_drift(
        self, inmemory_session, cfg, monkeypatch,
    ):
        """When cloud_id is None and multiple cloud presets share the same
        name, do NOT create a duplicate — set status to drift instead.
        """
        s = inmemory_session
        profile = _make_profile(s, name="Sunlu PLA+ Optimized",
                                filament_id="Pnew456")
        target = SyncTarget(
            profile_id=profile.id, kind="orca_preset",
            cloud_id=None, expected_hash="old-hash", status="pending",
        )
        s.add(target)
        s.flush()

        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "uuidv5-computed"
        fake_client.sync_push.return_value = {
            "updated_time": 99999, "created_time": 12345,
        }
        # Two cloud presets with the same name but different filament_ids
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {"id": "cloud-a", "content": {
                    "type": "filament", "name": "Sunlu PLA+ Optimized",
                    "filament_id": "Pold111", "filament_vendor": "Sunlu",
                    "filament_type": "PLA", "nozzle_temperature": "215",
                }},
                {"id": "cloud-b", "content": {
                    "type": "filament", "name": "Sunlu PLA+ Optimized",
                    "filament_id": "Pold222", "filament_vendor": "Sunlu",
                    "filament_type": "PLA", "nozzle_temperature": "215",
                }},
            ],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["status"] == "drift"
        # sync_push must NOT have been called — no duplicate created
        fake_client.sync_push.assert_not_called()
        assert target.status == "drift"
        assert target.cloud_id is None

    def test_push_no_cloud_match_creates_new_resource(
        self, inmemory_session, cfg, monkeypatch,
    ):
        """When cloud_id is None and no matching cloud preset exists,
        proceed with UUIDv5 push (correct behavior for a truly new preset).
        """
        s = inmemory_session
        profile = _make_profile(s, name="Brand New Profile",
                                filament_id="Pnew789")
        target = SyncTarget(
            profile_id=profile.id, kind="orca_preset",
            cloud_id=None, expected_hash=None, status="pending",
        )
        s.add(target)
        s.flush()

        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "uuidv5-new-resource"
        fake_client.sync_push.return_value = {
            "updated_time": 99999, "created_time": 12345,
        }
        fake_client.sync_pull.return_value = {
            "next_cursor": 0, "upserts": [], "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)

        result = sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        assert result["status"] == "in_sync"
        fake_client.sync_push.assert_called_once()
        call_kwargs = fake_client.sync_push.call_args[1]
        # No explicit cloud_id → sync_push computes UUIDv5 internally
        assert call_kwargs.get("cloud_id") is None
        assert target.cloud_id == "uuidv5-new-resource"


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


class TestPresetDriftComputation:
    """[P2] Finding 4: orca_preset expected_hash must be compared against
    pulled presets from Orca Cloud, updating status to in_sync / drift."""

    def test_preset_found_in_cloud_updates_temp_and_syncs(
        self, inmemory_session, cfg, monkeypatch
    ):
        """adopt=True: Cloud values adopted as baseline, nozzle_temperature
        written back to profile."""
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # Override sync_pull to return a preset with a different temp (cloud format)
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "filament_id": profile.filament_id,
                    "nozzle_temperature": "999",
                },
            }],
            "deletes": [],
        }

        sh.refresh_preset_drift(s, adopt=True)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        # Preset found in Cloud → in_sync (OrcaSlicer is the editor)
        assert target.status == "in_sync"
        # Cloud nozzle_temperature written to profile
        assert profile.nozzle_temperature == 999

    def test_read_only_drift_check_shows_drift_on_hash_mismatch(
        self, inmemory_session, cfg, monkeypatch
    ):
        """adopt=False (default): Cloud hash ≠ expected_hash → drift status,
        expected_hash and nozzle_temperature unchanged."""
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # Capture expected_hash set by handle_generate_preset
        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        original_expected_hash = target.expected_hash
        original_nozzle_temp = profile.nozzle_temperature

        # Override sync_pull to return a preset with a different temp (cloud format)
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "filament_id": profile.filament_id,
                    "nozzle_temperature": "999",  # differs from generated preset
                },
            }],
            "deletes": [],
        }

        sh.refresh_preset_drift(s)  # adopt=False (default)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        # Cloud hash ≠ expected_hash → drift (not in_sync)
        assert target.status == "drift"
        # expected_hash untouched
        assert target.expected_hash == original_expected_hash
        # nozzle_temperature not written back
        assert profile.nozzle_temperature == original_nozzle_temp

    def test_in_sync_when_pulled_preset_matches(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s, max_temp=215)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "filament_id": profile.filament_id,
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        assert target.status == "in_sync"

    def test_no_orca_client_leaves_status_unchanged(
        self, inmemory_session, cfg, monkeypatch
    ):
        s = inmemory_session
        profile = _make_profile(s)
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # No Orca Cloud client → refresh_preset_drift returns early
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        # Status remains as set by handle_generate_preset (in_sync on push success)
        assert target.status == "in_sync"

    def test_duplicate_cloud_presets_triggers_drift_with_detail(
        self, inmemory_session, cfg, monkeypatch
    ):
        """When multiple cloud presets share the profile's name (H-B:
        OrcaSlicer desktop re-uploads its own copy), refresh_preset_drift
        must mark the target as drift and store a _drift_detail warning
        in actual_state — even when the hash matches.
        """
        s = inmemory_session
        profile = _make_profile(s, name="Sunlu PLA+ Optimized")
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "app-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        # Capture expected_hash set by handle_generate_preset
        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        expected_hash = target.expected_hash

        # sync_pull returns TWO filament presets with different name casing
        # but the same filament_id — one from the app (matches cloud_id),
        # one from OrcaSlicer desktop (different id, uppercase name).
        # This mirrors the live evidence: 'SUNLU PLA+ Optimized' vs
        # 'Sunlu PLA+ Optimized', both with filament_id 'Pbbf5010'.
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {
                    "id": "app-uuid",
                    "name": "Sunlu PLA+ Optimized",
                    "content": {
                        "type": "filament",
                        "name": "Sunlu PLA+ Optimized",
                        "filament_type": "PLA",
                        "filament_vendor": "Sunlu",
                        "filament_id": profile.filament_id,
                        "nozzle_temperature": "215",
                    },
                },
                {
                    "id": "orcaslicer-uuid",
                    "name": "SUNLU PLA+ Optimized",
                    "content": {
                        "type": "filament",
                        "name": "SUNLU PLA+ Optimized",
                        "filament_type": "PLA",
                        "filament_vendor": "SUNLU",
                        "filament_id": profile.filament_id,
                        "nozzle_temperature": "215",
                    },
                },
            ],
            "deletes": [],
        }

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        # Duplicate cloud presets → drift regardless of hash match
        assert target.status == "drift"
        # actual_state contains the drift detail warning
        assert target.actual_state is not None
        state = json.loads(target.actual_state)
        assert "_drift_detail" in state
        assert "2 cloud presets share the name" in state["_drift_detail"]
        assert "app-uuid" in state["_drift_detail"]
        assert "orcaslicer-uuid" in state["_drift_detail"]
        # expected_hash unchanged (read-only drift check)
        assert target.expected_hash == expected_hash

    def test_single_cloud_preset_no_drift_detail(
        self, inmemory_session, cfg, monkeypatch
    ):
        """When only one cloud preset has the profile's name, no
        _drift_detail is set and status follows the normal hash check.
        """
        s = inmemory_session
        profile = _make_profile(s, name="Sunlu PLA+")
        monkeypatch.setattr(sh, "_preset_template_factory",
                            lambda p: _preset_template())
        fake_client = MagicMock()
        fake_client.cloud_id.return_value = "test-cloud-uuid"
        fake_client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_client)
        sh.handle_generate_preset(s, cfg, {"profile_id": profile.id})

        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "filament_id": profile.filament_id,
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }

        sh.refresh_preset_drift(s)

        target = s.scalars(select(SyncTarget).where(
            SyncTarget.profile_id == profile.id,
            SyncTarget.kind == "orca_preset",
        )).first()
        assert target.status == "in_sync"
        state = json.loads(target.actual_state)
        assert "_drift_detail" not in state


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


class TestRefreshPresetDriftProfileId:
    """Tests for the profile_id parameter of refresh_preset_drift."""

    def test_profile_id_filters_to_one_profile(self, inmemory_session, monkeypatch):
        """When profile_id is set, only that profile's orca_preset target
        is processed — other profiles' targets remain untouched."""
        from fm.models import Profile, SyncTarget
        from fm.service.sync_handlers import refresh_preset_drift

        # Create two profiles with orca_preset targets
        now = datetime.now(timezone.utc)
        p1 = Profile(brand="Sunlu", name="Sunlu PLA+", type="PLA",
                     min_temp=205, max_temp=215, density=1.24,
                     nozzle_temperature=220, filament_id="Paaa111",
                     created_at=now, updated_at=now)
        p2 = Profile(brand="eSUN", name="eSUN PETG", type="PETG",
                     min_temp=230, max_temp=240, density=1.27,
                     nozzle_temperature=240, filament_id="Pbbb222",
                     created_at=now, updated_at=now)
        inmemory_session.add_all([p1, p2])
        inmemory_session.flush()

        t1 = SyncTarget(profile_id=p1.id, kind="orca_preset",
                        cloud_id="cloud-1", expected_hash="hash_old_1",
                        status="in_sync")
        t2 = SyncTarget(profile_id=p2.id, kind="orca_preset",
                        cloud_id="cloud-2", expected_hash="hash_old_2",
                        status="in_sync")
        inmemory_session.add_all([t1, t2])
        inmemory_session.flush()

        # Mock the Orca client to return presets for both profiles
        from unittest.mock import MagicMock
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {"id": "cloud-1", "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": "Paaa111", "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"], "nozzle_temperature": ["210"],
                }},
                {"id": "cloud-2", "content": {
                    "type": "filament", "name": "eSUN PETG",
                    "filament_id": "Pbbb222", "filament_vendor": ["eSUN"],
                    "filament_type": ["PETG"], "nozzle_temperature": ["250"],
                }},
            ],
            "deletes": [],
        }
        monkeypatch.setattr(
            "fm.service.sync_handlers._orca_client_factory",
            lambda: fake_client,
        )

        # Call with profile_id=p1.id, adopt=True
        refresh_preset_drift(inmemory_session, adopt=True, profile_id=p1.id)

        # p1 should be adopted (nozzle_temp updated to 210)
        # p2 should be untouched (nozzle_temp stays 240)
        inmemory_session.refresh(p1)
        inmemory_session.refresh(p2)
        assert p1.nozzle_temperature == 210
        assert p2.nozzle_temperature == 240  # unchanged

    def test_profile_id_none_processes_all(self, inmemory_session, monkeypatch):
        """When profile_id is None (default), all profiles are processed
        — same as existing behavior."""
        from fm.models import Profile, SyncTarget
        from fm.service.sync_handlers import refresh_preset_drift

        now = datetime.now(timezone.utc)
        p1 = Profile(brand="Sunlu", name="Sunlu PLA+", type="PLA",
                     min_temp=205, max_temp=215, density=1.24,
                     nozzle_temperature=220, filament_id="Paaa111",
                     created_at=now, updated_at=now)
        p2 = Profile(brand="eSUN", name="eSUN PETG", type="PETG",
                     min_temp=230, max_temp=240, density=1.27,
                     nozzle_temperature=240, filament_id="Pbbb222",
                     created_at=now, updated_at=now)
        inmemory_session.add_all([p1, p2])
        inmemory_session.flush()

        t1 = SyncTarget(profile_id=p1.id, kind="orca_preset",
                        cloud_id="cloud-1", expected_hash="hash_old_1",
                        status="in_sync")
        t2 = SyncTarget(profile_id=p2.id, kind="orca_preset",
                        cloud_id="cloud-2", expected_hash="hash_old_2",
                        status="in_sync")
        inmemory_session.add_all([t1, t2])
        inmemory_session.flush()

        from unittest.mock import MagicMock
        fake_client = MagicMock()
        fake_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [
                {"id": "cloud-1", "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": "Paaa111", "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"], "nozzle_temperature": ["210"],
                }},
                {"id": "cloud-2", "content": {
                    "type": "filament", "name": "eSUN PETG",
                    "filament_id": "Pbbb222", "filament_vendor": ["eSUN"],
                    "filament_type": ["PETG"], "nozzle_temperature": ["250"],
                }},
            ],
            "deletes": [],
        }
        monkeypatch.setattr(
            "fm.service.sync_handlers._orca_client_factory",
            lambda: fake_client,
        )

        refresh_preset_drift(inmemory_session, adopt=True)

        inmemory_session.refresh(p1)
        inmemory_session.refresh(p2)
        assert p1.nozzle_temperature == 210
        assert p2.nozzle_temperature == 250
