# tests/test_ui_import_orca.py
"""UI import-from-Orca-Cloud tests — verifies that import_orca_apply
creates a SyncTarget(kind=orca_preset) and that drift_status then
reports the target as in_sync.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FakeTransport
from fm.api.app import create_app, Settings
from fm.api import ui as ui_module
from fm.config import Config
from fm.db import SessionLocal
from fm.models import Profile, SyncTarget
from fm.service import sync_handlers as sh


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return Config(
        printer_ip="192.168.0.101", ssh_user="root",
        ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
        ws_port=9999, version_override=9876543210,
        id_range_start=99001,
    )


@pytest.fixture
def fake_transport():
    return FakeTransport()


@pytest.fixture
def fake_orca_client():
    """Fake OrcaCloudClient with default empty sync_pull."""
    client = MagicMock()
    client.cloud_id.return_value = "test-cloud-uuid"
    client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
    client.sync_pull.return_value = {"next_cursor": 0, "upserts": [], "deletes": []}
    client.sync_delete.return_value = {"status": "ok"}
    client.get_user_id.return_value = "test-user-uuid"
    client.is_connected.return_value = True
    return client


# A preset in local format (as _cloud_presets_to_local would produce).
_PRESET = {
    "name": "Sunlu PLA+",
    "filament_id": "Pabc123def456",
    "filament_vendor": ["Sunlu"],
    "filament_type": ["PLA"],
    "nozzle_temperature": ["220"],
    "type": "filament",
    "_path": "test-cloud-uuid",
    "_system": False,
}


@pytest.fixture
def client(tmp_path, monkeypatch, cfg, fake_transport, fake_orca_client):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/import_orca.db")
    monkeypatch.setattr(sh, "_transport_factory", lambda c: fake_transport)
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_orca_client)
    monkeypatch.setattr(ui_module, "_pull_orca_presets", lambda: [_PRESET])
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestImportOrcaApply:
    def test_creates_sync_target_orca_preset(self, client):
        """POST /import/orca/apply creates a SyncTarget(kind=orca_preset)
        with cloud_id, expected_hash, actual_state, and last_synced_at."""
        r = client.post("/import/orca/apply", data={"include": ["Pabc123def456"]},
                        follow_redirects=False)
        assert r.status_code == 303

        with SessionLocal() as s:
            profile = s.scalars(select(Profile)).first()
            assert profile is not None
            assert profile.filament_id == "Pabc123def456"

            target = s.scalars(
                select(SyncTarget).where(
                    SyncTarget.profile_id == profile.id,
                    SyncTarget.kind == "orca_preset",
                )
            ).first()
            assert target is not None
            assert target.cloud_id == "test-cloud-uuid"
            assert target.expected_hash is not None
            assert target.actual_state is not None
            assert target.status == "pending"
            assert target.last_synced_at is not None

    def test_drift_status_shows_in_sync_after_import(self, client, fake_orca_client):
        """After import, drift_status (which calls refresh_preset_drift)
        finds the target by cloud_id and marks it in_sync."""
        client.post("/import/orca/apply", data={"include": ["Pabc123def456"]},
                    follow_redirects=False)

        # Configure sync_pull to return the same preset (cloud format).
        # refresh_preset_drift matches by cloud_id == "test-cloud-uuid".
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_id": "Pabc123def456",
                    "filament_vendor": "Sunlu",
                    "filament_type": "PLA",
                    "nozzle_temperature": "220",
                },
            }],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert len(matrix) == 1
        assert matrix[0]["orca_preset"]["status"] == "in_sync"
        assert matrix[0]["orca_preset"]["expected_hash"] is not None


# ---------------------------------------------------------------------------
# Sync from Orca — adopt path
# ---------------------------------------------------------------------------

class TestSyncFromOrcaAdopt:
    def test_sync_from_orca_adopts_baseline_and_writes_temp(self, client, fake_orca_client):
        """Explicit 'Sync from Orca' (POST /profiles/sync-from-orca) uses
        adopt=True: expected_hash updated to Cloud hash, nozzle_temperature
        written back to profile."""
        from fm.service.sync_handlers import canonical_hash

        # Import a profile with a SyncTarget (expected_hash = Cloud hash for 220)
        client.post("/import/orca/apply", data={"include": ["Pabc123def456"]},
                    follow_redirects=False)

        with SessionLocal() as s:
            profile = s.scalars(select(Profile)).first()
            original_nozzle_temp = profile.nozzle_temperature
            assert original_nozzle_temp == 220

        # Cloud now returns a preset with a different temp (999)
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_id": "Pabc123def456",
                    "filament_vendor": "Sunlu",
                    "filament_type": "PLA",
                    "nozzle_temperature": "999",
                },
            }],
            "deletes": [],
        }

        # Explicit sync-from-orca → adopt path
        r = client.post("/profiles/sync-from-orca", follow_redirects=False)
        assert r.status_code == 303

        # Expected hash should now be the Cloud hash (for temp 999)
        cloud_hash = canonical_hash({
            "name": "Sunlu PLA+",
            "filament_id": "Pabc123def456",
            "filament_vendor": ["Sunlu"],
            "filament_type": ["PLA"],
            "nozzle_temperature": ["999"],
        })

        with SessionLocal() as s:
            profile = s.scalars(select(Profile)).first()
            target = s.scalars(
                select(SyncTarget).where(
                    SyncTarget.profile_id == profile.id,
                    SyncTarget.kind == "orca_preset",
                )
            ).first()
            assert target.status == "in_sync"
            assert target.expected_hash == cloud_hash
            # nozzle_temperature written back from Cloud
            assert profile.nozzle_temperature == 999
