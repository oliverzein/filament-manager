# tests/test_ui_sync.py
"""UI sync page + import page tests.

Pins the brief-specified tests:
  * test_import_printer_scan_proposes_99001_to_99004
  * test_import_printer_apply_creates_profiles
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
        id_range_start=99001,
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
    single_client.post("/import/printer/scan")
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
        assert b"Sync Hub" in r.content
        assert b"drift-table" in r.content

    def test_sync_page_has_direction_clear_labels(self, client):
        """No old labels; new direction-clear labels present."""
        r = client.get("/sync")
        assert b"Push to Printer" in r.content
        assert b"Push to Cloud" in r.content
        assert b"Pull from Cloud" in r.content
        assert b"Backup to GitHub" in r.content
        # Old labels must NOT appear
        assert b"Sync from Orca" not in r.content
        assert b"Push all" not in r.content
        assert b"Sync now" not in r.content

    def test_sync_page_has_target_cards(self, client):
        """M3 layout: three target cards (Printer DB, Orca Cloud, Backup)."""
        r = client.get("/sync")
        assert b"Printer DB" in r.content
        assert b"Orca Cloud" in r.content
        assert b"GitHub backup" in r.content

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
# Import page (merged migration + import-orca)
# ---------------------------------------------------------------------------

class TestImportPage:
    def test_import_page_default_tab(self, client):
        """GET /import defaults to printer tab."""
        r = client.get("/import")
        assert r.status_code == 200
        assert b"From Printer DB" in r.content
        assert b"From Orca Cloud" in r.content

    def test_import_page_printer_tab(self, client):
        """GET /import?tab=printer shows printer DB import content."""
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"Start import scan" in r.content

    def test_import_page_orca_tab(self, client):
        """GET /import?tab=orca shows Orca Cloud import content."""
        r = client.get("/import?tab=orca")
        assert r.status_code == 200

    def test_old_migration_route_404(self, client):
        """GET /migration returns 404 (route removed)."""
        r = client.get("/migration")
        assert r.status_code == 404

    def test_old_import_orca_route_404(self, client):
        """GET /import-orca returns 404 (route removed)."""
        r = client.get("/import-orca")
        assert r.status_code == 404

    def test_import_printer_scan_redirects(self, client):
        """POST /import/printer/scan enqueues import_scan and redirects."""
        r = client.post("/import/printer/scan", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/import?tab=printer"

    def test_import_printer_scan_proposes_99001_to_99004(self, client):
        """After scan, /import?tab=printer shows proposals."""
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"99001" in r.content
        assert b"Sunlu" in r.content

    def test_import_printer_apply_creates_profiles(self, client):
        """POST /import/printer/apply with selected entries creates profiles."""
        client.post("/import/printer/scan")
        r = client.post("/import/printer/apply", data={
            "include": ["99001", "99003"],
        }, follow_redirects=False)
        assert r.status_code == 303

        with SessionLocal() as s:
            profiles = list(s.scalars(select(Profile).order_by(Profile.id)))
            assert len(profiles) == 2
            ids = {p.printer_db_id for p in profiles}
            assert ids == {"99001", "99003"}

    def test_import_printer_apply_no_scan_404(self, client):
        """POST /import/printer/apply without a prior scan → 404."""
        r = client.post("/import/printer/apply", data={"include": "99001"})
        assert r.status_code == 404
    def test_import_step_scan_with_prior_job(self, client):
        """GET /import?tab=printer&step=scan shows the scan card even when
        a completed import_scan job exists (Back button target)."""
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer&step=scan")
        assert r.status_code == 200
        assert b"Start import scan" in r.content
        assert b"migration-step2" not in r.content

    def test_import_default_shows_review_with_prior_job(self, client):
        """GET /import?tab=printer (no step) still shows the review table
        when a completed import_scan job exists (default behaviour unchanged)."""
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"migration-step2" in r.content
        assert b"Start import scan" not in r.content

    def test_import_review_has_back_link(self, client):
        """GET /import?tab=printer with a prior job renders a back link to
        the scan view (/import?tab=printer&step=scan)."""
        client.post("/import/printer/scan")
        r = client.get("/import?tab=printer")
        assert r.status_code == 200
        assert b"/import?tab=printer&step=scan" in r.content
