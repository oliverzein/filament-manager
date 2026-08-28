# tests/test_api_sync.py
"""Sync API tests — push, preset, tempsync, drift status, job resolve.

Uses FakeTransport from conftest.  The busy-flow test pins the spec behavior
from the task brief.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import FakeTransport
from fm.api.app import create_app, Settings
from fm.config import Config
from fm.models import Job, Profile, SyncTarget
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
def fake_transport_busy():
    return FakeTransport(busy={"filename": "x.gcode", "progress": 50})


@pytest.fixture
def cache_db_file(tmp_path, sample_db):
    p = tmp_path / "cache.json"
    p.write_text(json.dumps(sample_db))
    return p

@pytest.fixture
def fake_orca_client():
    """Fake OrcaCloudClient with default empty sync_pull and successful sync_push."""
    client = MagicMock()
    client.cloud_id.return_value = "test-cloud-uuid"
    client.sync_push.return_value = {"updated_time": 12345, "created_time": 12345}
    client.sync_pull.return_value = {"next_cursor": 0, "upserts": [], "deletes": []}
    client.sync_delete.return_value = {"status": "ok"}
    client.get_user_id.return_value = "test-user-uuid"
    client.is_connected.return_value = True
    return client

@pytest.fixture
def client(tmp_path, monkeypatch, cfg, fake_transport, cache_db_file, fake_orca_client):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
    monkeypatch.setattr(sh, "_transport_factory", lambda c: fake_transport)
    monkeypatch.setattr(sh, "_cache_db_path", cache_db_file)
    monkeypatch.setattr(sh, "_preset_template_factory", _test_template)
    monkeypatch.setattr(sh, "_sleep", lambda s: None)
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_orca_client)
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


def _test_template(p):
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


def _make_profile(client, **overrides) -> dict:
    payload = {
        "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
        "colors": ["#ffffff"], "min_temp": 205, "max_temp": 215,
        "density": 1.23, "drying_temp": 50, "drying_time": 8,
    }
    payload.update(overrides)
    r = client.post("/api/profiles", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def get_job_status(client, job_id) -> str:
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["status"]


def wait_job_done(client, job_id) -> dict:
    """Since run_pending is synchronous, the job is done after the request."""
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Push endpoint
# ---------------------------------------------------------------------------

class TestPushEndpoint:
    def test_push_returns_job_id(self, client):
        _make_profile(client)
        r = client.post("/api/sync/push", json={})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["job_id"] is not None
        assert data["status"] == "done"

    def test_push_creates_sync_target(self, client):
        p = _make_profile(client)
        r = client.post("/api/sync/push", json={})
        assert r.status_code == 200
        # Drift status should show the profile
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert len(matrix) == 1
        assert matrix[0]["profile_id"] == p["id"]
        assert matrix[0]["printer_db"]["status"] == "pending"

    def test_push_with_profile_ids(self, client):
        p1 = _make_profile(client, name="Sunlu PLA+")
        p2 = _make_profile(client, brand="eSUN", name="eSUN PETG Basic",
                           type="PETG", min_temp=230, max_temp=250)
        r = client.post("/api/sync/push", json={"profile_ids": [p1["id"]]})
        assert r.status_code == 200
        # Only p1 should have printer_db_id
        r = client.get("/api/profiles")
        profiles = {p["id"]: p for p in r.json()}
        assert profiles[p1["id"]]["printer_db_id"] is not None
        assert profiles[p2["id"]]["printer_db_id"] is None


# ---------------------------------------------------------------------------
# Busy flow — pins the spec behavior
# ---------------------------------------------------------------------------

class TestBusyFlow:
    def test_busy_push_pauses_for_user(self, client, monkeypatch, fake_transport_busy):
        _make_profile(client)
        # Override transport to busy
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: fake_transport_busy)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        assert get_job_status(client, job_id) == "awaiting_input"

        r = client.post(f"/api/jobs/{job_id}/resolve",
                        json={"choice": "force_reboot"})
        assert r.status_code == 200
        assert wait_job_done(client, job_id)["status"] == "done"

    def test_busy_no_reboot_choice(self, client, monkeypatch, fake_transport_busy):
        _make_profile(client)
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: fake_transport_busy)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        assert get_job_status(client, job_id) == "awaiting_input"

        r = client.post(f"/api/jobs/{job_id}/resolve",
                        json={"choice": "no_reboot"})
        assert r.status_code == 200
        assert wait_job_done(client, job_id)["status"] == "done"

    def test_busy_wait_choice_retries(self, client, monkeypatch, fake_transport_busy):
        _make_profile(client)
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: fake_transport_busy)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        assert get_job_status(client, job_id) == "awaiting_input"

        # "wait" retries without force — still busy → parks again
        r = client.post(f"/api/jobs/{job_id}/resolve",
                        json={"choice": "wait"})
        assert r.status_code == 200
        assert get_job_status(client, job_id) == "awaiting_input"

    def test_resolve_invalid_choice_422(self, client, monkeypatch, fake_transport_busy):
        _make_profile(client)
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: fake_transport_busy)
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        r = client.post(f"/api/jobs/{job_id}/resolve",
                        json={"choice": "bogus"})
        assert r.status_code == 422

    def test_resolve_not_awaiting_409(self, client):
        _make_profile(client)
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        # job is done, not awaiting_input
        r = client.post(f"/api/jobs/{job_id}/resolve",
                        json={"choice": "force_reboot"})
        assert r.status_code == 409

    def test_resolve_404(self, client):
        r = client.post("/api/jobs/9999/resolve",
                        json={"choice": "force_reboot"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Preset endpoint
# ---------------------------------------------------------------------------

class TestPresetEndpoint:
    def test_preset_returns_job_id(self, client):
        p = _make_profile(client)
        r = client.post(f"/api/sync/preset/{p['id']}")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["job_id"] is not None
        assert data["status"] == "done"
        # Result has filament_id
        assert data["result"]["filament_id"].startswith("P")

    def test_preset_sets_orca_sync_target(self, client, fake_orca_client):
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        p = _make_profile(client)
        client.post(f"/api/sync/preset/{p['id']}")

        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            fid = prof.filament_id

        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": p["name"],
                "content": {
                    "type": "filament",
                    "name": p["name"],
                    "filament_type": p["type"],
                    "filament_vendor": p["brand"],
                    "filament_id": fid,
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        matrix = r.json()
        assert matrix[0]["orca_preset"]["status"] == "in_sync"
        assert matrix[0]["orca_preset"]["expected_hash"] is not None

    def test_preset_404_profile(self, client):
        r = client.post("/api/sync/preset/9999")
        assert r.status_code == 200  # job enqueued
        data = r.json()
        # handler raises ValueError → job error
        assert data["status"] == "error"

# ---------------------------------------------------------------------------
# Tempsync endpoint
# ---------------------------------------------------------------------------
class TestTempsyncEndpoint:
    def test_tempsync_no_orca_client_errors(self, client, monkeypatch):
        """Tempsync errors when Orca Cloud is not configured."""
        _make_profile(client, printer_db_id=None)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        r = client.post("/api/sync/tempsync", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "error"

    def test_tempsync_with_orca_pull(self, client, fake_orca_client):
        # Create a profile with printer_db_id via push
        p = _make_profile(client)
        client.post("/api/sync/push", json={})
        # Re-fetch profile to get printer_db_id
        profiles = client.get("/api/profiles").json()
        pid = profiles[0]["id"]
        assert profiles[0]["printer_db_id"] is not None

        # Override sync_pull to return a cloud-format preset with different temp
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "uuid1",
                "name": "Sunlu PLA+",
                "content": {
                    "type": "filament",
                    "name": "Sunlu PLA+",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "filament_id": "P1234567890123",
                    "nozzle_temperature": "220",
                    "nozzle_temperature_initial_layer": "220",
                },
            }],
            "deletes": [],
        }

        r = client.post("/api/sync/tempsync", json={})
        assert r.status_code == 200
        data = r.json()
        # temp_sync enqueues a push, which runs and succeeds
        # The temp_sync job itself should be done
        assert data["status"] == "done"


# ---------------------------------------------------------------------------
# Drift status endpoint
# ---------------------------------------------------------------------------

class TestDriftStatus:
    def test_empty_catalog(self, client):
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        assert r.json() == []

    def test_after_push_and_preset(self, client, fake_orca_client):
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        p = _make_profile(client)
        client.post("/api/sync/push", json={})
        client.post(f"/api/sync/preset/{p['id']}")

        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            fid = prof.filament_id

        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": p["name"],
                "content": {
                    "type": "filament",
                    "name": p["name"],
                    "filament_type": p["type"],
                    "filament_vendor": p["brand"],
                    "filament_id": fid,
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert len(matrix) == 1
        entry = matrix[0]
        assert entry["profile_id"] == p["id"]
        assert entry["printer_db"]["status"] == "pending"
        assert entry["orca_preset"]["status"] == "in_sync"
        assert entry["printer_db"]["expected_hash"] is not None
        assert entry["orca_preset"]["expected_hash"] is not None

    def test_preset_drift_surfaces_in_drift_status(self, client, fake_orca_client):
        """Orca_preset drift computed from sync_pull must surface in drift output."""
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        p = _make_profile(client)
        client.post(f"/api/sync/preset/{p['id']}")

        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            fid = prof.filament_id

        # Override sync_pull to return a drifting preset (cloud format)
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": p["name"],
                "content": {
                    "type": "filament",
                    "name": p["name"],
                    "filament_type": p["type"],
                    "filament_vendor": p["brand"],
                    "filament_id": fid,
                    "nozzle_temperature": "999",  # differs from generated preset
                },
            }],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert matrix[0]["orca_preset"]["status"] == "drift"

    def test_preset_in_sync_surfaces_in_drift_status(self, client, fake_orca_client):
        """Matching pulled preset → in_sync in drift output."""
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        p = _make_profile(client)  # max_temp defaults to 215
        client.post(f"/api/sync/preset/{p['id']}")

        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            fid = prof.filament_id

        # Override sync_pull to return a matching preset (cloud format)
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "name": p["name"],
                "content": {
                    "type": "filament",
                    "name": p["name"],
                    "filament_type": p["type"],
                    "filament_vendor": p["brand"],
                    "filament_id": fid,
                    "nozzle_temperature": "215",
                },
            }],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert matrix[0]["orca_preset"]["status"] == "in_sync"

    def test_preset_deleted_from_cloud_shows_drift(self, client, fake_orca_client):
        """Preset deleted from Orca Cloud → drift status (not stale in_sync)."""
        p = _make_profile(client)
        client.post(f"/api/sync/preset/{p['id']}")

        # sync_pull returns empty — preset was deleted from cloud
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [],
            "deletes": [],
        }

        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        assert matrix[0]["orca_preset"]["status"] == "drift"


# ---------------------------------------------------------------------------
# Job query endpoint
# ---------------------------------------------------------------------------

class TestJobQuery:
    def test_get_job_200(self, client):
        _make_profile(client)
        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        r = client.get(f"/api/jobs/{job_id}")
        assert r.status_code == 200
        assert r.json()["job_id"] == job_id

    def test_get_job_404(self, client):
        assert client.get("/api/jobs/9999").status_code == 404


# ---------------------------------------------------------------------------
# 503 when config missing
# ---------------------------------------------------------------------------

class TestNoConfig:
    def test_sync_503_without_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/api.db")
        # Don't set FM_PRINTER_IP etc. → load_config fails → config None
        monkeypatch.delenv("FM_PRINTER_IP", raising=False)
        monkeypatch.delenv("FM_SSH_USER", raising=False)
        monkeypatch.delenv("FM_SSH_PASSWORD", raising=False)
        monkeypatch.delenv("FM_DB_REMOTE_PATH", raising=False)
        with TestClient(create_app()) as c:
            r = c.post("/api/sync/push", json={})
            assert r.status_code == 503
