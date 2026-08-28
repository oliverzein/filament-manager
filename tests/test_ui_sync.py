# tests/test_ui_sync.py
"""UI sync page + migration wizard tests.

Pins the two brief-specified tests:
  * test_migration_proposes_99001_to_99004
  * test_migration_import_preserves_filament_id
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import FakeTransport
from fm.api.app import create_app, Settings
from fm.config import Config
from fm.db import SessionLocal
from fm.models import Job, Profile, SyncTarget
from fm.service import sync_handlers as sh


def get_job_status(client, job_id) -> str:
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["status"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return Config(
        printer_ip="192.168.0.101", ssh_user="root",
        ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
        ws_port=9999, version_override=9876543210,
        id_range_start=99001, agent_token="testtoken",
    )


@pytest.fixture
def fake_transport():
    return FakeTransport()


def _custom_entry(eid, brand, name, mtype="PLA",
                  min_t=200, max_t=220, density=1.24,
                  drying_temp=50, drying_time=8,
                  flow_ratio="0.998", pa="0.032",
                  max_volumetric="10"):
    """Build a minimal 99xxx printer-DB entry."""
    return {
        "engineVersion": "1.0",
        "printerIntName": "creality_k2",
        "nozzleDiameter": "0.4",
        "base": {
            "id": eid, "brand": brand, "name": name,
            "meterialType": mtype, "colors": ["#ffffff"],
            "density": density, "diameter": "1.75",
            "costPerMeter": 0, "weightPerMeter": 0, "rank": 10000,
            "minTemp": min_t, "maxTemp": max_t,
            "isSoluble": False, "isSupport": False,
            "shrinkageRate": 0, "softeningTemp": 0,
            "dryingTemp": drying_temp, "dryingTime": drying_time,
            "dryingTempLow": 0, "dryingTempHigh": 0,
        },
        "kvParam": {
            "nozzle_temperature": str(max_t),
            "nozzle_temperature_range_high": str(max_t),
            "nozzle_temperature_range_low": str(min_t),
            "filament_type": mtype,
            "filament_vendor": brand,
            "filament_density": str(density),
            "filament_flow_ratio": flow_ratio,
            "pressure_advance": pa,
            "filament_max_volumetric_speed": max_volumetric,
        },
    }


@pytest.fixture
def import_db(tmp_path):
    """DB with 99001–99004 custom entries + template 01001."""
    db = {
        "code": 0, "msg": "", "reqId": "",
        "result": {
            "list": [
                _custom_entry("01001", "Creality", "Hyper PLA",
                              min_t=190, max_t=240),
                _custom_entry("99001", "Sunlu", "Sunlu PLA+",
                              min_t=205, max_t=215, density=1.23),
                _custom_entry("99002", "eSUN", "eSUN PLA+",
                              min_t=200, max_t=220, density=1.24),
                _custom_entry("99003", "Polymaker", "PolyLite PLA",
                              min_t=190, max_t=230, density=1.25),
                _custom_entry("99004", "Bambu", "Bambu PLA Basic",
                              min_t=210, max_t=220, density=1.24),
            ],
            "count": 5,
            "version": 1781668740,
        },
    }
    p = tmp_path / "import_db.json"
    p.write_text(json.dumps(db))
    return p


@pytest.fixture
def single_entry_db(tmp_path):
    """DB with a single 99001 custom entry + template 01001."""
    db = {
        "code": 0, "msg": "", "reqId": "",
        "result": {
            "list": [
                _custom_entry("01001", "Creality", "Hyper PLA",
                              min_t=190, max_t=240),
                _custom_entry("99001", "Sunlu", "Sunlu PLA+",
                              min_t=205, max_t=215, density=1.23),
            ],
            "count": 2,
            "version": 1781668740,
        },
    }
    p = tmp_path / "single_db.json"
    p.write_text(json.dumps(db))
    return p


@pytest.fixture
def client(tmp_path, monkeypatch, cfg, fake_transport, import_db):
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui_sync.db")
    monkeypatch.setattr(sh, "_transport_factory", lambda c: fake_transport)
    monkeypatch.setattr(sh, "_import_db_path", import_db)
    monkeypatch.setattr(sh, "_sleep", lambda s: None)
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


@pytest.fixture
def single_client(tmp_path, monkeypatch, cfg, fake_transport, single_entry_db):
    """Client with a single 99001 entry in the import DB."""
    monkeypatch.setenv("FM_DATABASE_URL", f"sqlite:///{tmp_path}/ui_sync2.db")
    monkeypatch.setattr(sh, "_transport_factory", lambda c: fake_transport)
    monkeypatch.setattr(sh, "_import_db_path", single_entry_db)
    monkeypatch.setattr(sh, "_sleep", lambda s: None)
    with TestClient(create_app(Settings(config=cfg))) as c:
        yield c


@pytest.fixture
def proposal(single_client):
    """Run import_scan and return the first proposal dict."""
    single_client.post("/migration/import")
    with SessionLocal() as s:
        job = s.scalars(
            select(Job).where(
                Job.kind == "import_scan", Job.status == "done",
            ).order_by(Job.id.desc())
        ).first()
        assert job is not None, "import_scan job did not complete"
        result = json.loads(job.result)
        proposals = result["proposals"]
        assert len(proposals) == 1
        return proposals[0]


# ---------------------------------------------------------------------------
# Sync page
# ---------------------------------------------------------------------------

class TestSyncPage:
    def test_sync_page_renders(self, client):
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"Sync" in r.content
        assert b"drift-table" in r.content

    def test_sync_page_shows_actions(self, client):
        r = client.get("/sync")
        assert b"Push all" in r.content
        assert b"Temperature sync" in r.content
        assert b"hx-post" in r.content

    def test_sync_page_job_progress_polling(self, client):
        r = client.get("/sync")
        assert b"job-progress" in r.content
        assert b"hx-get" in r.content
        assert b"/sync/progress" in r.content

    def test_sync_progress_fragment(self, client):
        r = client.get("/sync/progress?since=0")
        assert r.status_code == 200
        # No jobs yet → empty message
        assert b"No new jobs" in r.content

    def test_sync_page_busy_dialog_when_awaiting(self, client, monkeypatch,
                                                  sample_db, tmp_path):
        """When a push job is awaiting_input, the sync page shows the
        busy dialog with the three choice buttons."""
        # Create a profile so push has something to push
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Make transport busy
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        # Cache DB needs >= MIN_DB_ENTRIES (30) entries for push to proceed
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)

        client.post("/api/sync/push", json={})
        r = client.get("/sync")
        assert r.status_code == 200
        assert b"busy-dialog" in r.content
        assert b"Force reboot" in r.content
        assert b"Wait" in r.content
        assert b"No reboot" in r.content

    def test_sync_resolve_force_reboot_transition(self, client, monkeypatch,
                                                   sample_db, tmp_path):
        """Integration test: POST /sync/jobs/{id}/resolve with form-urlencoded
        body (what HTMX actually sends) transitions the job out of
        awaiting_input.  This exercises the real click path — the buttons in
        sync.html POST form data to the UI resolve endpoint, not JSON to the
        API endpoint."""
        # Create a profile so push has something to push
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        # Make transport busy so push parks as awaiting_input
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]
        assert get_job_status(client, job_id) == "awaiting_input"

        # Simulate HTMX click: form-urlencoded POST to the UI resolve endpoint
        r = client.post(f"/sync/jobs/{job_id}/resolve",
                        data={"choice": "force_reboot"})
        assert r.status_code == 200
        # After force_reboot, the job should transition to done (FakeTransport
        # with no busy → push succeeds). The busy-dialog fragment should be
        # empty (no more awaiting_input jobs).
        assert get_job_status(client, job_id) == "done"
        assert b"busy-dialog" not in r.content

    def test_sync_resolve_wait_stays_awaiting(self, client, monkeypatch,
                                               sample_db, tmp_path):
        """The 'wait' choice retries without force — still busy → parks again.
        The returned fragment should still contain the busy-dialog."""
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]

        r = client.post(f"/sync/jobs/{job_id}/resolve",
                        data={"choice": "wait"})
        assert r.status_code == 200
        assert get_job_status(client, job_id) == "awaiting_input"
        assert b"busy-dialog" in r.content

    def test_sync_resolve_no_reboot_transition(self, client, monkeypatch,
                                                sample_db, tmp_path):
        """The 'no_reboot' choice transitions the job to done."""
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]

        r = client.post(f"/sync/jobs/{job_id}/resolve",
                        data={"choice": "no_reboot"})
        assert r.status_code == 200
        assert get_job_status(client, job_id) == "done"

    def test_sync_resolve_invalid_choice_422(self, client, monkeypatch,
                                              sample_db, tmp_path):
        """Invalid choice returns 422 from the UI endpoint."""
        client.post("/api/profiles", json={
            "brand": "Sunlu", "name": "Sunlu PLA+", "type": "PLA",
            "min_temp": 205, "max_temp": 215, "density": 1.23,
        })
        busy_transport = FakeTransport(busy={"filename": "x.gcode", "progress": 50})
        monkeypatch.setattr(sh, "_transport_factory", lambda c: busy_transport)
        cache = tmp_path / "cache.json"
        cache.write_text(json.dumps(sample_db))
        monkeypatch.setattr(sh, "_cache_db_path", cache)

        r = client.post("/api/sync/push", json={})
        job_id = r.json()["job_id"]

        r = client.post(f"/sync/jobs/{job_id}/resolve",
                        data={"choice": "bogus"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Migration wizard
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migration_step1_no_import(self, client):
        """With no prior import_scan, /migration shows step 1."""
        r = client.get("/migration")
        assert r.status_code == 200
        assert b"Step 1" in r.content
        assert b"Start import scan" in r.content

    def test_migration_proposes_99001_to_99004(self, fake_transport, client):
        """After import_scan, /migration shows proposals for 99001–99004."""
        r = client.post("/migration/import", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/migration"

        r = client.get("/migration")
        assert r.status_code == 200
        assert b"99001" in r.content
        assert b"99002" in r.content
        assert b"99003" in r.content
        assert b"99004" in r.content
        assert b"Sunlu" in r.content
        assert b"eSUN" in r.content
        assert b"Polymaker" in r.content
        assert b"Bambu" in r.content


    def test_migration_apply_creates_profiles(self, client):
        """POST /migration/apply with selected entries creates profiles."""
        client.post("/migration/import")
        r = client.post("/migration/apply", data={
            "include": ["99001", "99003"],
        }, follow_redirects=False)
        assert r.status_code == 303

        with SessionLocal() as s:
            profiles = list(s.scalars(select(Profile).order_by(Profile.id)))
            assert len(profiles) == 2
            ids = {p.printer_db_id for p in profiles}
            assert ids == {"99001", "99003"}

    def test_migration_apply_creates_sync_targets(self, client):
        """Applied profiles get a SyncTarget(printer_db)."""
        client.post("/migration/import")
        client.post("/migration/apply", data={"include": "99001"})

        with SessionLocal() as s:
            targets = list(s.scalars(select(SyncTarget)))
            assert len(targets) == 1
            assert targets[0].kind == "printer_db"

    def test_migration_import_preserves_filament_id(self, single_client, proposal):
        """When a profile with the same printer_db_id already exists,
        migration apply must preserve its filament_id (Cloud-collision
        avoidance)."""
        pid = proposal["printer_db_id"]  # "99001"

        # Create an existing profile with the same printer_db_id
        resp = single_client.post("/api/profiles", json={
            "brand": "Old", "name": "Old Name", "type": "PLA",
            "min_temp": 200, "max_temp": 210, "density": 1.2,
        })
        assert resp.status_code == 201
        profile_id = resp.json()["id"]

        # Set printer_db_id + filament_id directly (API doesn't expose these)
        with SessionLocal() as s:
            p = s.get(Profile, profile_id)
            p.printer_db_id = pid
            p.filament_id = "P1234567890123"

        # Apply migration with this entry selected
        r = single_client.post("/migration/apply", data={"include": pid},
                               follow_redirects=False)
        assert r.status_code == 303

        # filament_id must be preserved, not overwritten
        with SessionLocal() as s:
            p = s.get(Profile, profile_id)
            assert p.filament_id == "P1234567890123"
            # Profile fields should be updated from the proposal
            assert p.brand == proposal["brand"]
            assert p.name == proposal["name"]
            assert p.min_temp == proposal["min_temp"]
            assert p.max_temp == proposal["max_temp"]

    def test_migration_apply_new_profile_has_no_filament_id(self, client):
        """A brand-new profile created by migration has filament_id=None."""
        client.post("/migration/import")
        client.post("/migration/apply", data={"include": "99001"})

        with SessionLocal() as s:
            p = s.scalars(
                select(Profile).where(Profile.printer_db_id == "99001")
            ).first()
            assert p is not None
            assert p.filament_id is None

    def test_migration_apply_no_import_404(self, client):
        """POST /migration/apply without a prior import_scan → 404."""
        r = client.post("/migration/apply", data={"include": "99001"})
        assert r.status_code == 404
