# tests/test_api_sync.py
"""Sync API tests — push, preset, drift status, job resolve.

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
from fm.errors import FmError
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
        assert matrix[0]["printer_db"]["status"] == "in_sync"

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
# Pull endpoints (Cloud → Catalog, adopt=True)
# ---------------------------------------------------------------------------

class TestPullEndpoint:
    def test_bulk_pull_returns_ok(self, client, fake_orca_client):
        """POST /api/sync/pull adopts all profiles from Cloud."""
        # Create a profile with an orca_preset target
        from fm.db import SessionLocal
        p = _make_profile(client)
        with SessionLocal() as s:
            from fm.models import SyncTarget
            target = SyncTarget(
                profile_id=p["id"], kind="orca_preset",
                cloud_id="test-cloud-uuid", expected_hash="old_hash",
                status="drift",
            )
            s.add(target)
            s.commit()

        # Set up Cloud to return a matching preset
        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": p.get("filament_id", "Ptest123"),
                    "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"],
                    "nozzle_temperature": ["210"],
                },
            }],
            "deletes": [],
        }

        r = client.post("/api/sync/pull")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_per_profile_pull_returns_ok(self, client, fake_orca_client):
        """POST /api/sync/pull/{profile_id} adopts one profile from Cloud."""
        from fm.db import SessionLocal
        p = _make_profile(client)
        with SessionLocal() as s:
            from fm.models import SyncTarget
            target = SyncTarget(
                profile_id=p["id"], kind="orca_preset",
                cloud_id="test-cloud-uuid", expected_hash="old_hash",
                status="drift",
            )
            s.add(target)
            s.commit()

        fake_orca_client.sync_pull.return_value = {
            "next_cursor": 0,
            "upserts": [{
                "id": "test-cloud-uuid",
                "content": {
                    "type": "filament", "name": "Sunlu PLA+",
                    "filament_id": p.get("filament_id", "Ptest123"),
                    "filament_vendor": ["Sunlu"],
                    "filament_type": ["PLA"],
                    "nozzle_temperature": ["210"],
                },
            }],
            "deletes": [],
        }

        r = client.post(f"/api/sync/pull/{p['id']}")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["profile_id"] == p["id"]

    def test_per_profile_pull_404_for_missing_profile(self, client):
        """POST /api/sync/pull/9999 returns 404."""
        r = client.post("/api/sync/pull/9999")
        assert r.status_code == 404

    def test_bulk_pull_error_without_orca_connection(self, client, monkeypatch):
        """POST /api/sync/pull returns error payload when Orca Cloud is not connected."""
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        r = client.post("/api/sync/pull")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "error"
        assert data["result"] == "Orca Cloud not connected — set up the connection in Settings"

    def test_per_profile_pull_error_without_orca_connection(self, client, monkeypatch):
        """POST /api/sync/pull/{profile_id} returns error payload when Orca Cloud is not connected."""
        p = _make_profile(client)
        monkeypatch.setattr(sh, "_orca_client_factory", lambda: None)
        r = client.post(f"/api/sync/pull/{p['id']}")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "error"
        assert data["result"] == "Orca Cloud not connected — set up the connection in Settings"

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
        assert entry["printer_db"]["status"] == "in_sync"
        assert entry["orca_preset"]["status"] == "in_sync"
        assert entry["printer_db"]["expected_hash"] is not None
        assert entry["orca_preset"]["expected_hash"] is not None

    def test_preset_drift_shown_when_cloud_temp_differs(self, client, fake_orca_client):
        """drift_status is read-only: Cloud temp ≠ expected_hash → drift,
        nozzle_temperature not written back to profile."""
        from fm.db import SessionLocal
        from fm.models import Profile as ProfileModel
        p = _make_profile(client)
        client.post(f"/api/sync/preset/{p['id']}")

        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            fid = prof.filament_id
            original_nozzle_temp = prof.nozzle_temperature

        # Override sync_pull to return a preset with a different temp (cloud format)
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
        # Cloud hash ≠ expected_hash → drift (read-only, no adopt)
        assert matrix[0]["orca_preset"]["status"] == "drift"
        # nozzle_temperature not written back
        with SessionLocal() as s:
            prof = s.get(ProfileModel, p["id"])
            assert prof.nozzle_temperature == original_nozzle_temp

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

    def test_stock_rfid_profile_shows_stock_status(self, client):
        """Creality profile with no printer_db target → stock status, not pending."""
        p = _make_profile(client, brand="Creality", name="Creality Hyper PLA")
        r = client.get("/api/status/drift")
        assert r.status_code == 200
        matrix = r.json()
        entry = next(e for e in matrix if e["profile_id"] == p["id"])
        assert entry["printer_db"]["status"] == "stock"
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
