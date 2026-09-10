# tests/test_api_sync.py
"""Sync API tests — push, backup, drift status, job resolve.

Uses FakeTransport from conftest.  The busy-flow test pins the spec behavior
from the task brief.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from conftest import FakeTransport
from fm.api.app import create_app, Settings
from fm.config import Config
from fm.db import SessionLocal
from fm.errors import FmError
from fm.models import Profile
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
    monkeypatch.setattr(sh, "_sleep", lambda s: None)
    monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake_orca_client)
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


def _make_profile(client, **overrides) -> dict:
    """Insert a Profile directly via ORM (catalog API is decommissioned)."""
    now = datetime.now(timezone.utc)
    p = Profile(
        brand=overrides.get("brand", "Sunlu"),
        name=overrides.get("name", "Sunlu PLA+"),
        type=overrides.get("type", "PLA"),
        colors=overrides.get("colors", "[]"),
        min_temp=overrides.get("min_temp", 205),
        max_temp=overrides.get("max_temp", 215),
        density=overrides.get("density", 1.23),
        drying_temp=overrides.get("drying_temp", 50),
        drying_time=overrides.get("drying_time", 8),
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as s:
        s.add(p)
        s.commit()
        s.refresh(p)
        return {
            "id": p.id,
            "brand": p.brand,
            "name": p.name,
            "type": p.type,
            "printer_db_id": p.printer_db_id,
        }


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
        assert matrix[0]["printer_db"]["status"] == "in_sync"

    def test_push_with_profile_ids(self, client):
        p1 = _make_profile(client, name="Sunlu PLA+")
        p2 = _make_profile(client, brand="eSUN", name="eSUN PETG Basic",
                           type="PETG", min_temp=230, max_temp=250)
        r = client.post("/api/sync/push", json={"profile_ids": [p1["id"]]})
        assert r.status_code == 200
        # Only p1 should have printer_db_id
        with SessionLocal() as s:
            prof1 = s.get(Profile, p1["id"])
            prof2 = s.get(Profile, p2["id"])
            assert prof1.printer_db_id is not None
            assert prof2.printer_db_id is None

    def test_push_with_cloud_resource_ids_no_fallback(self, client):
        """POST /api/sync/push with cloud_resource_ids that match no cloud
        presets must NOT fall back to catalog (fallback guard)."""
        p = _make_profile(client)
        r = client.post("/api/sync/push",
                        json={"cloud_resource_ids": ["nonexistent-uuid"]})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "done"
        result = data.get("result", {})
        # No presets pushed — cloud IDs requested but none matched
        assert result.get("pushed", 0) == 0
        # Catalog profile must NOT have been pushed (no fallback)
        with SessionLocal() as s:
            prof = s.get(Profile, p["id"])
            assert prof.printer_db_id is None

    def test_push_with_cloud_resource_ids_matches(self, client, monkeypatch):
        """POST /api/sync/push with cloud_resource_ids that match a cloud
        preset pushes that preset."""
        fake = MagicMock()
        fake.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "cloud-match-uuid",
                "content": {
                    "type": "filament",
                    "name": "Cloud Match",
                    "filament_type": "PLA",
                    "filament_vendor": "Sunlu",
                    "nozzle_temperature": "210",
                    "nozzle_temperature_range_low": "200",
                    "nozzle_temperature_range_high": "230",
                    "filament_density": "1.24",
                    "filament_flow_ratio": "0.98",
                    "pressure_advance": "0.035",
                    "filament_max_volumetric_speed": "18",
                },
            }],
            "deletes": [],
        }
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: fake)
        r = client.post("/api/sync/push",
                        json={"cloud_resource_ids": ["cloud-match-uuid"]})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "done"
        result = data.get("result", {})
        assert result.get("pushed", 0) == 1

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
# Drift status endpoint
# ---------------------------------------------------------------------------

class TestDriftStatus:
    def test_empty_catalog(self, client):
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        assert r.json() == []


    def test_creality_profile_shows_pending_status(self, client):
        """Creality profile with no printer_db target → pending status (stock RFID block removed)."""
        p = _make_profile(client, brand="Creality", name="Creality Hyper PLA")
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        entry = next(e for e in matrix if e["profile_id"] == p["id"])
        assert entry["printer_db"]["status"] == "new"
        assert entry["orca_preset"]["status"] == "pending"

    def test_legacy_entry_shows_in_sync(self, client):
        """A profile matching an existing printer-DB entry (no SyncTarget)
        shows in_sync — the CAILAB legacy scenario."""
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        with SessionLocal() as s:
            s.add(ProfileModel(
                brand="Sunlu", name="Sunlu PLA+", type="PLA", colors="[]",
                min_temp=205, max_temp=215, density=1.23,
                drying_temp=50, drying_time=8, nozzle_temperature=215,
                flow_ratio=0.998, pa=0.032,
                printer_db_id="99001",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))
            s.commit()
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        entry = r.json()[0]
        assert entry["printer_db"]["status"] == "in_sync"
        assert entry["printer_db"]["detail"] == ""

    def test_modified_entry_shows_modified_with_detail(self, client):
        """A profile whose printer-DB entry differs shows modified with a
        drift detail naming the changed field and both values."""
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        with SessionLocal() as s:
            s.add(ProfileModel(
                brand="Sunlu", name="Sunlu PLA+", type="PLA", colors="[]",
                min_temp=205, max_temp=220, density=1.23,
                drying_temp=50, drying_time=8, nozzle_temperature=215,
                flow_ratio=0.998, pa=0.032,
                printer_db_id="99001",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            ))
            s.commit()
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        entry = r.json()[0]
        assert entry["printer_db"]["status"] == "modified"
        detail = entry["printer_db"]["detail"]
        assert "Max temp" in detail
        assert "215" in detail
        assert "220" in detail

    def test_new_profile_shows_new(self, client):
        """A profile with no printer_db_id shows new (never pushed)."""
        _make_profile(client)
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        assert r.json()[0]["printer_db"]["status"] == "new"

    def test_drift_falls_back_without_printer_and_cache(self, client, monkeypatch, tmp_path):
        """When the printer is unreachable and no cache exists, drift falls
        back to SyncTarget bookkeeping — an unpushed profile shows pending."""
        from types import SimpleNamespace
        _make_profile(client)
        # scp_pull raises inside load_printer_db_for_drift's try/except → cache
        # fallback; the cache path points at a non-existent file → None → the
        # SyncTarget-based chain runs (unpushed profile → pending).
        monkeypatch.setattr(sh, "_transport_factory",
                            lambda c: SimpleNamespace(scp_pull=lambda p: (_ for _ in ()).throw(FmError(2, "down"))))
        monkeypatch.setattr(sh, "_cache_db_path", tmp_path / "missing.json")
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        assert r.json()[0]["printer_db"]["status"] == "pending"


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
